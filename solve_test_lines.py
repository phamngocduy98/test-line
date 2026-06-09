#!/usr/bin/env python3
"""Build optimized telecom test line specs from testcase requirements.

The solver is exact over a bounded, deterministic candidate pool. It requires
OR-Tools for assignment optimization.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Iterable


ANY = "any"
RELATION_TOKENS = {"intra", "inter"}
SINGLE_SELECT_COLUMNS = {"cc location"}
DU_COLUMNS = ("enb", "vdu", "au", "cu")
RU_COLUMN = "ru"
UE_COLUMN = "ue"
NUMERIC_EQUIPMENT_COLUMNS = set(DU_COLUMNS) | {UE_COLUMN}
DEFAULT_MAX_COMPATIBILITY_VARIANTS = 64


@dataclass(frozen=True)
class TestCase:
    index: int
    tc_id: str
    raw: dict[str, str]
    tokens: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Candidate:
    spec: dict[str, tuple[str, ...]]
    covered: tuple[int, ...]
    deltas: tuple[int, ...]
    equipment_count: int
    signature: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class RuBandSupport:
    ru_names: dict[str, str]
    lte_band_names: dict[str, str]
    nr_band_names: dict[str, str]
    lte_by_ru: dict[str, frozenset[str]]
    nr_by_ru: dict[str, frozenset[str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Optimize telecom test line specs from testcase requirements."
    )
    parser.add_argument("--input", default="input.csv", help="Input testcase CSV path.")
    parser.add_argument("--output", default="output_specs.csv", help="Output specs CSV path.")
    parser.add_argument(
        "--ru-band-support",
        required=True,
        help="Required CSV mapping RUs to supported LTE and NR bands.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="OR-Tools solve timeout in seconds. Default: 600.",
    )
    parser.add_argument(
        "--max-candidates-per-bucket",
        type=int,
        default=250,
        help="Candidate cap per compatible bucket. Default: 250.",
    )
    parser.add_argument(
        "--max-cover-checks-per-candidate",
        type=int,
        default=0,
        help=(
            "Limit coverage checks per candidate for very large files. "
            "0 means check all rows. Exact-row candidates always cover themselves."
        ),
    )
    parser.add_argument(
        "--ignore-tech-and-ue-capa",
        action="store_true",
        help=(
            "Ignore all tech and ue capa columns during optimization. "
            "Ignored columns are blank in the output."
        ),
    )
    parser.add_argument(
        "--max-tc-per-spec",
        type=int,
        default=338,
        help="Maximum assigned testcases per selected spec. Default: 338.",
    )
    return parser.parse_args()


def is_temporarily_ignored_column(column: str) -> bool:
    normalized = column.strip().lower()
    return normalized.startswith("tech ") or normalized.startswith("ue capa ")


def parse_cell(value: str | None) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    slots: list[str] = []
    for part in re.split(r"\s*\+\s*", value):
        alternatives = [item.strip() for item in re.split(r"\s*/\s*", part) if item.strip()]
        if alternatives:
            slots.append("/".join(dict.fromkeys(alternatives)))
    return tuple(slots)


def render_cell(tokens: Iterable[str]) -> str:
    return " + ".join(tokens)


def is_any(token: str) -> bool:
    return ANY in {alternative.lower() for alternative in token.split("/")}


def alternatives(token: str) -> frozenset[str]:
    return frozenset(part.lower() for part in token.split("/"))


def slots_cover(spec_tokens: tuple[str, ...], case_tokens: tuple[str, ...]) -> bool:
    if len(spec_tokens) < len(case_tokens):
        return False

    compatible_specs = []
    for case_token in case_tokens:
        matches = []
        for index, spec_token in enumerate(spec_tokens):
            if (
                is_any(case_token)
                or is_any(spec_token)
                or alternatives(case_token) & alternatives(spec_token)
            ):
                matches.append(index)
        if not matches:
            return False
        compatible_specs.append(matches)

    matched_cases: dict[int, int] = {}

    def assign(case_index: int, seen: set[int]) -> bool:
        for spec_index in compatible_specs[case_index]:
            if spec_index in seen:
                continue
            seen.add(spec_index)
            previous_case = matched_cases.get(spec_index)
            if previous_case is None or assign(previous_case, seen):
                matched_cases[spec_index] = case_index
                return True
        return False

    for case_index in sorted(
        range(len(case_tokens)), key=lambda index: len(compatible_specs[index])
    ):
        if not assign(case_index, set()):
            return False
    return True


def any_count(tokens: Iterable[str]) -> int:
    return sum(1 for token in tokens if is_any(token))


def concrete_tokens(tokens: Iterable[str]) -> tuple[str, ...]:
    return tuple(token for token in tokens if not is_any(token))


def all_integer_tokens(tokens: Iterable[str]) -> bool:
    values = tuple(tokens)
    if not values:
        return False
    try:
        for token in values:
            int(token)
    except ValueError:
        return False
    return True


def load_cases(path: Path) -> tuple[list[str], list[TestCase]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path} has no header row")
        if "tc_id" not in reader.fieldnames:
            raise SystemExit(f"{path} must contain a tc_id column")
        columns = list(reader.fieldnames)
        rows = list(reader)

    cases: list[TestCase] = []
    for index, row in enumerate(rows):
        tc_id = (row.get("tc_id") or "").strip()
        if not tc_id:
            raise SystemExit(f"row {index + 2} has an empty tc_id")
        tokens = {column: parse_cell(row.get(column, "")) for column in columns if column != "tc_id"}
        raw = {column: row.get(column, "") for column in columns if column != "tc_id"}
        cases.append(TestCase(index=index, tc_id=tc_id, raw=raw, tokens=tokens))
    if not cases:
        raise SystemExit(f"{path} has no testcase rows")
    return columns, cases


def _support_values(value: str | None, column: str, row_number: int) -> list[str]:
    values: list[str] = []
    for token in parse_cell(value):
        for item in token.split("/"):
            normalized = item.strip().lower()
            if normalized == ANY or normalized in RELATION_TOKENS:
                raise SystemExit(
                    f"invalid {column} value {item!r} in RU support row {row_number}"
                )
            values.append(item.strip())
    return values


def load_ru_band_support(path: Path) -> RuBandSupport:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"ru", "lte_band", "nr_band"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise SystemExit(
                f"{path} must contain columns: ru,lte_band,nr_band"
            )
        rows = list(reader)

    ru_names: dict[str, str] = {}
    lte_band_names: dict[str, str] = {}
    nr_band_names: dict[str, str] = {}
    lte_by_ru: dict[str, set[str]] = defaultdict(set)
    nr_by_ru: dict[str, set[str]] = defaultdict(set)

    for row_number, row in enumerate(rows, start=2):
        ru_tokens = parse_cell(row.get("ru"))
        if (
            len(ru_tokens) != 1
            or is_any(ru_tokens[0])
            or len(alternatives(ru_tokens[0])) != 1
            or ru_tokens[0].lower() in RELATION_TOKENS
        ):
            raise SystemExit(
                f"RU support row {row_number} must contain one concrete ru value"
            )
        ru_name = ru_tokens[0].strip()
        ru_key = ru_name.lower()
        ru_names.setdefault(ru_key, ru_name)

        for band_name in _support_values(row.get("lte_band"), "lte_band", row_number):
            band_key = band_name.lower()
            lte_band_names.setdefault(band_key, band_name)
            lte_by_ru[ru_key].add(band_key)
        for band_name in _support_values(row.get("nr_band"), "nr_band", row_number):
            band_key = band_name.lower()
            nr_band_names.setdefault(band_key, band_name)
            nr_by_ru[ru_key].add(band_key)

    if not ru_names:
        raise SystemExit(f"{path} has no RU support rows")

    return RuBandSupport(
        ru_names=ru_names,
        lte_band_names=lte_band_names,
        nr_band_names=nr_band_names,
        lte_by_ru={
            ru: frozenset(lte_by_ru.get(ru, set())) for ru in ru_names
        },
        nr_by_ru={
            ru: frozenset(nr_by_ru.get(ru, set())) for ru in ru_names
        },
    )


def validate_support_references(
    requirement_columns: list[str],
    cases: list[TestCase],
    support: RuBandSupport,
) -> None:
    unknown_rus: set[str] = set()
    unknown_lte: set[str] = set()
    unknown_nr: set[str] = set()

    for case in cases:
        if RU_COLUMN in requirement_columns:
            for token in case.tokens[RU_COLUMN]:
                unknown_rus.update(
                    value
                    for value in alternatives(token) - {ANY}
                    if value not in support.ru_names
                )
        for column, names, unknown in (
            ("lte band", support.lte_band_names, unknown_lte),
            ("nr band", support.nr_band_names, unknown_nr),
        ):
            if column not in requirement_columns:
                continue
            for token in case.tokens[column]:
                unknown.update(
                    value
                    for value in alternatives(token) - RELATION_TOKENS - {ANY}
                    if value not in names
                )

    messages = []
    if unknown_rus:
        messages.append(f"unknown RUs: {', '.join(sorted(unknown_rus))}")
    if unknown_lte:
        messages.append(f"unknown LTE bands: {', '.join(sorted(unknown_lte))}")
    if unknown_nr:
        messages.append(f"unknown NR bands: {', '.join(sorted(unknown_nr))}")
    if messages:
        raise SystemExit("RU support table is incomplete; " + "; ".join(messages))


def split_band_tokens(tokens: Iterable[str]) -> tuple[list[str], set[str], int]:
    bands: list[str] = []
    relations: set[str] = set()
    anys = 0
    for token in tokens:
        if is_any(token):
            anys += 1
            continue
        options = alternatives(token)
        relation_options = options & RELATION_TOKENS
        band_options = options - RELATION_TOKENS
        relations.update(relation_options)
        if band_options:
            bands.append("/".join(sorted(band_options)))
    return bands, relations, anys


def relation_satisfied(tokens: tuple[str, ...], relation: str) -> bool:
    bands, relations, anys = split_band_tokens(tokens)
    if relation in relations:
        return True
    if len(bands) + anys < 2:
        return False
    if relation == "intra":
        return bool(anys or len(set(bands)) < len(bands))
    if relation == "inter":
        return bool(anys or len(set(bands)) >= 2)
    return False


def _selected_ru_keys(tokens: tuple[str, ...]) -> set[str]:
    return {
        value
        for token in tokens
        if not is_any(token)
        for value in alternatives(token)
    }


def _replace_any_alternative(token: str, replacement: str) -> str:
    parts = [
        part.strip()
        for part in token.split("/")
        if part.strip() and part.strip().lower() != ANY
    ]
    parts.append(replacement)
    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = part.lower()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(part)
    return "/".join(unique)


def spec_has_compatible_ru_bands(
    spec: dict[str, tuple[str, ...]], support: RuBandSupport
) -> bool:
    ru_tokens = spec.get(RU_COLUMN, ())
    if not ru_tokens:
        return True
    if any(is_any(token) for token in ru_tokens):
        return False

    selected_rus = _selected_ru_keys(ru_tokens)
    if not selected_rus or not selected_rus.issubset(support.ru_names):
        return False

    for column, support_by_ru in (
        ("lte band", support.lte_by_ru),
        ("nr band", support.nr_by_ru),
    ):
        band_tokens = spec.get(column, ())
        if not band_tokens:
            continue
        supported_bands = set().union(
            *(support_by_ru.get(ru, frozenset()) for ru in selected_rus)
        )
        for token in band_tokens:
            if is_any(token):
                return False
            band_options = alternatives(token) - RELATION_TOKENS
            if band_options and not band_options.intersection(supported_bands):
                return False
    return True


def resolve_compatibility_variants(
    spec: dict[str, tuple[str, ...]],
    support: RuBandSupport,
    max_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> list[dict[str, tuple[str, ...]]]:
    max_variants = max(1, max_variants)
    ru_tokens = spec.get(RU_COLUMN, ())
    ru_domains: list[tuple[str, ...]] = []
    for token in ru_tokens:
        if is_any(token):
            ru_domains.append(
                tuple(
                    _replace_any_alternative(token, support.ru_names[key])
                    for key in sorted(support.ru_names)
                )
            )
        else:
            ru_domains.append((token,))

    ru_assignments = product(*ru_domains) if ru_domains else [()]
    variants: list[dict[str, tuple[str, ...]]] = []
    seen: set[tuple[tuple[str, tuple[str, ...]], ...]] = set()

    for ru_assignment in ru_assignments:
        selected_rus = _selected_ru_keys(tuple(ru_assignment))
        band_domains_by_column: list[tuple[str, list[tuple[str, ...]]]] = []
        feasible = True

        for column, names, support_by_ru in (
            ("lte band", support.lte_band_names, support.lte_by_ru),
            ("nr band", support.nr_band_names, support.nr_by_ru),
        ):
            tokens = spec.get(column, ())
            supported_bands = (
                set().union(
                    *(support_by_ru.get(ru, frozenset()) for ru in selected_rus)
                )
                if selected_rus
                else set(names)
            )
            token_domains: list[tuple[str, ...]] = []
            for token in tokens:
                if is_any(token):
                    choices = tuple(
                        _replace_any_alternative(token, names[key])
                        for key in sorted(supported_bands)
                        if key in names
                    )
                    if not choices:
                        feasible = False
                        break
                    token_domains.append(choices)
                    continue
                band_options = alternatives(token) - RELATION_TOKENS
                if (
                    selected_rus
                    and band_options
                    and not band_options.intersection(supported_bands)
                ):
                    feasible = False
                    break
                token_domains.append((token,))
            if not feasible:
                break
            band_domains_by_column.append((column, token_domains))

        if not feasible:
            continue

        flattened_domains = [
            domain
            for _, token_domains in band_domains_by_column
            for domain in token_domains
        ]
        band_assignments = product(*flattened_domains) if flattened_domains else [()]
        for band_assignment in band_assignments:
            variant = dict(spec)
            if ru_tokens:
                variant[RU_COLUMN] = tuple(ru_assignment)
            offset = 0
            for column, token_domains in band_domains_by_column:
                count = len(token_domains)
                if column in spec:
                    variant[column] = tuple(band_assignment[offset : offset + count])
                offset += count

            if not spec_has_compatible_ru_bands(variant, support):
                continue
            signature = spec_signature(variant)
            if signature in seen:
                continue
            seen.add(signature)
            variants.append(variant)
            if len(variants) >= max_variants:
                return variants

    return variants


def covers_column(
    column: str,
    spec_tokens: tuple[str, ...],
    case_tokens: tuple[str, ...],
    enforce_delta: bool = True,
) -> tuple[bool, int]:
    if not case_tokens:
        return True, 0

    if column in SINGLE_SELECT_COLUMNS:
        concrete = [token for token in spec_tokens if not is_any(token)]
        if len(concrete) > 1:
            return False, 0

    if (
        column in NUMERIC_EQUIPMENT_COLUMNS
        and all_integer_tokens(spec_tokens)
        and all_integer_tokens(case_tokens)
    ):
        spec_count = sum(map(int, spec_tokens))
        case_count = sum(map(int, case_tokens))
        if spec_count < case_count:
            return False, 0
        return True, spec_count - case_count
    elif column in {"lte band", "nr band"}:
        for token in case_tokens:
            lower = token.lower()
            if is_any(token):
                continue
            if lower in RELATION_TOKENS:
                if not relation_satisfied(spec_tokens, lower):
                    return False, 0
        case_bands, _, _ = split_band_tokens(case_tokens)
        spec_bands, _, _ = split_band_tokens(spec_tokens)
        if not slots_cover(tuple(spec_bands), tuple(case_bands)):
            return False, 0
    else:
        if not slots_cover(spec_tokens, case_tokens):
            return False, 0

    required_slots = len(concrete_tokens(case_tokens)) + any_count(case_tokens)
    if column in {"lte band", "nr band"}:
        case_bands, case_relations, case_anys = split_band_tokens(case_tokens)
        required_slots = len(case_bands) + case_anys + len(case_relations)
    if len(spec_tokens) < required_slots:
        return False, 0

    delta = len(spec_tokens) - len(case_tokens)
    if enforce_delta and delta > 1:
        return False, 0
    return True, max(0, delta)


def coverage_delta(
    requirement_columns: list[str],
    candidate_spec: dict[str, tuple[str, ...]],
    case: TestCase,
    enforce_delta: bool = True,
    support: RuBandSupport | None = None,
) -> tuple[bool, int]:
    if support is not None and not spec_has_compatible_ru_bands(candidate_spec, support):
        return False, 0
    total_delta = 0
    for column in requirement_columns:
        ok, delta = covers_column(
            column,
            candidate_spec[column],
            case.tokens[column],
            enforce_delta=enforce_delta,
        )
        if not ok:
            return False, 0
        total_delta += delta
    return True, total_delta


def merge_column(column: str, token_lists: Iterable[tuple[str, ...]]) -> tuple[str, ...] | None:
    token_list = list(token_lists)
    numeric_rows = [tokens for tokens in token_list if tokens]
    if (
        column in NUMERIC_EQUIPMENT_COLUMNS
        and numeric_rows
        and all(all_integer_tokens(tokens) for tokens in numeric_rows)
    ):
        return (str(max(sum(map(int, tokens)) for tokens in numeric_rows)),)

    ordered_tokens: list[str] = []
    max_counts: Counter[str] = Counter()
    max_len = 0
    for tokens in token_list:
        max_len = max(max_len, len(tokens))
        row_counts: Counter[str] = Counter()
        for token in tokens:
            lower = token.lower()
            if is_any(token):
                continue
            if column in {"lte band", "nr band"} and lower in RELATION_TOKENS:
                canonical = lower
            else:
                canonical = token
            row_counts[canonical] += 1
            if canonical not in ordered_tokens:
                ordered_tokens.append(canonical)
        for token, count in row_counts.items():
            max_counts[token] = max(max_counts[token], count)

    values: list[str] = []
    for token in ordered_tokens:
        values.extend([token] * max_counts[token])

    if column in SINGLE_SELECT_COLUMNS:
        concrete = [token for token in values if not is_any(token)]
        if len(concrete) > 1:
            return None

    while len(values) < max_len:
        values.append(ANY)
    return tuple(values)


def merge_cases(requirement_columns: list[str], cases: Iterable[TestCase]) -> dict[str, tuple[str, ...]] | None:
    case_list = list(cases)
    spec: dict[str, tuple[str, ...]] = {}
    for column in requirement_columns:
        merged = merge_column(column, (case.tokens[column] for case in case_list))
        if merged is None:
            return None
        spec[column] = merged
    return spec


def numeric_equipment(tokens: tuple[str, ...]) -> int:
    if not tokens:
        return 0
    total = 0
    for token in tokens:
        if is_any(token):
            total += 1
            continue
        try:
            total += int(token)
        except ValueError:
            total += 1
    return total


def equipment_count(requirement_columns: list[str], spec: dict[str, tuple[str, ...]]) -> int:
    total = 0
    for column in DU_COLUMNS:
        if column in requirement_columns:
            total += numeric_equipment(spec[column])
    if RU_COLUMN in requirement_columns:
        total += len(spec[RU_COLUMN])
    if UE_COLUMN in requirement_columns:
        total += numeric_equipment(spec[UE_COLUMN])
    return total


def spec_signature(spec: dict[str, tuple[str, ...]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((column, spec[column]) for column in sorted(spec))


def single_select_key(case: TestCase) -> tuple[tuple[str, tuple[str, ...]], ...]:
    parts: list[tuple[str, tuple[str, ...]]] = []
    for column in SINGLE_SELECT_COLUMNS:
        if column not in case.tokens:
            continue
        concrete = tuple(token for token in case.tokens[column] if not is_any(token))
        parts.append((column, concrete))
    return tuple(sorted(parts))


def coarse_signature(requirement_columns: list[str], case: TestCase, include_equipment: bool) -> tuple:
    parts: list[tuple[str, tuple[str, ...]]] = []
    for column in requirement_columns:
        tokens = case.tokens[column]
        if column in SINGLE_SELECT_COLUMNS:
            parts.append((column, tuple(token for token in tokens if not is_any(token))))
        elif column in {"lte band", "nr band"}:
            _, relations, _ = split_band_tokens(tokens)
            parts.append((column, tuple(sorted(relations))))
        elif include_equipment and (
            column in DU_COLUMNS or column in {RU_COLUMN, UE_COLUMN}
        ):
            parts.append((column, tokens))
        elif (
            not include_equipment
            and column not in DU_COLUMNS
            and column not in {RU_COLUMN, UE_COLUMN}
        ):
            parts.append((column, tuple(token for token in tokens if not is_any(token))))
    return tuple(parts)


def build_candidate_variants(
    requirement_columns: list[str],
    cases: list[TestCase],
    indices: Iterable[int],
    all_cases: list[TestCase],
    max_cover_checks: int,
    enforce_delta: bool = True,
    support: RuBandSupport | None = None,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> list[Candidate]:
    index_tuple = tuple(sorted(set(indices)))
    if not index_tuple:
        return []
    merged_spec = merge_cases(requirement_columns, (cases[index] for index in index_tuple))
    if merged_spec is None:
        return []
    specs = (
        resolve_compatibility_variants(
            merged_spec, support, max_variants=max_compatibility_variants
        )
        if support is not None
        else [merged_spec]
    )

    check_cases = all_cases
    if max_cover_checks > 0 and len(all_cases) > max_cover_checks:
        seed = list(index_tuple)
        others = [case.index for case in all_cases if case.index not in set(seed)]
        selected = seed + others[: max(0, max_cover_checks - len(seed))]
        check_cases = [all_cases[index] for index in selected]

    candidates: list[Candidate] = []
    for spec in specs:
        covered: list[int] = []
        deltas: list[int] = []
        for case in check_cases:
            ok, delta = coverage_delta(
                requirement_columns,
                spec,
                case,
                enforce_delta=enforce_delta,
                support=support,
            )
            if ok:
                covered.append(case.index)
                deltas.append(delta)

        seed_is_covered = True
        for index in index_tuple:
            if index in covered:
                continue
            ok, delta = coverage_delta(
                requirement_columns,
                spec,
                all_cases[index],
                enforce_delta=enforce_delta,
                support=support,
            )
            if not ok:
                seed_is_covered = False
                break
            covered.append(index)
            deltas.append(delta)
        if not seed_is_covered:
            continue

        signature = spec_signature(spec)
        candidates.append(
            Candidate(
                spec=spec,
                covered=tuple(covered),
                deltas=tuple(deltas),
                equipment_count=equipment_count(requirement_columns, spec),
                signature=signature,
            )
        )
    return candidates


def build_candidate(
    requirement_columns: list[str],
    cases: list[TestCase],
    indices: Iterable[int],
    all_cases: list[TestCase],
    max_cover_checks: int,
    enforce_delta: bool = True,
    support: RuBandSupport | None = None,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> Candidate | None:
    variants = build_candidate_variants(
        requirement_columns,
        cases,
        indices,
        all_cases,
        max_cover_checks,
        enforce_delta=enforce_delta,
        support=support,
        max_compatibility_variants=max_compatibility_variants,
    )
    return variants[0] if variants else None


def add_candidate(
    candidates: dict[tuple[tuple[str, tuple[str, ...]], ...], Candidate],
    candidate: Candidate | None,
) -> None:
    if candidate is None:
        return
    current = candidates.get(candidate.signature)
    if current is None:
        candidates[candidate.signature] = candidate
        return
    # Identical specs can be generated from different seed rows. When coverage
    # checks are capped, each generation may discover a different subset. Keep
    # their union so deduplication cannot discard an exact row's self-coverage.
    delta_by_case = dict(zip(current.covered, current.deltas))
    for case_index, delta in zip(candidate.covered, candidate.deltas):
        previous = delta_by_case.get(case_index)
        if previous is None or delta < previous:
            delta_by_case[case_index] = delta
    covered = tuple(sorted(delta_by_case))
    candidates[candidate.signature] = Candidate(
        spec=current.spec,
        covered=covered,
        deltas=tuple(delta_by_case[index] for index in covered),
        equipment_count=current.equipment_count,
        signature=current.signature,
    )


def generate_candidates(
    requirement_columns: list[str],
    cases: list[TestCase],
    max_candidates_per_bucket: int,
    max_cover_checks: int,
    support: RuBandSupport | None = None,
) -> list[Candidate]:
    candidates: dict[tuple[tuple[str, tuple[str, ...]], ...], Candidate] = {}

    for case in cases:
        exact_variants = build_candidate_variants(
            requirement_columns,
            cases,
            [case.index],
            cases,
            max_cover_checks,
            support=support,
            max_compatibility_variants=max_candidates_per_bucket,
        )
        if support is not None and not exact_variants:
            raise SystemExit(
                f"testcase {case.tc_id} has no compatible RU-band realization"
            )
        for candidate in exact_variants:
            add_candidate(candidates, candidate)

    bucket_map: dict[tuple, list[TestCase]] = defaultdict(list)
    for case in cases:
        bucket_map[single_select_key(case)].append(case)

    for bucket_cases in bucket_map.values():
        sorted_bucket = sorted(
            bucket_cases,
            key=lambda case: (
                sum(len(case.tokens[column]) for column in requirement_columns),
                case.index,
            ),
        )
        if len(sorted_bucket) > 1:
            for candidate in build_candidate_variants(
                    requirement_columns,
                    cases,
                    [case.index for case in sorted_bucket],
                    cases,
                    max_cover_checks,
                    support=support,
                    max_compatibility_variants=max_candidates_per_bucket,
                ):
                add_candidate(candidates, candidate)

        for window_size in (2, 3, 5, 8, 13, 21, 34, 55):
            if window_size > len(sorted_bucket):
                continue
            made = 0
            step = max(1, window_size // 2)
            for start in range(0, len(sorted_bucket) - window_size + 1, step):
                for candidate in build_candidate_variants(
                        requirement_columns,
                        cases,
                        [case.index for case in sorted_bucket[start : start + window_size]],
                        cases,
                        max_cover_checks,
                        support=support,
                        max_compatibility_variants=max_candidates_per_bucket,
                    ):
                    add_candidate(candidates, candidate)
                made += 1
                if made >= max_candidates_per_bucket:
                    break

        signature_groups: dict[tuple, list[TestCase]] = defaultdict(list)
        for case in sorted_bucket:
            signature_groups[coarse_signature(requirement_columns, case, include_equipment=False)].append(case)
            signature_groups[coarse_signature(requirement_columns, case, include_equipment=True)].append(case)

        ranked_groups = sorted(
            signature_groups.values(),
            key=lambda group: (-len(group), min(case.index for case in group)),
        )
        for group in ranked_groups[:max_candidates_per_bucket]:
            if len(group) > 1:
                for candidate in build_candidate_variants(
                        requirement_columns,
                        cases,
                        [case.index for case in group],
                        cases,
                        max_cover_checks,
                        support=support,
                        max_compatibility_variants=max_candidates_per_bucket,
                    ):
                    add_candidate(candidates, candidate)

    return sorted(
        candidates.values(),
        key=lambda candidate: (
            candidate.equipment_count,
            -len(candidate.covered),
            sum(candidate.deltas),
            candidate.signature,
        ),
    )


def expand_candidates_for_capacity(
    candidates: list[Candidate], max_tc_per_spec: int
) -> list[Candidate]:
    expanded: list[Candidate] = []
    for candidate in candidates:
        copy_count = max(1, math.ceil(len(candidate.covered) / max_tc_per_spec))
        expanded.extend([candidate] * copy_count)
    return expanded


def solve_with_ortools(
    candidates: list[Candidate],
    cases: list[TestCase],
    timeout_seconds: float,
    max_tc_per_spec: int,
) -> tuple[str, list[int], dict[int, int]]:
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        raise SystemExit("Missing dependency: pip install ortools") from None

    model = cp_model.CpModel()
    num_cases = len(cases)
    num_candidates = len(candidates)

    selected = [model.NewBoolVar(f"selected_{j}") for j in range(num_candidates)]
    assignments: dict[tuple[int, int], object] = {}
    coverers: dict[int, list[int]] = defaultdict(list)
    delta_by_assignment: dict[tuple[int, int], int] = {}

    for j, candidate in enumerate(candidates):
        for case_index, delta in zip(candidate.covered, candidate.deltas):
            var = model.NewBoolVar(f"assign_{case_index}_{j}")
            assignments[(case_index, j)] = var
            coverers[case_index].append(j)
            delta_by_assignment[(case_index, j)] = delta
            model.Add(var <= selected[j])

    for case in cases:
        if not coverers[case.index]:
            raise SystemExit(f"no candidate covers testcase {case.tc_id}")
        model.Add(sum(assignments[(case.index, j)] for j in coverers[case.index]) == 1)

    assigned_count_vars = []
    selected_count = sum(selected)
    for j, candidate in enumerate(candidates):
        assigned_count = model.NewIntVar(0, num_cases, f"assigned_count_{j}")
        model.Add(
            assigned_count
            == sum(assignments[(case_index, j)] for case_index in candidate.covered)
        )
        model.Add(assigned_count >= selected[j])
        model.Add(assigned_count <= max_tc_per_spec * selected[j])
        assigned_count_vars.append(assigned_count)

    max_equipment = model.NewIntVar(0, max(c.equipment_count for c in candidates), "max_equipment")
    for j, candidate in enumerate(candidates):
        model.Add(max_equipment >= candidate.equipment_count * selected[j])

    total_equipment = sum(candidates[j].equipment_count * selected[j] for j in range(num_candidates))
    total_delta = sum(
        delta_by_assignment[key] * var for key, var in assignments.items()
    )

    max_assigned = model.NewIntVar(0, num_cases, "max_assigned")
    min_assigned = model.NewIntVar(0, num_cases, "min_assigned")
    for j in range(num_candidates):
        model.Add(max_assigned >= assigned_count_vars[j])
        # If not selected, use a large relaxed upper side so it does not force min to 0.
        model.Add(min_assigned <= assigned_count_vars[j] + num_cases * (1 - selected[j]))
    imbalance = model.NewIntVar(0, num_cases, "imbalance")
    model.Add(imbalance == max_assigned - min_assigned)

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = max(1, min(8, os.cpu_count() or 8))
    started_at = time.monotonic()

    objectives = (
        max_equipment,
        total_equipment,
        selected_count,
        imbalance,
        total_delta,
    )
    solve_status = "OPTIMAL"
    status = None
    for objective in objectives:
        remaining = max(0.1, timeout_seconds - (time.monotonic() - started_at))
        solver.parameters.max_time_in_seconds = remaining
        model.Minimize(objective)
        status = solver.Solve(model)
        if status == cp_model.OPTIMAL:
            model.Add(objective == int(solver.ObjectiveValue()))
            continue
        if status == cp_model.FEASIBLE:
            solve_status = "FEASIBLE_TIMEOUT"
            break
        raise SystemExit("no feasible solution found")

    selected_indices = [j for j, var in enumerate(selected) if solver.BooleanValue(var)]
    assignment_by_case: dict[int, int] = {}
    for (case_index, candidate_index), var in assignments.items():
        if solver.BooleanValue(var):
            assignment_by_case[case_index] = candidate_index

    return solve_status, selected_indices, assignment_by_case


def validate_solution(
    requirement_columns: list[str],
    cases: list[TestCase],
    candidates: list[Candidate],
    selected_indices: list[int],
    assignment_by_case: dict[int, int],
    max_tc_per_spec: int,
    enforce_delta: bool = True,
    support: RuBandSupport | None = None,
) -> None:
    if set(assignment_by_case) != {case.index for case in cases}:
        raise SystemExit("verification failed: every testcase must be assigned exactly once")
    selected_set = set(selected_indices)
    assigned_candidates = set(assignment_by_case.values())
    if assigned_candidates != selected_set:
        raise SystemExit("verification failed: selected specs and assigned specs differ")

    assigned_counts = Counter(assignment_by_case.values())
    if any(count > max_tc_per_spec for count in assigned_counts.values()):
        raise SystemExit("verification failed: testcase limit exceeded")

    for case in cases:
        candidate = candidates[assignment_by_case[case.index]]
        ok, _ = coverage_delta(
            requirement_columns,
            candidate.spec,
            case,
            enforce_delta=enforce_delta,
            support=support,
        )
        if not ok:
            raise SystemExit(f"verification failed: testcase {case.tc_id} is not covered")
        for column in SINGLE_SELECT_COLUMNS:
            if column in candidate.spec:
                concrete = [token for token in candidate.spec[column] if not is_any(token)]
                if len(concrete) > 1:
                    raise SystemExit(f"verification failed: {column} has multiple concrete values")
        if candidate.equipment_count != equipment_count(requirement_columns, candidate.spec):
            raise SystemExit("verification failed: equipment count mismatch")
        if support is not None and not spec_has_compatible_ru_bands(
            candidate.spec, support
        ):
            raise SystemExit("verification failed: incompatible RU-band spec")


def write_output(
    path: Path,
    input_columns: list[str],
    requirement_columns: list[str],
    cases: list[TestCase],
    candidates: list[Candidate],
    selected_indices: list[int],
    assignment_by_case: dict[int, int],
    solve_status: str,
    enforce_delta: bool = True,
    support: RuBandSupport | None = None,
) -> tuple[int, int, int, int, list[int]]:
    assignments_by_candidate: dict[int, list[TestCase]] = defaultdict(list)
    for case in cases:
        assignments_by_candidate[assignment_by_case[case.index]].append(case)

    output_columns = [
        "spec_id",
        "assigned_tc_ids",
        "assigned_count",
        "covered_tc_ids",
        "covered_count",
        "equipment_count",
        "total_delta",
        "solve_status",
    ] + [column for column in input_columns if column != "tc_id"]

    selected_sorted = sorted(
        selected_indices,
        key=lambda index: (
            candidates[index].equipment_count,
            -len(assignments_by_candidate[index]),
            min(case.index for case in assignments_by_candidate[index]),
        ),
    )

    total_delta = 0
    distribution: list[int] = []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_columns)
        writer.writeheader()
        for spec_number, candidate_index in enumerate(selected_sorted, start=1):
            candidate = candidates[candidate_index]
            assigned_cases = sorted(assignments_by_candidate[candidate_index], key=lambda case: case.index)
            assigned_ids = [case.tc_id for case in assigned_cases]
            covered_ids = [cases[index].tc_id for index in candidate.covered]
            assigned_delta = sum(
                coverage_delta(
                    requirement_columns,
                    candidate.spec,
                    case,
                    enforce_delta=enforce_delta,
                    support=support,
                )[1]
                for case in assigned_cases
            )
            total_delta += assigned_delta
            distribution.append(len(assigned_cases))

            row = {
                "spec_id": f"spec_{spec_number}",
                "assigned_tc_ids": " + ".join(assigned_ids),
                "assigned_count": len(assigned_ids),
                "covered_tc_ids": " + ".join(covered_ids),
                "covered_count": len(covered_ids),
                "equipment_count": candidate.equipment_count,
                "total_delta": assigned_delta,
                "solve_status": solve_status,
            }
            for column in requirement_columns:
                row[column] = render_cell(candidate.spec[column])
            writer.writerow(row)

    selected_candidates = [candidates[index] for index in selected_indices]
    max_equipment = max(candidate.equipment_count for candidate in selected_candidates)
    total_equipment = sum(candidate.equipment_count for candidate in selected_candidates)
    return len(selected_indices), max_equipment, total_equipment, total_delta, distribution


def main() -> int:
    args = parse_args()
    started_at = time.monotonic()
    input_path = Path(args.input)
    output_path = Path(args.output)
    support_path = Path(args.ru_band_support)
    if args.max_tc_per_spec <= 0:
        raise SystemExit("--max-tc-per-spec must be positive")

    input_columns, cases = load_cases(input_path)
    support = load_ru_band_support(support_path)
    requirement_columns = [column for column in input_columns if column != "tc_id"]
    if args.ignore_tech_and_ue_capa:
        requirement_columns = [
            column
            for column in requirement_columns
            if not is_temporarily_ignored_column(column)
        ]
    validate_support_references(requirement_columns, cases, support)

    candidates = generate_candidates(
        requirement_columns=requirement_columns,
        cases=cases,
        max_candidates_per_bucket=max(1, args.max_candidates_per_bucket),
        max_cover_checks=max(0, args.max_cover_checks_per_candidate),
        support=support,
    )
    if not candidates:
        raise SystemExit("no candidate specs generated")
    candidates = expand_candidates_for_capacity(candidates, args.max_tc_per_spec)

    solve_status, selected_indices, assignment_by_case = solve_with_ortools(
        candidates,
        cases,
        args.timeout,
        args.max_tc_per_spec,
    )
    validate_solution(
        requirement_columns,
        cases,
        candidates,
        selected_indices,
        assignment_by_case,
        args.max_tc_per_spec,
        support=support,
    )
    spec_count, max_equipment, total_equipment, total_delta, distribution = write_output(
        output_path,
        input_columns,
        requirement_columns,
        cases,
        candidates,
        selected_indices,
        assignment_by_case,
        solve_status,
        support=support,
    )

    elapsed = time.monotonic() - started_at
    print(f"status={solve_status}")
    print(f"runtime_seconds={elapsed:.2f}")
    print(f"input_testcases={len(cases)}")
    print(f"candidate_specs={len(candidates)}")
    print(f"selected_specs={spec_count}")
    print(f"assignment_distribution={distribution}")
    print(f"max_equipment={max_equipment}")
    print(f"total_equipment={total_equipment}")
    print(f"total_delta={total_delta}")
    print(f"output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
