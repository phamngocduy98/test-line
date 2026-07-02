"""CSV rendering for solved specs."""

from __future__ import annotations

import csv
from pathlib import Path

from .coverage import active_requirement_columns, equipment_count
from .evaluation import SolutionEvaluator
from .models import ParsedCsv, Solution, SolveOptions, SupportTable, Token
from .parsing import render_tokens


def write_solution_csv(path: Path, parsed: ParsedCsv, support: SupportTable, solution: Solution, options: SolveOptions) -> None:
    output_requirement_columns = tuple(column for column in parsed.columns if column != "tc_id")
    active_columns = set(active_requirement_columns(parsed.columns, options))
    evaluation = SolutionEvaluator(parsed, support, options).evaluate(solution.candidates)
    rows = list(evaluation.rows)

    rows.sort(
        key=lambda item: (
            equipment_count(item.evaluated.spec),
            -len(item.evaluated.coverage.row_indexes),
            min(item.evaluated.coverage.row_indexes) if item.evaluated.coverage.row_indexes else len(parsed.rows),
            _rendered_signature(item.evaluated.spec, output_requirement_columns),
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
            spec = item.evaluated.spec
            coverage = item.evaluated.coverage
            row = {
                "spec_id": f"spec_{spec_index}",
                "covered_tc_ids": _join_tc_ids(parsed, coverage.row_indexes),
                "covered_count": str(len(coverage.row_indexes)),
                "equipment_count": str(equipment_count(spec)),
                "solve_status": solution.status,
            }
            if options.auto_assign:
                row["assigned_tc_ids"] = _join_tc_ids(parsed, item.assigned_indexes)
                row["assigned_count"] = str(item.assigned_count)
            for column in output_requirement_columns:
                if options.ignore_optional_columns and column not in active_columns:
                    row[column] = ""
                else:
                    row[column] = _render_output_tokens(column, spec.get(column, ()), support)
            writer.writerow(row)


def _join_tc_ids(parsed: ParsedCsv, indexes: tuple[int, ...]) -> str:
    return " + ".join(parsed.rows[index].raw["tc_id"].strip() for index in indexes)


def _rendered_signature(spec: dict[str, tuple[Token, ...]], columns: tuple[str, ...]) -> str:
    return "|".join(f"{column}={render_tokens(spec.get(column, ())) }" for column in columns)


def _render_output_tokens(column: str, tokens: tuple[Token, ...], support: SupportTable) -> str:
    if column == "ru":
        return render_tokens(_compact_full_domain_tokens(tokens, support.ru_order))
    if column == "lte band":
        return render_tokens(_compact_full_domain_tokens(tokens, tuple(support.lte_display)))
    if column == "nr band":
        return render_tokens(_compact_full_domain_tokens(tokens, tuple(support.nr_display)))
    return render_tokens(tokens)


def _compact_full_domain_tokens(tokens: tuple[Token, ...], domain: tuple[str, ...]) -> tuple[Token, ...]:
    if not domain:
        return tokens
    domain_values = set(domain)
    compacted: list[Token] = []
    for token in tokens:
        token_values = {alternative.casefold() for alternative in token.alternatives}
        if token_values == domain_values:
            compacted.append(Token(("any",)))
        else:
            compacted.append(token)
    return tuple(compacted)
