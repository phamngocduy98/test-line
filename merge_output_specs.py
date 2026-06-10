#!/usr/bin/env python3
"""Build combined second-pass specs from compatible solver output groups."""

from __future__ import annotations

import argparse
import csv
import time
from dataclasses import dataclass
from pathlib import Path

from solve_test_lines import (
    DU_COLUMNS,
    RU_COLUMN,
    UE_COLUMN,
    TestCase,
    alternatives,
    coverage_delta,
    covers_column,
    equipment_count,
    is_any,
    load_cases,
    load_ru_band_support,
    merge_column,
    numeric_equipment,
    parse_cell,
    render_cell,
    split_band_tokens,
    spec_has_compatible_ru_bands,
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
        description="Build combined second-pass specs from compatible solver output."
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
        "--max-ue",
        type=int,
        default=10,
        help="Maximum UE capacity in a resulting spec. Default: 10.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log every merge attempt and the first failed condition.",
    )
    return parser.parse_args()


def parse_id_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(" + ") if item.strip()]


def ru_count(spec: dict[str, tuple[str, ...]]) -> int:
    return len(spec.get(RU_COLUMN, ()))


def du_count(spec: dict[str, tuple[str, ...]]) -> int:
    return sum(numeric_equipment(spec.get(column, ())) for column in DU_COLUMNS)


def ue_count(spec: dict[str, tuple[str, ...]]) -> int:
    return numeric_equipment(spec.get(UE_COLUMN, ()))


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


def relation_failure(
    spec: dict[str, tuple[str, ...]],
    support,
) -> str | None:
    ru_tokens = spec.get(RU_COLUMN, ())
    if ru_tokens:
        selected_rus = {
            value
            for token in ru_tokens
            if not is_any(token)
            for value in alternatives(token)
        }
    else:
        selected_rus = set(support.ru_names)

    for column, support_by_ru in (
        ("lte band", support.lte_by_ru),
        ("nr band", support.nr_by_ru),
    ):
        if column not in spec:
            continue
        _, relations, _ = split_band_tokens(spec[column])
        if not relations:
            continue
        supported_bands = set().union(
            *(support_by_ru.get(ru, frozenset()) for ru in selected_rus)
        )
        if "intra" in relations and not supported_bands:
            return f"relation column={column!r} relation='intra'"
        if "inter" in relations and len(supported_bands) < 2:
            return f"relation column={column!r} relation='inter'"
    return None


def merge_attempt(
    left: SpecGroup,
    right: SpecGroup,
    requirement_columns: list[str],
    support,
    max_ru: int,
    max_du: int,
    max_ue: int,
) -> tuple[dict[str, tuple[str, ...]] | None, str]:
    candidate: dict[str, tuple[str, ...]] = {}
    for column in requirement_columns:
        merged = merge_column(column, (left.spec[column], right.spec[column]))
        if merged is None:
            return None, f"merge_conflict column={column!r}"
        candidate[column] = merged

    candidate_du = du_count(candidate)
    if candidate_du > max_du:
        return None, f"max_du actual={candidate_du} limit={max_du}"

    candidate_ru = ru_count(candidate)
    if candidate_ru > max_ru:
        return None, f"max_ru actual={candidate_ru} limit={max_ru}"

    candidate_ue = ue_count(candidate)
    if candidate_ue > max_ue:
        return None, f"max_ue actual={candidate_ue} limit={max_ue}"

    if not spec_has_compatible_ru_bands(candidate, support):
        return None, "ru_band_compatibility"

    failed_relation = relation_failure(candidate, support)
    if failed_relation is not None:
        return None, failed_relation

    for group in (left, right):
        requirement = group_as_requirement(group, requirement_columns)
        matches, _ = coverage_delta(
            requirement_columns,
            candidate,
            requirement,
            enforce_delta=False,
            support=support,
        )
        if not matches:
            return None, f"post_merge_coverage source=spec_{group.original_order + 1}"
    return candidate, "compatible"


