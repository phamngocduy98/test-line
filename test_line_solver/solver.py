"""Top-level solving orchestration."""

from __future__ import annotations

from pathlib import Path

from .candidates import generate_candidates
from .errors import InputError
from .models import ParsedCsv, SolveOptions, SupportTable
from .optimizer import optimize
from .output import write_solution_csv


def solve_to_csv(parsed: ParsedCsv, support: SupportTable, output_path: Path, options: SolveOptions) -> None:
    if options.solver == "ortools":
        raise InputError("--solver ortools is not available in this standard-library build")
    candidates = generate_candidates(parsed, support, options)
    try:
        solution = optimize(candidates, len(parsed.rows), options.timeout_seconds)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    write_solution_csv(output_path, parsed, support, solution, options)

