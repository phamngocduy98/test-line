"""Top-level solving orchestration."""

from __future__ import annotations

from pathlib import Path

from .candidates import generate_candidates
from .errors import InputError
from .models import ParsedCsv, SolveOptions, SupportTable
from .output import write_solution_csv


def solve_to_csv(parsed: ParsedCsv, support: SupportTable, output_path: Path, options: SolveOptions) -> None:
    candidates = generate_candidates(parsed, support, options)
    try:
        if options.solver == "stdlib":
            from .optimizer import optimize

            solution = optimize(candidates, len(parsed.rows), options.timeout_seconds)
        else:
            try:
                from .ortools_optimizer import OrtoolsUnavailableError, optimize

                solution = optimize(candidates, len(parsed.rows), options.timeout_seconds, solver_threads=options.solver_threads)
            except OrtoolsUnavailableError:
                if options.solver == "ortools":
                    raise
                from .optimizer import optimize

                solution = optimize(candidates, len(parsed.rows), options.timeout_seconds)
    except RuntimeError as exc:
        raise InputError(str(exc)) from exc
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    write_solution_csv(output_path, parsed, support, solution, options)
