"""CSV rendering for solved specs."""

from __future__ import annotations

import csv
from pathlib import Path

from .coverage import equipment_count
from .indexing import CoverageIndex, IndexedCoverage
from .models import Candidate, ParsedCsv, Solution, SolveOptions, SupportTable, Token
from .parsing import render_tokens


def expanded_spec(spec: dict[str, tuple[Token, ...]], support: SupportTable) -> dict[str, tuple[Token, ...]]:
    expanded = dict(spec)
    expanded_ru_tokens = _expand_ru_tokens(spec.get("ru", ()), spec, support)
    selected_rus = _selected_ru_domain(expanded_ru_tokens, support)
    expanded["ru"] = expanded_ru_tokens
    expanded["lte band"] = tuple(_expand_band_token(token, selected_rus, support.lte_by_ru, support.lte_display) for token in spec.get("lte band", ()))
    expanded["nr band"] = tuple(_expand_band_token(token, selected_rus, support.nr_by_ru, support.nr_display) for token in spec.get("nr band", ()))
    return expanded


def write_solution_csv(path: Path, parsed: ParsedCsv, support: SupportTable, solution: Solution, options: SolveOptions) -> None:
    output_requirement_columns = tuple(column for column in parsed.columns if column != "tc_id")
    coverage_index = CoverageIndex.build(parsed, support, options)
    rows = []
    for candidate in solution.candidates:
        spec = expanded_spec(candidate.spec, support)
        coverage = coverage_index.coverage_for_spec(spec)
        rows.append((candidate, spec, coverage))

    assigned_by_row = _assign_expanded_rows(parsed, rows, coverage_index)
    _validate_expanded_solution(parsed, rows, assigned_by_row)

    rows.sort(
        key=lambda item: (
            equipment_count(item[1]),
            -len(item[2].row_indexes),
            min(item[2].row_indexes) if item[2].row_indexes else len(parsed.rows),
            _rendered_signature(item[1], output_requirement_columns),
        )
    )

    fieldnames = ["spec_id", "covered_tc_ids", "covered_count", "equipment_count", "solve_status"]
    if options.auto_assign:
        fieldnames += ["assigned_tc_ids", "assigned_count"]
    fieldnames += list(output_requirement_columns)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for spec_index, item in enumerate(rows, start=1):
            _candidate, spec, coverage = item
            assigned = assigned_by_row[id(item)]
            row = {
                "spec_id": f"spec_{spec_index}",
                "covered_tc_ids": _join_tc_ids(parsed, coverage.row_indexes),
                "covered_count": str(len(coverage.row_indexes)),
                "equipment_count": str(equipment_count(spec)),
                "solve_status": solution.status,
            }
            if options.auto_assign:
                row["assigned_tc_ids"] = _join_tc_ids(parsed, assigned)
                row["assigned_count"] = str(len(assigned))
            for column in output_requirement_columns:
                if options.ignore_optional_columns and column not in coverage_index.columns:
                    row[column] = ""
                else:
                    row[column] = render_tokens(spec.get(column, ()))
            writer.writerow(row)


def _assign_expanded_rows(
    parsed: ParsedCsv,
    rows: list[tuple[Candidate, dict[str, tuple[Token, ...]], IndexedCoverage]],
    coverage_index: CoverageIndex,
) -> dict[int, tuple[int, ...]]:
    assigned: dict[int, list[int]] = {id(item): [] for item in rows}
    for testcase_index, row in enumerate(parsed.rows):
        group_index = coverage_index.row_to_group[testcase_index]
        choices = []
        for item in rows:
            _candidate, spec, coverage = item
            if not coverage.group_mask & (1 << group_index):
                continue
            choices.append(
                (
                    coverage.excess_by_group.get(group_index, 0),
                    equipment_count(spec),
                    _rendered_signature(spec, coverage_index.columns),
                    item,
                )
            )
        if choices:
            item = min(choices, key=lambda choice: choice[:3])[3]
            assigned[id(item)].append(testcase_index)
    return {key: tuple(value) for key, value in assigned.items()}


