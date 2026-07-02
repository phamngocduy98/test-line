"""Command-line interface for the solver."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Sequence

from .constants import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_CANDIDATES_PER_BUCKET,
    DEFAULT_MAX_EXTRA_ALTERNATIVES,
    DEFAULT_MAX_EXTRA_SLOTS,
    DEFAULT_MAX_MERGE_WIDTH,
    DEFAULT_MAX_NUMERIC_OVERAGE_RATIO,
    DEFAULT_MAX_NUMERIC_OVERAGE_UNITS,
    DEFAULT_MIN_ASSIGNED_CASES_PER_SPEC,
    DEFAULT_TIMEOUT_SECONDS,
)
from .errors import InputError
from .models import ParsedCsv, SolveOptions
from .parsing import parsed_payload_to_json, read_ru_band_csv, read_testcase_csv
from .support import build_support_table
from .validation import validate_testcases


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Solve reusable telecom test-line specs.")
    parser.add_argument("--input", default="input.csv", help="Path to testcase input CSV. Default: input.csv")
    parser.add_argument("--output", default="output_specs.csv", help="Path to output CSV. Default: output_specs.csv")
    parser.add_argument(
        "--ru-band",
        "--ru-band-support",
        dest="ru_band",
        default="ru-band.csv",
        help="Path to RU-band support CSV. Default: ru-band.csv",
    )
    parser.add_argument("--parse-only", action="store_true", help="Print parsed JSON and exit without solving")
    parser.add_argument("--limit-rows", type=int, help="Only process the first N testcase input rows")
    parser.add_argument("--auto-assign", action="store_true", help="Write assigned testcase columns in output")
    parser.add_argument("--ignore-optional-columns", action="store_true", help="Ignore optional technology and UE capability columns")
    parser.add_argument(
        "--ignore-tech-and-ue-capa",
        dest="ignore_optional_columns",
        action="store_true",
        help="Legacy alias for --ignore-optional-columns",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--solver", choices=("auto", "stdlib", "ortools"), default="auto")
    parser.add_argument("--solver-threads", type=int, help="Worker threads for solver backends that support parallel search")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument("--max-candidates-per-bucket", type=int, default=DEFAULT_MAX_CANDIDATES_PER_BUCKET)
    parser.add_argument("--max-merge-width", type=int, default=DEFAULT_MAX_MERGE_WIDTH)
    parser.add_argument("--max-extra-slots", type=int, default=DEFAULT_MAX_EXTRA_SLOTS)
    parser.add_argument("--max-extra-alternatives", type=int, default=DEFAULT_MAX_EXTRA_ALTERNATIVES)
    parser.add_argument("--max-numeric-overage-ratio", type=float, default=DEFAULT_MAX_NUMERIC_OVERAGE_RATIO)
    parser.add_argument("--max-numeric-overage-units", type=int, default=DEFAULT_MAX_NUMERIC_OVERAGE_UNITS)
    parser.add_argument("--reject-spec-side-wildcard", action="append", default=[])
    parser.add_argument(
        "--min-assigned-cases-per-spec",
        type=int,
        default=DEFAULT_MIN_ASSIGNED_CASES_PER_SPEC,
        help="Treat selected specs with fewer than N assigned testcases as low-use. Use 0 to disable.",
    )
    return parser.parse_args(argv)


def limited_testcases(parsed: ParsedCsv, limit_rows: int | None) -> ParsedCsv:
    if limit_rows is None:
        return parsed
    if limit_rows < 1:
        raise InputError("--limit-rows must be a positive integer")
    return ParsedCsv(path=parsed.path, columns=parsed.columns, rows=parsed.rows[:limit_rows])


def progress(message: str) -> None:
    print(message, file=sys.stderr)


def options_from_args(args: argparse.Namespace) -> SolveOptions:
    if args.solver_threads is not None and args.solver_threads < 1:
        raise InputError("--solver-threads must be a positive integer")
    if args.min_assigned_cases_per_spec < 0:
        raise InputError("--min-assigned-cases-per-spec must be zero or a positive integer")
    return SolveOptions(
        ignore_optional_columns=args.ignore_optional_columns,
        auto_assign=args.auto_assign,
        timeout_seconds=args.timeout,
        solver=args.solver,
        solver_threads=args.solver_threads,
        max_candidates=args.max_candidates,
        max_candidates_per_bucket=args.max_candidates_per_bucket,
        max_merge_width=args.max_merge_width,
        max_extra_slots=args.max_extra_slots,
        max_extra_alternatives=args.max_extra_alternatives,
        max_numeric_overage_ratio=args.max_numeric_overage_ratio,
        max_numeric_overage_units=args.max_numeric_overage_units,
        reject_spec_side_wildcard=tuple(args.reject_spec_side_wildcard),
        min_assigned_cases_per_spec=args.min_assigned_cases_per_spec,
    )


def run(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    start = time.perf_counter()
    try:
        progress(f"Reading testcase CSV: {args.input}")
        testcase_csv = read_testcase_csv(Path(args.input), require_ru=not args.parse_only)
        original_rows = len(testcase_csv.rows)
        testcase_csv = limited_testcases(testcase_csv, args.limit_rows)
        if args.limit_rows is not None:
            progress(f"Limited testcase rows: {len(testcase_csv.rows)} of {original_rows}")
        else:
            progress(f"Parsed testcase rows: {len(testcase_csv.rows)}")
        progress(f"Reading RU-band support CSV: {args.ru_band}")
        ru_band_csv = read_ru_band_csv(Path(args.ru_band))
        if args.parse_only:
            progress("Writing parsed JSON")
            print(parsed_payload_to_json(testcase_csv, ru_band_csv))
            progress(f"Completed in {time.perf_counter() - start:.3f}s")
            return 0

        progress("Building RU-band support table")
        support = build_support_table(ru_band_csv)
        progress("Validating testcase compatibility")
        validate_testcases(testcase_csv, support, final_solver=True)

        from .solver import solve_to_csv

        progress("Solving selected test-line specs")
        solve_to_csv(testcase_csv, support, Path(args.output), options_from_args(args))
        progress(f"Wrote output CSV: {args.output}")
        progress(f"Completed in {time.perf_counter() - start:.3f}s")
        return 0
    except InputError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def main() -> int:
    return run()
