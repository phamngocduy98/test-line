"""Top-level solving orchestration."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import sys
import time

from .candidates import candidate_from_spec, generate_candidates
from .errors import InputError
from .evaluation import SolutionEvaluation, SolutionEvaluator
from .merge import merge_specs
from .models import Candidate, ParsedCsv, ParsedRow, Solution, SolveOptions, SupportTable, Token
from .output import write_solution_csv
from .parsing import parse_cell
from .validation import validate_testcases


LOW_USE_MAX_REFINEMENT_ROUNDS = 2
LOW_USE_MAX_SELECTED_MERGE_PARTNERS = 50
LOW_USE_MAX_REPLACEMENT_CANDIDATES = 200
LOW_USE_MAX_RESCUE_MERGE_CANDIDATES = 50
LOW_USE_MIN_REMAINING_SECONDS = 0.05
OUTPUT_METADATA_COLUMNS = frozenset(
    {
        "spec_id",
        "covered_tc_ids",
        "covered_count",
        "equipment_count",
        "solve_status",
        "assigned_tc_ids",
        "assigned_count",
    }
)


@dataclass(frozen=True)
class _LowUseRefinement:
    solution: Solution
    evaluation: SolutionEvaluation
    changed: bool
    completed: bool


@dataclass(frozen=True)
class _LowUseMove:
    candidates: tuple[Candidate, ...] | None
    evaluation: SolutionEvaluation | None
    completed: bool


def solve_to_csv(parsed: ParsedCsv, support: SupportTable, output_path: Path, options: SolveOptions) -> None:
    candidates = generate_candidates(parsed, support, options)
    solve_deadline = time.monotonic() + options.timeout_seconds
    try:
        if options.solver == "stdlib":
            from .optimizer import optimize

            solution = optimize(candidates, len(parsed.rows), _remaining_seconds(solve_deadline))
        else:
            try:
                from .ortools_optimizer import OrtoolsUnavailableError, optimize

                solution = optimize(candidates, len(parsed.rows), _remaining_seconds(solve_deadline), solver_threads=options.solver_threads)
            except OrtoolsUnavailableError:
                if options.solver == "ortools":
                    raise
                from .optimizer import optimize

                solution = optimize(candidates, len(parsed.rows), _remaining_seconds(solve_deadline))
    except RuntimeError as exc:
        raise InputError(str(exc)) from exc
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    evaluator = SolutionEvaluator(parsed, support, options)
    try:
        refinement = _run_low_use_refinement(candidates, solution, evaluator, options, solve_deadline)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    solution = _solution_with_low_use_status(refinement, options)
    _report_low_use(refinement.evaluation, options)
    write_solution_csv(output_path, parsed, support, solution, options)


def refine_output_to_csv(
    parsed: ParsedCsv,
    support: SupportTable,
    refine_output_path: Path,
    output_path: Path,
    options: SolveOptions,
) -> None:
    candidates = generate_candidates(parsed, support, options)
    evaluator = SolutionEvaluator(parsed, support, options)
    imported_candidates = _read_output_candidates(refine_output_path, parsed, support, evaluator)
    solution = Solution(
        candidates=imported_candidates,
        assignments=_assign_candidates(imported_candidates, len(parsed.rows)),
        status="OPTIMAL",
    )
    try:
        evaluator.evaluate(solution.candidates)
        refinement = _run_low_use_refinement(
            tuple({candidate.signature: candidate for candidate in candidates + imported_candidates}.values()),
            solution,
            evaluator,
            options,
            solve_deadline=None,
        )
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    solution = _solution_with_low_use_status(refinement, options)
    _report_low_use(refinement.evaluation, options)
    write_solution_csv(output_path, parsed, support, solution, options)


def _run_low_use_refinement(
    candidates: tuple[Candidate, ...],
    solution: Solution,
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    solve_deadline: float | None,
) -> _LowUseRefinement:
    deadline = _low_use_refinement_deadline(options, solve_deadline)
    if options.min_assigned_cases_per_spec <= 0:
        print("Low-use refinement disabled.", file=sys.stderr)
    else:
        print("Running low-use refinement.", file=sys.stderr)
    refinement = _refine_low_use_specs(candidates, solution, evaluator, options, deadline)
    if options.min_assigned_cases_per_spec > 0:
        if refinement.completed and refinement.changed:
            print("Low-use refinement completed with changes.", file=sys.stderr)
        elif refinement.completed:
            print("Low-use refinement completed without changes.", file=sys.stderr)
        else:
            print("Low-use refinement timed out before completing.", file=sys.stderr)
    return refinement


def _low_use_refinement_deadline(options: SolveOptions, solve_deadline: float | None) -> float | None:
    if options.low_use_refinement_timeout_seconds is not None:
        return time.monotonic() + options.low_use_refinement_timeout_seconds
    if solve_deadline is not None:
        return solve_deadline
    return time.monotonic() + options.timeout_seconds


def _read_output_candidates(
    path: Path,
    parsed: ParsedCsv,
    support: SupportTable,
    evaluator: SolutionEvaluator,
) -> tuple[Candidate, ...]:
    requirement_columns = tuple(column for column in parsed.columns if column != "tc_id")
    imported = _read_output_specs(path, requirement_columns, support)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for row in imported.rows:
        spec = {column: row.tokens.get(column, ()) for column in requirement_columns}
        candidate = candidate_from_spec(
            spec,
            source_indexes=(),
            coverage_index=evaluator.coverage_index,
        )
        if candidate is None:
            raise InputError(f"{path}:{row.row_number} imported spec does not cover any current testcase")
        if candidate.signature in seen:
            raise InputError(f"{path}:{row.row_number} has duplicate imported spec signature {candidate.signature!r}")
        seen.add(candidate.signature)
        candidates.append(candidate)

    try:
        evaluator.evaluate(tuple(candidates))
    except ValueError as exc:
        raise InputError(f"{path} imported specs do not cover the current input: {exc}") from exc
    return tuple(candidates)


def _read_output_specs(path: Path, requirement_columns: tuple[str, ...], support: SupportTable) -> ParsedCsv:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise InputError(f"{path} has no header row")

        fieldnames = tuple(reader.fieldnames)
        missing = [column for column in requirement_columns if column not in fieldnames]
        if missing:
            raise InputError(f"{path} is missing current input requirement column(s): {', '.join(missing)}")

        unknown = [
            column
            for column in fieldnames
            if column not in requirement_columns and column not in OUTPUT_METADATA_COLUMNS
        ]
        if unknown:
            raise InputError(f"{path} has unexpected output column(s): {', '.join(unknown)}")

        rows: list[ParsedRow] = []
        for row_number, raw_row in enumerate(reader, start=2):
            if None in raw_row:
                raise InputError(f"{path}:{row_number} has more values than headers")
            raw = {"tc_id": f"imported_spec_{row_number}"}
            tokens: dict[str, tuple[Token, ...]] = {}
            for column in requirement_columns:
                raw_value = raw_row.get(column) or ""
                raw[column] = raw_value
                tokens[column] = parse_cell(raw_value, blank_tokens=())
            rows.append(ParsedRow(row_number=row_number, raw=raw, tokens=tokens))

    if not rows:
        raise InputError(f"{path} has no imported spec rows")

    imported = ParsedCsv(path=path, columns=("tc_id",) + requirement_columns, rows=tuple(rows))
    validate_testcases(imported, support, final_solver=True)
    return imported


def _refine_low_use_specs(
    candidates: tuple[Candidate, ...],
    solution: Solution,
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None = None,
) -> _LowUseRefinement:
    best_candidates = _unique_candidates(solution.candidates)
    best_evaluation = evaluator.evaluate(best_candidates)
    if options.min_assigned_cases_per_spec <= 0 or best_evaluation.low_use_spec_count == 0:
        return _LowUseRefinement(solution, best_evaluation, changed=False, completed=True)
    if deadline is not None and not _has_refinement_time(deadline):
        return _LowUseRefinement(solution, best_evaluation, changed=False, completed=False)

    changed = False
    all_candidates = tuple(sorted(candidates, key=lambda candidate: candidate.signature))
    for _round in range(LOW_USE_MAX_REFINEMENT_ROUNDS):
        improved = False
        low_rows = sorted(
            (row for row in best_evaluation.rows if row.assigned_count < options.min_assigned_cases_per_spec),
            key=lambda row: (row.assigned_count, row.evaluated.output_signature),
        )
        for low_row in low_rows:
            if deadline is not None and not _has_refinement_time(deadline):
                return _LowUseRefinement(_solution_for_candidates(solution, best_candidates, evaluator), best_evaluation, changed=changed, completed=False)
            current_candidates = tuple(row.evaluated.candidate for row in best_evaluation.rows)
            replacement = _best_low_use_move(low_row.evaluated.candidate, current_candidates, all_candidates, best_evaluation, evaluator, deadline)
            if not replacement.completed:
                return _LowUseRefinement(_solution_for_candidates(solution, best_candidates, evaluator), best_evaluation, changed=changed, completed=False)
            if replacement.candidates is None or replacement.evaluation is None:
                continue
            best_candidates, best_evaluation = replacement.candidates, replacement.evaluation
            changed = True
            improved = True
            break
        if not improved or best_evaluation.low_use_spec_count == 0:
            break

    return _LowUseRefinement(_solution_for_candidates(solution, best_candidates, evaluator), best_evaluation, changed=changed, completed=True)


def _best_low_use_move(
    low_candidate: Candidate,
    selected: tuple[Candidate, ...],
    all_candidates: tuple[Candidate, ...],
    current_evaluation: SolutionEvaluation,
    evaluator: SolutionEvaluator,
    deadline: float | None = None,
) -> _LowUseMove:
    best_candidates: tuple[Candidate, ...] | None = None
    best_evaluation: SolutionEvaluation | None = None
    selected_signatures = {candidate.signature for candidate in selected}
    selected_without_low = tuple(candidate for candidate in selected if candidate.signature != low_candidate.signature)

    def consider(trial_candidates: tuple[Candidate, ...]) -> bool:
        nonlocal best_candidates, best_evaluation
        if deadline is not None and not _has_refinement_time(deadline):
            return False
        trial_candidates = _unique_candidates(trial_candidates)
        try:
            trial_evaluation = evaluator.evaluate(trial_candidates)
        except ValueError:
            return True
        if not _acceptable_low_use_improvement(trial_evaluation, current_evaluation):
            return True
        if best_evaluation is None or trial_evaluation.objective() < best_evaluation.objective():
            best_candidates = trial_candidates
            best_evaluation = trial_evaluation
        return True

    if selected_without_low and not consider(selected_without_low):
        return _LowUseMove(best_candidates, best_evaluation, completed=False)

    selected_others = sorted(selected_without_low, key=lambda candidate: candidate.signature)[:LOW_USE_MAX_SELECTED_MERGE_PARTNERS]
    for other in selected_others:
        if deadline is not None and not _has_refinement_time(deadline):
            return _LowUseMove(best_candidates, best_evaluation, completed=False)
        merged = _merged_candidate(low_candidate, other, evaluator)
        if merged is not None and not consider(tuple(candidate for candidate in selected_without_low if candidate.signature != other.signature) + (merged,)):
            return _LowUseMove(best_candidates, best_evaluation, completed=False)

    target_indexes = _target_indexes_for_candidate(low_candidate, current_evaluation)
    promising = [
        candidate
        for candidate in all_candidates
        if candidate.signature not in selected_signatures and (not target_indexes or target_indexes & candidate.coverage)
    ]
    promising.sort(key=lambda candidate: (candidate.equipment_count, -len(candidate.coverage & target_indexes), candidate.signature))

    for candidate in promising[:LOW_USE_MAX_REPLACEMENT_CANDIDATES]:
        if not consider(selected_without_low + (candidate,)):
            return _LowUseMove(best_candidates, best_evaluation, completed=False)

    for candidate in promising[:LOW_USE_MAX_RESCUE_MERGE_CANDIDATES]:
        if deadline is not None and not _has_refinement_time(deadline):
            return _LowUseMove(best_candidates, best_evaluation, completed=False)
        merged = _merged_candidate(low_candidate, candidate, evaluator)
        if merged is not None and not consider(selected_without_low + (merged,)):
            return _LowUseMove(best_candidates, best_evaluation, completed=False)

    return _LowUseMove(best_candidates, best_evaluation, completed=True)


def _acceptable_low_use_improvement(candidate: SolutionEvaluation, current: SolutionEvaluation) -> bool:
    if candidate.total_equipment > current.total_equipment:
        return False
    if candidate.total_assignment_excess > current.total_assignment_excess:
        return False
    return (candidate.low_use_spec_count, candidate.low_use_deficit) < (current.low_use_spec_count, current.low_use_deficit)


def _target_indexes_for_candidate(candidate: Candidate, evaluation: SolutionEvaluation) -> frozenset[int]:
    for row in evaluation.rows:
        if row.evaluated.candidate.signature == candidate.signature:
            if row.assigned_indexes:
                return frozenset(row.assigned_indexes)
            return frozenset(row.evaluated.coverage.row_indexes)
    return frozenset(candidate.coverage)


def _merged_candidate(left: Candidate, right: Candidate, evaluator: SolutionEvaluator) -> Candidate | None:
    columns = evaluator.coverage_index.columns
    merged = merge_specs(left.spec, right.spec, columns)
    return candidate_from_spec(
        merged,
        tuple(sorted(set(left.source_indexes) | set(right.source_indexes))),
        evaluator.coverage_index,
    )


def _unique_candidates(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    by_signature = {candidate.signature: candidate for candidate in candidates}
    return tuple(by_signature[signature] for signature in sorted(by_signature))


def _assign_candidates(candidates: tuple[Candidate, ...], testcase_count: int) -> dict[int, Candidate]:
    assignments: dict[int, Candidate] = {}
    for index in range(testcase_count):
        covering = [candidate for candidate in candidates if index in candidate.coverage]
        if covering:
            assignments[index] = min(covering, key=lambda candidate: (candidate.assignment_excess.get(index, 0), candidate.equipment_count, candidate.signature))
    return assignments


def _solution_for_candidates(original: Solution, candidates: tuple[Candidate, ...], evaluator: SolutionEvaluator) -> Solution:
    if tuple(candidate.signature for candidate in candidates) == tuple(candidate.signature for candidate in original.candidates):
        return original
    return Solution(
        candidates=candidates,
        assignments=_assign_candidates(candidates, len(evaluator.parsed.rows)),
        status=original.status,
    )


def _solution_with_low_use_status(refinement: _LowUseRefinement, options: SolveOptions) -> Solution:
    solution = refinement.solution
    if options.min_assigned_cases_per_spec <= 0:
        return solution
    if solution.status == "FEASIBLE_TIMEOUT" or not refinement.completed:
        status = "FEASIBLE_TIMEOUT"
    elif refinement.changed:
        status = "FEASIBLE_LOW_USE_REFINED"
    else:
        status = "FEASIBLE_LOW_USE_CHECKED"
    return Solution(solution.candidates, solution.assignments, status)


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _has_refinement_time(deadline: float) -> bool:
    return _remaining_seconds(deadline) > LOW_USE_MIN_REMAINING_SECONDS


def _report_low_use(evaluation: SolutionEvaluation, options: SolveOptions) -> None:
    threshold = options.min_assigned_cases_per_spec
    if threshold <= 0 or not evaluation.low_use_spec_count:
        return
    smallest = min(row.assigned_count for row in evaluation.rows)
    print(
        "Low-use specs remain: "
        f"{evaluation.low_use_spec_count} selected specs have fewer than {threshold} assigned testcases "
        f"(smallest assigned_count={smallest}, low_use_deficit={evaluation.low_use_deficit}).",
        file=sys.stderr,
    )