def _validate_expanded_solution(
    parsed: ParsedCsv,
    rows: list[tuple[Candidate, dict[str, tuple[Token, ...]], IndexedCoverage]],
    assigned_by_row: dict[int, tuple[int, ...]],
) -> None:
    covered = set()
    for _candidate, _spec, coverage in rows:
        covered.update(coverage.row_indexes)
    expected = set(range(len(parsed.rows)))
    if covered != expected:
        missing = sorted(expected - covered)
        raise ValueError(f"expanded solution does not cover testcase indexes: {missing}")

    assignment_counts = [0 for _ in parsed.rows]
    for assigned in assigned_by_row.values():
        for testcase_index in assigned:
            assignment_counts[testcase_index] += 1
    bad_indexes = [index for index, count in enumerate(assignment_counts) if count != 1]
    if bad_indexes:
        raise ValueError(f"expanded solution does not assign testcase indexes exactly once: {bad_indexes}")


def _selected_ru_domain(tokens: tuple[Token, ...], support: SupportTable) -> set[str]:
    selected: set[str] = set()
    for token in tokens:
        if token.has_any():
            selected.update(support.ru_order)
        else:
            selected.update(alternative.casefold() for alternative in token.alternatives if alternative.casefold() in support.ru_display)
    return selected


def _expand_ru_tokens(tokens: tuple[Token, ...], spec: dict[str, tuple[Token, ...]], support: SupportTable) -> tuple[Token, ...]:
    domains = [_ru_token_domain(token, support) for token in tokens]
    expanded: list[Token] = []
    for index, domain in enumerate(domains):
        compatible = [
            ru
            for ru in support.ru_order
            if ru in domain and _ru_choice_can_satisfy(index, ru, domains, spec, support)
        ]
        expanded.append(Token(tuple(support.ru_display[ru] for ru in compatible)))
    return tuple(expanded)


def _ru_token_domain(token: Token, support: SupportTable) -> set[str]:
    if token.has_any():
        return set(support.ru_order)
    return {alternative.casefold() for alternative in token.alternatives if alternative.casefold() in support.ru_display}


def _ru_choice_can_satisfy(
    slot_index: int,
    ru: str,
    domains: list[set[str]],
    spec: dict[str, tuple[Token, ...]],
    support: SupportTable,
) -> bool:
    possible = {ru}
    for index, domain in enumerate(domains):
        if index != slot_index:
            possible.update(domain)
    return _band_slots_satisfied(possible, spec.get("lte band", ()), support.lte_by_ru, support.lte_display) and _band_slots_satisfied(
        possible, spec.get("nr band", ()), support.nr_by_ru, support.nr_display
    )


def _band_slots_satisfied(
    selected_rus: set[str],
    tokens: tuple[Token, ...],
    support_by_ru: dict[str, tuple[str, ...]],
    display: dict[str, str],
) -> bool:
    supported = set().union(*(set(support_by_ru.get(ru, ())) for ru in selected_rus))
    for token in tokens:
        if any(alternative.casefold() in {"intra", "inter"} for alternative in token.alternatives):
            continue
        if token.has_any():
            if not supported:
                return False
            continue
        values = {alternative.casefold() for alternative in token.alternatives if alternative.casefold() in display}
        if values and not values & supported:
            return False
    return True


def _expand_band_token(token: Token, selected_rus: set[str], support_by_ru: dict[str, tuple[str, ...]], display: dict[str, str]) -> Token:
    if any(alternative.casefold() in {"intra", "inter"} for alternative in token.alternatives):
        return token
    if not token.has_any():
        return Token(tuple(display.get(alternative.casefold(), alternative) for alternative in token.alternatives))
    seen: set[str] = set()
    for ru in selected_rus:
        for band in support_by_ru.get(ru, ()):
            seen.add(band)
    return Token(tuple(display[band] for band in display if band in seen))


def _covered_indexes(parsed: ParsedCsv, spec: dict[str, tuple[Token, ...]], columns: tuple[str, ...], support: SupportTable, options: SolveOptions) -> tuple[int, ...]:
    covered: list[int] = []
    for index, row in enumerate(parsed.rows):
        if coverage_excess(row.tokens, spec, columns, support, options) is not None:
            covered.append(index)
    return tuple(covered)


def _join_tc_ids(parsed: ParsedCsv, indexes: tuple[int, ...]) -> str:
    return " + ".join(parsed.rows[index].raw["tc_id"].strip() for index in indexes)


def _rendered_signature(spec: dict[str, tuple[Token, ...]], columns: tuple[str, ...]) -> str:
    return "|".join(f"{column}={render_tokens(spec.get(column, ())) }" for column in columns)
