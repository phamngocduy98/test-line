#!/usr/bin/env python3
"""Deterministic performance benchmark for the test-line solver."""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_line_solver.candidates import generate_candidates
from test_line_solver.models import SolveOptions
from test_line_solver.output import write_solution_csv
from test_line_solver.parsing import read_ru_band_csv, read_testcase_csv
from test_line_solver.support import build_support_table
from test_line_solver.validation import validate_testcases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark deterministic solver phases.")
    parser.add_argument("--rows", nargs="+", type=int, default=[30, 40, 50, 3000])
    parser.add_argument("--ru-count", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--max-merge-width", type=int, default=55)
    parser.add_argument("--max-candidates", type=int, default=20000)
    parser.add_argument("--max-candidates-per-bucket", type=int, default=250)
    parser.add_argument("--solver", choices=("auto", "stdlib", "ortools"), default="auto")
    parser.add_argument("--solver-threads", type=int)
    parser.add_argument("--scenario", choices=("easy", "hard"), default="easy")
    return parser.parse_args()


def support_csv(ru_count: int) -> str:
    rows = ["ru,lte_band,nr_band"]
    for index in range(1, ru_count + 1):
        next_index = 1 + (index % ru_count)
        rows.append(f"RU{index},b{index} + b{next_index},n{index}")
    return "\n".join(rows) + "\n"


def input_csv(row_count: int, ru_count: int) -> str:
    rows = ["tc_id,ru,lte band,nr band,cc location,ue"]
    for index in range(row_count):
        ru_index = 1 + (index % ru_count)
        next_ru_index = 1 + (ru_index % ru_count)
        band_index = ru_index
        next_band_index = 1 + (band_index % ru_count)
        cc_location = "A" if index % 2 == 0 else "B"
        ue = 1 + (index % 3)
        rows.append(
            f"T{index + 1},RU{ru_index}/RU{next_ru_index},b{band_index}/b{next_band_index},n{ru_index},{cc_location},{ue}"
        )
    return "\n".join(rows) + "\n"


def hard_input_csv(row_count: int, ru_count: int) -> str:
    rows = ["tc_id,ru,lte band,nr band,cc location,ca type,rf condition,ue"]
    locations = ("A", "B", "C", "D")
    ca_types = ("intra cc", "inter cc", "dual cc")
    rf_conditions = ("rf-1", "rf-2", "rf-3", "rf-4", "rf-5")
    for index in range(row_count):
        ru_index = 1 + (index % ru_count)
        alt_ru = 1 + ((index * 7 + 3) % ru_count)
        band_index = ru_index
        next_band = 1 + (band_index % ru_count)
        nr_index = ru_index
        location = locations[(index + index // 11) % len(locations)]
        ca_type = ca_types[(index // 5 + index) % len(ca_types)]
        rf_condition = rf_conditions[(index * 3 + index // 7) % len(rf_conditions)]
        ue = 1 + ((index + index // 13) % 4)
        rows.append(
            f"H{index + 1},RU{ru_index}/RU{alt_ru},b{band_index}/b{next_band},n{nr_index},{location},{ca_type},{rf_condition},{ue}"
        )
    return "\n".join(rows) + "\n"


def benchmark(row_count: int, args: argparse.Namespace) -> dict[str, float | int | str]:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        input_path = directory / "input.csv"
        support_path = directory / "ru-band.csv"
        output_path = directory / "output.csv"
        input_text = hard_input_csv(row_count, args.ru_count) if args.scenario == "hard" else input_csv(row_count, args.ru_count)
        input_path.write_text(input_text, encoding="utf-8")
        support_path.write_text(support_csv(args.ru_count), encoding="utf-8")

        options = SolveOptions(
            timeout_seconds=args.timeout,
            max_candidates=args.max_candidates,
            max_candidates_per_bucket=args.max_candidates_per_bucket,
            max_merge_width=args.max_merge_width,
            solver=args.solver,
            solver_threads=args.solver_threads,
        )

        start = time.perf_counter()
        parsed = read_testcase_csv(input_path, require_ru=True)
        parsed_at = time.perf_counter()
        support = build_support_table(read_ru_band_csv(support_path))
        support_at = time.perf_counter()
        validate_testcases(parsed, support, final_solver=True)
        validated_at = time.perf_counter()
        candidates = generate_candidates(parsed, support, options)
        candidates_at = time.perf_counter()
        solution = optimize_candidates(candidates, len(parsed.rows), options)
        optimized_at = time.perf_counter()
        write_solution_csv(output_path, parsed, support, solution, options)
        output_at = time.perf_counter()

    return {
        "rows": row_count,
        "candidates": len(candidates),
        "edges": coverage_edges(candidates),
        "selected": len(solution.candidates),
        "status": solution.status,
        "parse": parsed_at - start,
        "support": support_at - parsed_at,
        "validate": validated_at - support_at,
        "candidate": candidates_at - validated_at,
        "optimize": optimized_at - candidates_at,
        "output": output_at - optimized_at,
        "total": output_at - start,
    }


def coverage_edges(candidates) -> int:
    total = 0
    for candidate in candidates:
        if candidate.group_coverage_mask:
            total += candidate.group_coverage_mask.bit_count()
        else:
            total += len(candidate.coverage)
    return total


def optimize_candidates(candidates, testcase_count: int, options: SolveOptions):
    if options.solver == "stdlib":
        from test_line_solver.optimizer import optimize

        return optimize(candidates, testcase_count, options.timeout_seconds)
    try:
        from test_line_solver.ortools_optimizer import OrtoolsUnavailableError, optimize

        return optimize(candidates, testcase_count, options.timeout_seconds, solver_threads=options.solver_threads)
    except OrtoolsUnavailableError:
        if options.solver == "ortools":
            raise
        from test_line_solver.optimizer import optimize

        return optimize(candidates, testcase_count, options.timeout_seconds)


def main() -> int:
    args = parse_args()
    print("rows,candidates,edges,selected,status,parse,support,validate,candidate,optimize,output,total")
    for row_count in args.rows:
        result = benchmark(row_count, args)
        print(
            "{rows},{candidates},{edges},{selected},{status},{parse:.3f},{support:.3f},{validate:.3f},{candidate:.3f},{optimize:.3f},{output:.3f},{total:.3f}".format(
                **result
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
