#!/usr/bin/env python3
"""Merge covered solve_test_lines.py specs into eligible target specs."""

from __future__ import annotations

import argparse
import csv
import time
from dataclasses import dataclass
from pathlib import Path

from solve_test_lines import (
    DU_COLUMNS,
    RU_COLUMN,
    TestCase,
    coverage_delta,
    equipment_count,
    load_cases,
    load_ru_band_support,
    numeric_equipment,
    parse_cell,
    render_cell,
)


METADATA_COLUMNS = [
    "spec_id",
    "assigned_tc_ids",
    "assigned_count",
    "covered_tc_ids",
    "covered_count",
    "equipment_count",
    "total_delta",
    "solve_status",
]


@dataclass
class SpecGroup:
    original_order: int
    assigned_indices: list[int]
    spec: dict[str, tuple[str, ...]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge covered solver output specs into eligible target specs."
    )
    parser.add_argument(
        "--input",
        default="output_specs.csv",
        help="First-pass solver output. Default: output_specs.csv",
    )
    parser.add_argument(
        "--testcases",
        default="input.csv",
        help="Original testcase CSV used by the solver. Default: input.csv",
    )
    parser.add_argument(
        "--ru-band-support",
        required=True,
        help="RU-band support CSV used by the solver.",
    )
    parser.add_argument(
        "--output",
        default="merged_output_specs.csv",
        help="Second-pass output path. Default: merged_output_specs.csv",
    )
    parser.add_argument(
        "--max-ru",
        type=int,
        default=3,
        help="Maximum RU slots in a resulting target spec. Default: 3.",
    )
    parser.add_argument(
        "--max-du",
        type=int,
        default=3,
        help="Maximum total enb+vdu+au+cu capacity in a resulting target spec. Default: 3.",
    )
    parser.add_argument(
        "--max-tc-per-spec",
        type=int,
        default=338,
        help="Maximum assigned testcases in a resulting spec. Default: 338.",
    )
    return parser.parse_args()


def parse_id_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(" + ") if item.strip()]


def ru_count(spec: dict[str, tuple[str, ...]]) -> int:
    return len(spec.get(RU_COLUMN, ()))


def du_count(spec: dict[str, tuple[str, ...]]) -> int:
    return sum(numeric_equipment(spec.get(column, ())) for column in DU_COLUMNS)


def load_groups(
    path: Path,
    cases: list[TestCase],
) -> tuple[list[str], list[str], list[SpecGroup]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path} has no header row")
        missing = set(METADATA_COLUMNS) - set(reader.fieldnames)
        if missing:
            raise SystemExit(
                f"{path} is missing solver output columns: {', '.join(sorted(missing))}"
            )
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    case_by_id = {case.tc_id: case for case in cases}
    if len(case_by_id) != len(cases):
        raise SystemExit("testcase tc_id values must be unique")

    requirement_columns = [
        column for column in fieldnames if column not in METADATA_COLUMNS
    ]
    active_columns = [
        column
        for column in requirement_columns
        if any((row.get(column) or "").strip() for row in rows)
    ]

    groups: list[SpecGroup] = []
    assigned_once: set[int] = set()
    for order, row in enumerate(rows):
        tc_ids = parse_id_list(row.get("assigned_tc_ids", ""))
        if not tc_ids:
            raise SystemExit(f"spec row {order + 2} has no assigned testcase IDs")
        unknown = [tc_id for tc_id in tc_ids if tc_id not in case_by_id]
        if unknown:
            raise SystemExit(
                f"spec row {order + 2} references unknown testcase IDs: "
                + ", ".join(unknown)
            )
        indices = [case_by_id[tc_id].index for tc_id in tc_ids]
        duplicate_indices = assigned_once.intersection(indices)
        if duplicate_indices:
            duplicate_ids = [cases[index].tc_id for index in sorted(duplicate_indices)]
            raise SystemExit(
                "testcases are assigned to multiple specs: " + ", ".join(duplicate_ids)
            )
        assigned_once.update(indices)
        groups.append(
            SpecGroup(
                original_order=order,
                assigned_indices=indices,
                spec={
                    column: parse_cell(row.get(column, ""))
                    for column in active_columns
                },
            )
        )

    expected = {case.index for case in cases}
    if assigned_once != expected:
        missing_ids = [cases[index].tc_id for index in sorted(expected - assigned_once)]
        raise SystemExit(
            "first-pass output does not assign every testcase; missing: "
            + ", ".join(missing_ids)
        )
    return fieldnames, active_columns, groups


def group_as_requirement(
    group: SpecGroup,
    requirement_columns: list[str],
) -> TestCase:
    return TestCase(
        index=-1,
        tc_id=f"spec_{group.original_order}",
        raw={column: render_cell(group.spec[column]) for column in requirement_columns},
        tokens=group.spec,
    )