def assigned_testcase_failure(
    candidate: dict[str, tuple[str, ...]],
    assigned_indices: list[int],
    requirement_columns: list[str],
    cases: list[TestCase],
    support,
) -> str | None:
    for index in assigned_indices:
        case = cases[index]
        matches, _ = coverage_delta(
            requirement_columns,
            candidate,
            case,
            enforce_delta=False,
            support=support,
        )
        if matches:
            continue
        for column in requirement_columns:
            column_matches, _ = covers_column(
                column,
                candidate[column],
                case.tokens[column],
                enforce_delta=False,
            )
            if not column_matches:
                return (
                    f"assigned_testcase_coverage tc_id={case.tc_id} "
                    f"column={column!r} "
                    f"candidate={render_cell(candidate[column])!r} "
                    f"requirement={render_cell(case.tokens[column])!r}"
                )
        return f"assigned_testcase_coverage tc_id={case.tc_id}"
    return None


def merge_small_groups(
    groups: list[SpecGroup],
    requirement_columns: list[str],
    cases: list[TestCase],
    support,
    max_ru: int,
    max_du: int,
    max_ue: int = 10,
    verbose: bool = False,
) -> tuple[list[SpecGroup], int]:
    active = list(groups)
    merged_count = 0

    while True:
        merged = False
        ordered = sorted(active, key=lambda group: group.original_order)
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1:]:
                left_name = f"spec_{left.original_order + 1}"
                right_name = f"spec_{right.original_order + 1}"
                if verbose:
                    print(f"TRY left={left_name} right={right_name}")
                candidate, reason = merge_attempt(
                    left,
                    right,
                    requirement_columns,
                    support,
                    max_ru,
                    max_du,
                    max_ue,
                )
                if candidate is None:
                    if verbose:
                        print(
                            f"FAIL left={left_name} right={right_name} "
                            f"condition={reason}"
                        )
                    continue

                assigned_indices = sorted(
                    set(left.assigned_indices + right.assigned_indices)
                )
                failure = assigned_testcase_failure(
                    candidate,
                    assigned_indices,
                    requirement_columns,
                    cases,
                    support,
                )
                if failure is not None:
                    if verbose:
                        print(
                            f"FAIL left={left_name} right={right_name} "
                            f"condition={failure}"
                        )
                    continue

                left.spec = candidate
                left.assigned_indices = assigned_indices
                active.remove(right)
                merged_count += 1
                if verbose:
                    print(
                        f"MERGE target={left_name} source={right_name} "
                        f"condition={reason}"
                    )
                    assigned_ids = " + ".join(
                        cases[index].tc_id for index in assigned_indices
                    )
                    rendered_spec = " ".join(
                        f"{column}={render_cell(candidate[column])!r}"
                        for column in requirement_columns
                    )
                    print(
                        f"NEW_SPEC target={left_name} "
                        f"assigned_tc_ids={assigned_ids} {rendered_spec}"
                    )
                merged = True
                break
            if merged:
                break
        if not merged:
            break

    return active, merged_count


def validate_merged_groups(
    groups: list[SpecGroup],
    requirement_columns: list[str],
    cases: list[TestCase],
    support,
) -> list[tuple[SpecGroup, TestCase]]:
    failures: list[tuple[SpecGroup, TestCase]] = []
    for group in groups:
        for index in group.assigned_indices:
            case = cases[index]
            matches, _ = coverage_delta(
                requirement_columns,
                group.spec,
                case,
                enforce_delta=False,
                support=support,
            )
            if not matches:
                failures.append((group, case))
    return failures


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
                    enforce_delta=False,
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
    for name in ("max_ru", "max_du", "max_ue"):
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
        args.max_ue,
        verbose=args.verbose,
    )
    validation_failures = validate_merged_groups(
        merged_groups,
        requirement_columns,
        cases,
        support,
    )

    print(f"merged_requirement_check={'PASS' if not validation_failures else 'FAIL'}")
    print(f"unsatisfied_testcases={len(validation_failures)}")
    for group, case in validation_failures:
        print(
            f"unsatisfied_tc_id={case.tc_id} "
            f"target_spec=spec_{group.original_order + 1}"
        )
    if validation_failures:
        raise SystemExit("merged specs do not satisfy all assigned testcase requirements")

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
