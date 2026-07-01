"""Coverage and assignment-excess scoring."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, product

from .constants import BAND_COLUMNS, NUMERIC_COLUMNS, OPTIONAL_COLUMNS
from .models import SolveOptions, SupportTable, Token


@dataclass(frozen=True)
class CoverageResult:
    excess: int


def active_requirement_columns(columns: tuple[str, ...], options: SolveOptions) -> tuple[str, ...]:
    return tuple(
        column
        for column in columns
        if column != "tc_id" and not (options.ignore_optional_columns and column in OPTIONAL_COLUMNS)
    )


def numeric_value(tokens: tuple[Token, ...]) -> int:
    if not tokens:
        return 0
    return int(tokens[0].alternatives[0])


def equipment_count(spec: dict[str, tuple[Token, ...]]) -> int:
    total = sum(numeric_value(spec.get(column, ())) for column in NUMERIC_COLUMNS)
    total += len(spec.get("ru", ()))
    return total


def spec_ru_band_compatible(spec: dict[str, tuple[Token, ...]], support: SupportTable) -> bool:
    return _ru_band_compatible(spec, support)


def coverage_excess(
    testcase: dict[str, tuple[Token, ...]],
    spec: dict[str, tuple[Token, ...]],
    columns: tuple[str, ...],
    support: SupportTable,
    options: SolveOptions,
) -> CoverageResult | None:
    total = 0
    for column in columns:
        tc_tokens = testcase.get(column, ())
        spec_tokens = spec.get(column, ())
        if column in NUMERIC_COLUMNS:
            result = _numeric_coverage(tc_tokens, spec_tokens, options)
        else:
            result = _token_coverage(column, tc_tokens, spec_tokens, support, options)
        if result is None:
            return None
        total += result

    if not _ru_band_compatible(spec, support):
        return None
    if not _ru_band_covers(testcase, spec, support):
        return None
    return CoverageResult(total)


def _numeric_coverage(testcase: tuple[Token, ...], spec: tuple[Token, ...], options: SolveOptions) -> int | None:
    testcase_value = numeric_value(testcase)
    spec_value = numeric_value(spec)
    if spec_value < testcase_value:
        return None
    overage = spec_value - testcase_value
    if testcase_value > 0 and spec_value > testcase_value * options.max_numeric_overage_ratio and overage > options.max_numeric_overage_units:
        return None
    return overage


def _token_coverage(
    column: str,
    testcase: tuple[Token, ...],
    spec: tuple[Token, ...],
    support: SupportTable,
    options: SolveOptions,
) -> int | None:
    tc_relation_tokens = tuple(token for token in testcase if _is_relation_token(token) and column in BAND_COLUMNS)
    spec_relation_tokens = tuple(token for token in spec if _is_relation_token(token) and column in BAND_COLUMNS)
    tc_normal = tuple(token for token in testcase if token not in tc_relation_tokens)
    spec_normal = tuple(token for token in spec if token not in spec_relation_tokens)

    for relation in tc_relation_tokens:
        if not _relation_satisfied(relation, spec, column, support):
            return None

    relation_consumed_slots = 2 * len(tc_relation_tokens)
    if len(spec_normal) + relation_consumed_slots < len(tc_normal):
        return None
    extra_slots = max(0, len(spec_normal) - len(tc_normal) - relation_consumed_slots)
    if extra_slots > options.max_extra_slots:
        return None

    match_excess = _best_slot_match_excess(column, tc_normal, spec_normal, options)
    if match_excess is None:
        return None
    return extra_slots + match_excess


def _best_slot_match_excess(
    column: str,
    testcase: tuple[Token, ...],
    spec: tuple[Token, ...],
    options: SolveOptions,
) -> int | None:
    return _best_slot_match_excess_cached(
        column,
        tuple(token.normalized() for token in testcase),
        tuple(token.normalized() for token in spec),
        options.max_extra_alternatives,
        column in options.reject_spec_side_wildcard,
    )


@lru_cache(maxsize=50000)
def _best_slot_match_excess_cached(
    column: str,
    testcase: tuple[tuple[str, ...], ...],
    spec: tuple[tuple[str, ...], ...],
    max_extra_alternatives: int,
    reject_spec_side_wildcard: bool,
) -> int | None:
    if not testcase:
        return 0

    best: tuple[int, tuple[int, ...]] | None = None

    def visit(tc_index: int, used: set[int], excess: int, indexes: tuple[int, ...]) -> None:
        nonlocal best
        if best is not None and excess > best[0]:
            return
        if tc_index == len(testcase):
            candidate = (excess, indexes)
            if best is None or candidate < best:
                best = candidate
            return

        for spec_index, spec_token in enumerate(spec):
            if spec_index in used:
                continue
            slot_excess = _slot_excess_values(
                testcase[tc_index],
                spec_token,
                max_extra_alternatives,
                reject_spec_side_wildcard,
            )
            if slot_excess is None:
                continue
            used.add(spec_index)
            visit(tc_index + 1, used, excess + slot_excess, indexes + (spec_index,))
            used.remove(spec_index)

    visit(0, set(), 0, ())
    return None if best is None else best[0]


def _slot_excess_values(
    testcase: tuple[str, ...],
    spec: tuple[str, ...],
    max_extra_alternatives: int,
    reject_spec_side_wildcard: bool,
) -> int | None:
    tc_values = set(testcase)
    spec_values = set(spec)
    if "any" in tc_values:
        return 0
    if "any" in spec_values:
        if reject_spec_side_wildcard:
            return None
        return 1
    if not (tc_values & spec_values):
        return None
    extra = len(spec_values - tc_values)
    if extra > max_extra_alternatives:
        return None
    return extra


def _is_relation_token(token: Token) -> bool:
    values = {value.casefold() for value in token.alternatives}
    return bool(values & {"intra", "inter"})


def _relation_satisfied(token: Token, spec: tuple[Token, ...], column: str, support: SupportTable) -> bool:
    relations = {value.casefold() for value in token.alternatives if value.casefold() in {"intra", "inter"}}
    if any(relation in {value.casefold() for value in spec_token.alternatives} for relation in relations for spec_token in spec):
        return True

    concrete_domains = _band_domains(spec, column, support)
    for relation in relations:
        for left, right in combinations(range(len(concrete_domains)), 2):
            left_values = concrete_domains[left]
            right_values = concrete_domains[right]
            if relation == "intra" and left_values & right_values:
                return True
            if relation == "inter" and any(a != b for a in left_values for b in right_values):
                return True
    return False


def _ru_band_compatible(spec: dict[str, tuple[Token, ...]], support: SupportTable) -> bool:
    domains = _ru_domains(spec.get("ru", ()), support)
    if not domains:
        return not _has_band_requirements(spec)
    if any(not domain for domain in domains):
        return False
    total = 1
    for domain in domains:
        total *= len(domain)
        if total > 20000:
            selected = set().union(*domains)
            return _band_slots_supported(selected, spec.get("lte band", ()), "lte band", support) and _band_slots_supported(
                selected, spec.get("nr band", ()), "nr band", support
            )
    for choice in product(*(tuple(sorted(domain)) for domain in domains)):
        selected = set(choice)
        if _band_slots_supported(selected, spec.get("lte band", ()), "lte band", support) and _band_slots_supported(
            selected, spec.get("nr band", ()), "nr band", support
        ):
            return True
    return False


def _ru_band_covers(testcase: dict[str, tuple[Token, ...]], spec: dict[str, tuple[Token, ...]], support: SupportTable) -> bool:
    if len(spec.get("ru", ())) < len(testcase.get("ru", ())):
        return False
    return _ru_band_compatible(spec, support)


def _has_band_requirements(tokens_by_column: dict[str, tuple[Token, ...]]) -> bool:
    return bool(tokens_by_column.get("lte band", ()) or tokens_by_column.get("nr band", ()))


def _ru_domains(tokens: tuple[Token, ...], support: SupportTable) -> list[set[str]]:
    domains: list[set[str]] = []
    for token in tokens:
        if token.has_any():
            domains.append(set(support.ru_order))
        else:
            domains.append({alternative.casefold() for alternative in token.alternatives if alternative.casefold() in support.ru_display})
    return domains


def _band_domains(tokens: tuple[Token, ...], column: str, support: SupportTable) -> list[set[str]]:
    display = support.lte_display if column == "lte band" else support.nr_display
    domains: list[set[str]] = []
    for token in tokens:
        if _is_relation_token(token):
            continue
        if token.has_any():
            domains.append(set(display))
        else:
            domains.append({alternative.casefold() for alternative in token.alternatives if alternative.casefold() in display})
    return domains


def _band_slots_supported(selected_rus: set[str], tokens: tuple[Token, ...], column: str, support: SupportTable) -> bool:
    support_by_ru = support.lte_by_ru if column == "lte band" else support.nr_by_ru
    supported = set().union(*(set(support_by_ru.get(ru, ())) for ru in selected_rus))
    for domain in _band_domains(tokens, column, support):
        if domain and not domain & supported:
            return False
    return True