def target_accepts_source(
    source: SpecGroup,
    target: SpecGroup,
    requirement_columns: list[str],
    cases: list[TestCase],
    support,
) -> bool:
    source_requirement = group_as_requirement(source, requirement_columns)
    source_matches, _ = coverage_delta(
        requirement_columns,
        target.spec,
        source_requirement,
        support=support,
    )
    if not source_matches:
        return False

    combined_indices = set(source.assigned_indices + target.assigned_indices)
    return all(
        coverage_delta(
            requirement_columns,
            target.spec,
            cases[index],
            support=support,
        )[0]
        for index in combined_indices
    )


def merge_small_groups(
    groups: list[SpecGroup],
    requirement_columns: list[str],
    cases: list[TestCase],
    support,
    max_ru: int,
    max_du: int,
    max_tc_per_spec: int,
) -> tuple[list[SpecGroup], int]:
    active = list(groups)
    merged_count = 0

    targets = sorted(
        (
            group
            for group in active
            if du_count(group.spec) <= max_du and ru_count(group.spec) <= max_ru
        ),
        key=lambda group: (
            -len(group.assigned_indices),
            -equipment_count(requirement_columns, group.spec),
            group.original_order,
        ),
    )
    for target in targets:
        if target not in active:
            continue
        sources = sorted(
            (group for group in active if group is not target),
            key=lambda group: (len(group.assigned_indices), group.original_order),
        )
        for source in sources:
            if (
                len(target.assigned_indices) + len(source.assigned_indices)
                > max_tc_per_spec
            ):
                continue
            if not target_accepts_source(
                source,
                target,
                requirement_columns,
                cases,
                support,
            ):
                continue
            target.assigned_indices = sorted(
                set(target.assigned_indices + source.assigned_indices)
            )
            active.remove(source)
            merged_count += 1

    return active, merged_count


def write_groups(
    path: Path,
    fieldnames: list[str],
    requirement_columns: list[str],
    groups: list[SpecGroup],
    cases: list[TestCase],
    support,
) -> None:
    output_requirement_columns = [
        column for column in fieldnames if column not in METADATA_COLUMNS
    ]
    sorted_groups = sorted(
        groups,
        key=lambda group: (
            equipment_count(requirement_columns, group.spec),
            -len(group.assigned_indices),
            min(group.assigned_indices),
        ),
    )

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for number, group in enumerate(sorted_groups, start=1):
            covered: list[int] = []
            delta_by_case: dict[int, int] = {}
            for case in cases:
                ok, delta = coverage_delta(
                    requirement_columns,
                    group.spec,
                    case,
                    support=support,
                )
                if ok:
                    covered.append(case.index)
                    delta_by_case[case.index] = delta

            assigned = sorted(group.assigned_indices)
            row = {
                "spec_id": f"spec_{number}",
                "assigned_tc_ids": " + ".join(cases[index].tc_id for index in assigned),
                "assigned_count": len(assigned),
                "covered_tc_ids": " + ".join(cases[index].tc_id for index in covered),
                "covered_count": len(covered),
                "equipment_count": equipment_count(
                    requirement_columns, group.spec
                ),
                "total_delta": sum(delta_by_case[index] for index in assigned),
                "solve_status": "SECOND_PASS",
            }
            for column in output_requirement_columns:
                row[column] = (
                    render_cell(group.spec[column])
                    if column in requirement_columns
                    else ""
                )
            writer.writerow(row)


def main() -> int:
    args = parse_args()
    if args.max_tc_per_spec <= 0:
        raise SystemExit("--max-tc-per-spec must be positive")
    for name in ("max_ru", "max_du"):
        if getattr(args, name) < 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be non-negative")

    started_at = time.monotonic()
    testcase_columns, cases = load_cases(Path(args.testcases))
    support = load_ru_band_support(Path(args.ru_band_support))
    fieldnames, requirement_columns, groups = load_groups(Path(args.input), cases)

    unknown_columns = set(requirement_columns) - set(testcase_columns)
    if unknown_columns:
        raise SystemExit(
            "solver output contains requirement columns absent from testcase input: "
            + ", ".join(sorted(unknown_columns))
        )

    merged_groups, merged_count = merge_small_groups(
        groups,
        requirement_columns,
        cases,
        support,
        args.max_ru,
        args.max_du,
        args.max_tc_per_spec,
    )
    write_groups(
        Path(args.output),
        fieldnames,
        requirement_columns,
        merged_groups,
        cases,
        support,
    )

    print(f"runtime_seconds={time.monotonic() - started_at:.2f}")
    print(f"input_specs={len(groups)}")
    print(f"merged_specs={merged_count}")
    print(f"output_specs={len(merged_groups)}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
