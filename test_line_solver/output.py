"""CSV rendering for solved specs."""

from __future__ import annotations

import csv
from pathlib import Path

from .coverage import active_requirement_columns, coverage_excess, equipment_count
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
    columns = active_requirement_columns(parsed.columns, options)
    output_requirement_columns = tuple(column for column in parsed.columns if column != "tc_id")
    rows = []
    for candidate in solution.candidates:
        spec = expanded_spec(candidate.spec, support)
        covered = _covered_indexes(parsed, spec, columns, support, options)
        rows.append((candidate, spec, covered))

    assigned_by_row = _assign_expanded_rows(parsed, rows, columns, support, options)

    rows.sort(
        key=lambda item: (
            equipment_count(item[1]),
            -len(item[2]),
            min(item[2]) if item[2] else len(parsed.rows),
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
            _candidate, spec, covered = item
            assigned = assigned_by_row[id(item)]
            row = {
                "spec_id": f"spec_{spec_index}",
                "covered_tc_ids": _join_tc_ids(parsed, covered),
                "covered_count": str(len(covered)),
                "equipment_count": str(equipment_count(spec)),
                "solve_status": solution.status,
            }
            if options.auto_assign:
                row["assigned_tc_ids"] = _join_tc_ids(parsed, assigned)
                row["assigned_count"] = str(len(assigned))
            for column in output_requirement_columns:
                if options.ignore_optional_columns and column not in columns:
                    row[column] = ""
                else:
                    row[column] = render_tokens(spec.get(column, ()))
            writer.writerow(row)


def _assign_expanded_rows(
    parsed: ParsedCsv,
    rows: list[tuple[Candidate, dict[str, tuple[Token, ...]], tuple[int, ...]]],
    columns: tuple[str, ...],
    support: SupportTable,
    options: SolveOptions,
) -> dict[int, tuple[int, ...]]:
    assigned: dict[int, list[int]] = {id(item): [] for item in rows}
    for testcase_index, row in enumerate(parsed.rows):
        choices = []
        for item in rows:
            _candidate, spec, covered = item
            if testcase_index not in covered:
                continue
            result = coverage_excess(row.tokens, spec, columns, support, options)
            if result is not None:
                choices.append((result.excess, equipment_count(spec), _rendered_signature(spec, columns), item))
        if choices:
            item = min(choices, key=lambda choice: choice[:3])[3]
            assigned[id(item)].append(testcase_index)
    return {key: tuple(value) for key, value in assigned.items()}


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
