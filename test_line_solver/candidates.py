"""Deterministic candidate generation."""

from __future__ import annotations

from .coverage import active_requirement_columns, coverage_excess, equipment_count
from .constants import NUMERIC_COLUMNS
from .merge import exact_spec, merge_specs, spec_signature
from .models import Candidate, ParsedCsv, SolveOptions, SupportTable, Token


def generate_candidates(parsed: ParsedCsv, support: SupportTable, options: SolveOptions) -> tuple[Candidate, ...]:
    columns = active_requirement_columns(parsed.columns, options)
    exact_specs = [exact_spec(row.tokens, columns) for row in parsed.rows]
    max_slots_by_column = _max_slots_by_column(parsed, columns)
    candidates: list[Candidate] = []
    seen: set[str] = set()

    for index, spec in enumerate(exact_specs):
        _add_candidate(candidates, seen, spec, (index,), parsed, columns, support, options)

    if len(candidates) >= options.max_candidates:
        return tuple(candidates)

    generated_by_bucket: dict[str, int] = {}
    for start, spec in enumerate(exact_specs):
        limit = min(len(exact_specs), start + 1 + options.max_merge_width)
        current = spec
        for end in range(start + 1, limit):
            bucket = _bucket_key(parsed.rows[start].tokens, parsed.rows[end].tokens, columns)
            if generated_by_bucket.get(bucket, 0) >= options.max_candidates_per_bucket:
                continue
            current = merge_specs(current, exact_specs[end], columns)
            if _too_broad_for_any_testcase(current, columns, max_slots_by_column, options.max_extra_slots):
                break
            added = _add_candidate(candidates, seen, current, tuple(range(start, end + 1)), parsed, columns, support, options)
            if added:
                generated_by_bucket[bucket] = generated_by_bucket.get(bucket, 0) + 1
            if len(candidates) >= options.max_candidates:
                return tuple(candidates)
    return tuple(candidates)


def _max_slots_by_column(parsed: ParsedCsv, columns: tuple[str, ...]) -> dict[str, int]:
    result: dict[str, int] = {}
    for column in columns:
        if column in NUMERIC_COLUMNS:
            continue
        result[column] = max((len(row.tokens.get(column, ())) for row in parsed.rows), default=0)
    return result


def _too_broad_for_any_testcase(
    spec: dict[str, tuple[Token, ...]],
    columns: tuple[str, ...],
    max_slots_by_column: dict[str, int],
    max_extra_slots: int,
) -> bool:
    for column in columns:
        if column in NUMERIC_COLUMNS:
            continue
        if len(spec.get(column, ())) > max_slots_by_column.get(column, 0) + max_extra_slots:
            return True
    return False


def _add_candidate(
    candidates: list[Candidate],
    seen: set[str],
    spec: dict[str, tuple[Token, ...]],
    source_indexes: tuple[int, ...],
    parsed: ParsedCsv,
    columns: tuple[str, ...],
    support: SupportTable,
    options: SolveOptions,
) -> bool:
    signature = spec_signature(spec, columns)
    if signature in seen:
        return False
    coverage: set[int] = set()
    excess: dict[int, int] = {}
    for index, row in enumerate(parsed.rows):
        result = coverage_excess(row.tokens, spec, columns, support, options)
        if result is not None:
            coverage.add(index)
            excess[index] = result.excess
    if not coverage:
        return False
    seen.add(signature)
    candidates.append(
        Candidate(
            signature=signature,
            spec=spec,
            source_indexes=source_indexes,
            equipment_count=equipment_count(spec),
            coverage=frozenset(coverage),
            assignment_excess=excess,
        )
    )
    return True


def _bucket_key(
    left: dict[str, tuple[Token, ...]],
    right: dict[str, tuple[Token, ...]],
    columns: tuple[str, ...],
) -> str:
    parts: list[str] = []
    for column in columns:
        if column in {"ru", "lte band", "nr band", "cc location", "ca type"}:
            left_len = len(left.get(column, ()))
            right_len = len(right.get(column, ()))
            parts.append(f"{column}:{min(left_len, right_len)}-{max(left_len, right_len)}")
    return "|".join(parts)
