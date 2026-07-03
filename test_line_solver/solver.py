"""Top-level solving orchestration."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import os
import sys
import time

from .candidates import candidate_from_spec, generate_candidates
from .coverage import equipment_count
from .errors import InputError
from .evaluation import EvaluatedCandidate, SolutionEvaluation, SolutionEvaluator
from .merge import merge_specs
from .models import Candidate, ParsedCsv, ParsedRow, Solution, SolveOptions, SupportTable, Token
from .optimizer import _iter_bits
from .output import write_solution_csv
from .parsing import parse_cell
from .validation import validate_testcases


LOW_USE_MIN_REMAINING_SECONDS = 0.05
OUTPUT_METADATA_COLUMNS = frozenset(
    {
        "spec_id",
        "covered_tc_ids",
        "covered_count",
        "equipment_count",
        "solve_status",
        "main_solve_status",
        "low_use_refinement_status",
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
class _LowUseCandidatePool:
    candidates: tuple[Candidate, ...]
    completed: bool


@dataclass(frozen=True)
class _RefinementIndexedCandidate:
    candidate: Candidate
    evaluated: EvaluatedCandidate
    group_mask: int
    excess_by_group: dict[int, int]
    equipment_count: int


@dataclass(frozen=True)
class _ExactLowUseResult:
    candidates: tuple[Candidate, ...]
    assignments: dict[int, Candidate]
    evaluation: SolutionEvaluation
    completed: bool


@dataclass(frozen=True)
class _EvacuationMove:
    candidates: tuple[Candidate, ...]
    assignments: dict[int, Candidate]
    evaluation: SolutionEvaluation


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
        main_status="IMPORTED",
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
    best_solution = _solution_for_candidates(solution, best_candidates, evaluator)
    best_evaluation = evaluator.evaluate(best_solution.candidates, best_solution.assignments)
    if options.min_assigned_cases_per_spec <= 0 or best_evaluation.low_use_spec_count == 0:
        return _LowUseRefinement(best_solution, best_evaluation, changed=False, completed=True)
    if deadline is not None and not _has_refinement_time(deadline):
        return _LowUseRefinement(best_solution, best_evaluation, changed=False, completed=False)

    starting_solution = best_solution
    starting_evaluation = best_evaluation
    starting_low_assignments = _low_use_starting_assignments(starting_evaluation, options)
    evacuation = _evacuate_low_use_specs(
        candidates,
        best_solution,
        best_evaluation,
        starting_evaluation,
        starting_low_assignments,
        evaluator,
        options,
        deadline,
    )
    best_solution = Solution(
        evacuation.candidates,
        evacuation.assignments,
        solution.status,
        solution.main_status,
        solution.refinement_status,
    )
    best_evaluation = evacuation.evaluation

    if deadline is not None and not _has_refinement_time(deadline):
        changed = _solution_changed(starting_solution, evacuation)
        return _LowUseRefinement(best_solution, best_evaluation, changed=changed, completed=False)

    pool = _build_low_use_refinement_pool(candidates, best_evaluation, evaluator, options, deadline)
    result = _optimize_low_use_pool(
        pool.candidates,
        best_evaluation,
        starting_evaluation,
        starting_low_assignments,
        evaluator,
        options,
        deadline,
    )
    refined_solution = Solution(
        result.candidates,
        result.assignments,
        solution.status,
        solution.main_status,
        solution.refinement_status,
    )
    return _LowUseRefinement(
        refined_solution,
        result.evaluation,
        changed=_solution_changed(starting_solution, result),
        completed=pool.completed and result.completed and evacuation.completed,
    )


def _build_low_use_refinement_pool(
    candidates: tuple[Candidate, ...],
    evaluation: SolutionEvaluation,
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> _LowUseCandidatePool:
    selected = tuple(row.evaluated.candidate for row in evaluation.rows)
    low_rows = tuple(row for row in evaluation.rows if row.assigned_count < options.min_assigned_cases_per_spec)
    target_group_mask = 0
    for row in low_rows:
        target_group_mask |= row.evaluated.coverage.group_mask
        for testcase_index in row.assigned_indexes:
            target_group_mask |= 1 << evaluator.coverage_index.row_to_group[testcase_index]

    completed = True
    pool_by_signature: dict[str, Candidate] = {}

    def add(candidate: Candidate, *, required: bool = False) -> bool:
        nonlocal completed
        if candidate.signature in pool_by_signature:
            return True
        if not required and len(pool_by_signature) >= options.max_low_use_refinement_candidates:
            completed = False
            return False
        pool_by_signature[candidate.signature] = candidate
        if len(pool_by_signature) > options.max_low_use_refinement_candidates:
            completed = False
        return True

    for candidate in sorted(selected, key=lambda item: item.signature):
        add(candidate, required=True)

    all_candidates = tuple(sorted(candidates, key=lambda item: _pool_candidate_key(item, target_group_mask)))
    normal_candidates: list[Candidate] = []
    for candidate in all_candidates:
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            break
        group_mask = _evaluated_group_mask(candidate, evaluator)
        if target_group_mask and not (group_mask & target_group_mask):
            continue
        normal_candidates.append(candidate)
        if not add(candidate):
            break

    merge_partners = tuple(
        sorted(
            {candidate.signature: candidate for candidate in selected + tuple(normal_candidates)}.values(),
            key=lambda item: _pool_candidate_key(item, target_group_mask),
        )
    )
    frontier = tuple(row.evaluated.candidate for row in low_rows)
    for _depth in range(options.max_low_use_merge_depth):
        if not frontier or not completed:
            break
        next_frontier: list[Candidate] = []
        for left in sorted(frontier, key=lambda item: item.signature):
            for right in merge_partners:
                if deadline is not None and not _has_refinement_time(deadline):
                    completed = False
                    break
                merged = _merged_candidate(left, right, evaluator)
                if merged is None or merged.signature in pool_by_signature:
                    continue
                if not add(merged):
                    break
                next_frontier.append(merged)
            if not completed:
                break
        frontier = tuple(next_frontier)

    return _LowUseCandidatePool(
        candidates=tuple(pool_by_signature[signature] for signature in sorted(pool_by_signature)),
        completed=completed,
    )


def _pool_candidate_key(candidate: Candidate, target_group_mask: int) -> tuple[int, int, str]:
    target_coverage = (candidate.group_coverage_mask & target_group_mask).bit_count() if target_group_mask else len(candidate.coverage)
    return (candidate.equipment_count, -target_coverage, candidate.signature)


def _evaluated_group_mask(candidate: Candidate, evaluator: SolutionEvaluator) -> int:
    if candidate.group_coverage_mask:
        return candidate.group_coverage_mask
    return evaluator._evaluate_candidate(candidate).coverage.group_mask


def _evacuate_low_use_specs(
    candidates: tuple[Candidate, ...],
    solution: Solution,
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    starting_low_assignments: dict[int, str],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> _ExactLowUseResult:
    current_solution = solution
    current_evaluation = evaluation
    completed = True

    while current_evaluation.low_use_spec_count:
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            break
        move, move_completed = _best_affordable_evacuation_move(
            candidates,
            current_evaluation,
            starting_evaluation,
            starting_low_assignments,
            evaluator,
            options,
            deadline,
        )
        completed = completed and move_completed
        if move is None:
            break
        current_solution = Solution(
            move.candidates,
            move.assignments,
            current_solution.status,
            current_solution.main_status,
            current_solution.refinement_status,
        )
        current_evaluation = move.evaluation

    return _ExactLowUseResult(
        candidates=current_solution.candidates,
        assignments=current_solution.assignments,
        evaluation=current_evaluation,
        completed=completed,
    )


def _best_affordable_evacuation_move(
    candidates: tuple[Candidate, ...],
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    starting_low_assignments: dict[int, str],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> tuple[_EvacuationMove | None, bool]:
    current_candidates = tuple(row.evaluated.candidate for row in evaluation.rows)
    current_assignments = _assignments_from_evaluation(evaluation)
    low_rows = tuple(row for row in evaluation.rows if row.assigned_count < options.min_assigned_cases_per_spec)
    low_signatures = {row.evaluated.candidate.signature for row in low_rows}
    non_low_rows = tuple(row for row in evaluation.rows if row.evaluated.candidate.signature not in low_signatures)
    selected_signatures = {candidate.signature for candidate in current_candidates}
    best_move: _EvacuationMove | None = None
    best_objective: tuple[int, int, int, int, int, tuple[str, ...]] | None = None
    completed = True

    def consider(trial_candidates: tuple[Candidate, ...], trial_assignments: dict[int, Candidate]) -> bool:
        nonlocal best_move, best_objective, completed
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            return False
        try:
            trial_evaluation = evaluator.evaluate(trial_candidates, trial_assignments)
        except ValueError:
            return True
        if (trial_evaluation.low_use_spec_count, trial_evaluation.low_use_deficit) >= (
            evaluation.low_use_spec_count,
            evaluation.low_use_deficit,
        ):
            return True
        if not _result_is_affordable(trial_evaluation, trial_assignments, starting_evaluation, starting_low_assignments, options):
            return True
        objective = _evacuation_objective(trial_evaluation, starting_evaluation)
        if best_objective is None or objective < best_objective:
            best_objective = objective
            best_move = _EvacuationMove(trial_candidates, trial_assignments, trial_evaluation)
        return True

    for low_row in low_rows:
        low_candidate = low_row.evaluated.candidate
        if not low_row.assigned_indexes:
            trial_candidates = _replace_selected_candidates(current_candidates, {low_candidate.signature}, ())
            if not consider(trial_candidates, dict(current_assignments)):
                return best_move, completed

    for low_row in low_rows:
        if not low_row.assigned_indexes:
            continue
        trial = _assign_low_row_to_existing_receivers(low_row, non_low_rows, current_candidates, current_assignments, evaluator)
        if trial is not None and not consider(*trial):
            return best_move, completed

    for low_row in low_rows:
        for receiver_row in non_low_rows:
            trial = _merge_low_row_into_receiver(low_row, receiver_row, current_candidates, current_assignments, evaluator)
            if trial is not None and not consider(*trial):
                return best_move, completed

    for left_index, left_row in enumerate(low_rows):
        for right_row in low_rows[left_index + 1 :]:
            trial = _merge_low_rows(left_row, right_row, current_candidates, current_assignments, evaluator)
            if trial is not None and not consider(*trial):
                return best_move, completed

    generated = _generated_evacuation_candidates(candidates, low_rows, selected_signatures, evaluator, options, deadline)
    completed = completed and generated.completed
    for candidate in generated.candidates:
        trial = _replace_low_rows_with_generated_candidate(candidate, low_rows, current_candidates, current_assignments, evaluator)
        if trial is not None and not consider(*trial):
            return best_move, completed

    return best_move, completed


def _assign_low_row_to_existing_receivers(
    low_row,
    receiver_rows,
    current_candidates: tuple[Candidate, ...],
    current_assignments: dict[int, Candidate],
    evaluator: SolutionEvaluator,
) -> tuple[tuple[Candidate, ...], dict[int, Candidate]] | None:
    if not receiver_rows:
        return None
    trial_assignments = dict(current_assignments)
    for testcase_index in low_row.assigned_indexes:
        group_index = evaluator.coverage_index.row_to_group[testcase_index]
        covering = [
            row
            for row in receiver_rows
            if row.evaluated.coverage.group_mask & (1 << group_index)
        ]
        if not covering:
            return None
        receiver = min(
            covering,
            key=lambda row: (
                row.evaluated.coverage.excess_by_group.get(group_index, 0),
                equipment_count(row.evaluated.spec),
                row.evaluated.output_signature,
            ),
        )
        trial_assignments[testcase_index] = receiver.evaluated.candidate
    return (
        _replace_selected_candidates(current_candidates, {low_row.evaluated.candidate.signature}, ()),
        trial_assignments,
    )


def _merge_low_row_into_receiver(
    low_row,
    receiver_row,
    current_candidates: tuple[Candidate, ...],
    current_assignments: dict[int, Candidate],
    evaluator: SolutionEvaluator,
) -> tuple[tuple[Candidate, ...], dict[int, Candidate]] | None:
    merged = _merged_candidate(low_row.evaluated.candidate, receiver_row.evaluated.candidate, evaluator)
    if merged is None:
        return None
    removed = {low_row.evaluated.candidate.signature, receiver_row.evaluated.candidate.signature}
    trial_assignments = dict(current_assignments)
    for testcase_index, assigned_candidate in current_assignments.items():
        if assigned_candidate.signature in removed:
            trial_assignments[testcase_index] = merged
    return _replace_selected_candidates(current_candidates, removed, (merged,)), trial_assignments


def _merge_low_rows(
    left_row,
    right_row,
    current_candidates: tuple[Candidate, ...],
    current_assignments: dict[int, Candidate],
    evaluator: SolutionEvaluator,
) -> tuple[tuple[Candidate, ...], dict[int, Candidate]] | None:
    merged = _merged_candidate(left_row.evaluated.candidate, right_row.evaluated.candidate, evaluator)
    if merged is None:
        return None
    removed = {left_row.evaluated.candidate.signature, right_row.evaluated.candidate.signature}
    trial_assignments = dict(current_assignments)
    for testcase_index, assigned_candidate in current_assignments.items():
        if assigned_candidate.signature in removed:
            trial_assignments[testcase_index] = merged
    return _replace_selected_candidates(current_candidates, removed, (merged,)), trial_assignments


def _replace_low_rows_with_generated_candidate(
    candidate: Candidate,
    low_rows,
    current_candidates: tuple[Candidate, ...],
    current_assignments: dict[int, Candidate],
    evaluator: SolutionEvaluator,
) -> tuple[tuple[Candidate, ...], dict[int, Candidate]] | None:
    covered_low_rows = [
        row
        for row in low_rows
        if row.assigned_indexes and _candidate_covers_indexes(candidate, row.assigned_indexes, evaluator)
    ]
    if not covered_low_rows:
        return None
    removed = {row.evaluated.candidate.signature for row in covered_low_rows}
    trial_assignments = dict(current_assignments)
    for row in covered_low_rows:
        for testcase_index in row.assigned_indexes:
            trial_assignments[testcase_index] = candidate
    return _replace_selected_candidates(current_candidates, removed, (candidate,)), trial_assignments


def _generated_evacuation_candidates(
    candidates: tuple[Candidate, ...],
    low_rows,
    selected_signatures: set[str],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> _LowUseCandidatePool:
    low_group_mask = 0
    for row in low_rows:
        for testcase_index in row.assigned_indexes:
            low_group_mask |= 1 << evaluator.coverage_index.row_to_group[testcase_index]
    if not low_group_mask:
        return _LowUseCandidatePool((), completed=True)
    completed = True
    generated: list[tuple[tuple[int, int, int, str], Candidate]] = []
    for candidate in candidates:
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            break
        if candidate.signature in selected_signatures:
            continue
        group_mask = _evaluated_group_mask(candidate, evaluator)
        if group_mask & low_group_mask:
            if deadline is not None and not _has_refinement_time(deadline):
                completed = False
                break
            generated.append((_evacuation_candidate_key(candidate, low_group_mask, evaluator), candidate))
    if len(generated) > options.max_low_use_refinement_candidates:
        completed = False
    generated.sort(key=lambda item: item[0])
    if deadline is not None and not _has_refinement_time(deadline):
        completed = False
    return _LowUseCandidatePool(
        candidates=tuple(candidate for _key, candidate in generated[: options.max_low_use_refinement_candidates]),
        completed=completed,
    )


def _evacuation_candidate_key(candidate: Candidate, low_group_mask: int, evaluator: SolutionEvaluator) -> tuple[int, int, int, str]:
    evaluated = evaluator._evaluate_candidate(candidate)
    covered_low_mask = evaluated.coverage.group_mask & low_group_mask
    excess = sum(
        evaluator.coverage_index.groups[group_index].weight * evaluated.coverage.excess_by_group.get(group_index, 0)
        for group_index in _iter_bits(covered_low_mask)
    )
    return (
        equipment_count(evaluated.spec),
        excess,
        -covered_low_mask.bit_count(),
        evaluated.output_signature,
    )


def _candidate_covers_indexes(candidate: Candidate, indexes: tuple[int, ...], evaluator: SolutionEvaluator) -> bool:
    evaluated = evaluator._evaluate_candidate(candidate)
    for testcase_index in indexes:
        group_index = evaluator.coverage_index.row_to_group[testcase_index]
        if not evaluated.coverage.group_mask & (1 << group_index):
            return False
    return True


def _replace_selected_candidates(
    current_candidates: tuple[Candidate, ...],
    removed_signatures: set[str],
    additions: tuple[Candidate, ...],
) -> tuple[Candidate, ...]:
    by_signature = {
        candidate.signature: candidate
        for candidate in current_candidates
        if candidate.signature not in removed_signatures
    }
    for candidate in additions:
        by_signature[candidate.signature] = candidate
    return tuple(by_signature[signature] for signature in sorted(by_signature))


def _low_use_starting_assignments(evaluation: SolutionEvaluation, options: SolveOptions) -> dict[int, str]:
    assignments: dict[int, str] = {}
    for row in evaluation.rows:
        if row.assigned_count >= options.min_assigned_cases_per_spec:
            continue
        for testcase_index in row.assigned_indexes:
            assignments[testcase_index] = row.evaluated.candidate.signature
    return assignments


def _result_is_affordable(
    evaluation: SolutionEvaluation,
    assignments: dict[int, Candidate],
    starting_evaluation: SolutionEvaluation,
    starting_low_assignments: dict[int, str],
    options: SolveOptions,
) -> bool:
    equipment_delta = evaluation.total_equipment - starting_evaluation.total_equipment
    if equipment_delta > options.low_use_affordable_equipment_delta:
        return False
    assignment_excess_delta = evaluation.total_assignment_excess - starting_evaluation.total_assignment_excess
    evacuated_count = _evacuated_case_count(assignments, starting_low_assignments)
    return assignment_excess_delta <= evacuated_count * options.low_use_affordable_excess_per_case


def _evacuated_case_count(assignments: dict[int, Candidate], starting_low_assignments: dict[int, str]) -> int:
    return sum(
        1
        for testcase_index, original_signature in starting_low_assignments.items()
        if assignments[testcase_index].signature != original_signature
    )


def _evacuation_objective(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
) -> tuple[int, int, int, int, int, tuple[str, ...]]:
    return (
        evaluation.low_use_spec_count,
        evaluation.low_use_deficit,
        evaluation.total_equipment - starting_evaluation.total_equipment,
        evaluation.total_assignment_excess - starting_evaluation.total_assignment_excess,
        evaluation.selected_spec_count,
        tuple(sorted(row.evaluated.output_signature for row in evaluation.rows)),
    )


def _optimize_low_use_pool(
    candidates: tuple[Candidate, ...],
    incumbent_evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    starting_low_assignments: dict[int, str],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> _ExactLowUseResult:
    incumbent_assignments = _assignments_from_evaluation(incumbent_evaluation)
    if options.solver != "stdlib":
        try:
            result = _optimize_low_use_pool_with_ortools(candidates, evaluator, options, deadline)
        except ImportError:
            result = None
        if result is not None:
            return _better_exact_result(
                result,
                incumbent_evaluation,
                incumbent_assignments,
                starting_evaluation,
                starting_low_assignments,
                evaluator,
                options,
            )

    if len(candidates) <= options.max_low_use_stdlib_candidates:
        return _better_exact_result(
            _optimize_low_use_pool_stdlib(candidates, evaluator, options, deadline),
            incumbent_evaluation,
            incumbent_assignments,
            starting_evaluation,
            starting_low_assignments,
            evaluator,
            options,
        )

    incumbent_candidates = tuple(row.evaluated.candidate for row in incumbent_evaluation.rows)
    return _ExactLowUseResult(
        candidates=incumbent_candidates,
        assignments=incumbent_assignments,
        evaluation=incumbent_evaluation,
        completed=False,
    )


def _better_exact_result(
    result: _ExactLowUseResult,
    incumbent_evaluation: SolutionEvaluation,
    incumbent_assignments: dict[int, Candidate],
    starting_evaluation: SolutionEvaluation,
    starting_low_assignments: dict[int, str],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
) -> _ExactLowUseResult:
    if (
        _low_use_refinement_objective(result.evaluation) <= _low_use_refinement_objective(incumbent_evaluation)
        and _result_is_affordable(result.evaluation, result.assignments, starting_evaluation, starting_low_assignments, options)
        and _keeps_non_low_assignments(result.assignments, incumbent_assignments, starting_low_assignments)
    ):
        return result
    incumbent_candidates = tuple(row.evaluated.candidate for row in incumbent_evaluation.rows)
    return _ExactLowUseResult(
        candidates=incumbent_candidates,
        assignments=incumbent_assignments,
        evaluation=evaluator.evaluate(incumbent_candidates, incumbent_assignments),
        completed=False,
    )


def _keeps_non_low_assignments(
    result_assignments: dict[int, Candidate],
    incumbent_assignments: dict[int, Candidate],
    starting_low_assignments: dict[int, str],
) -> bool:
    for testcase_index, incumbent_candidate in incumbent_assignments.items():
        if testcase_index in starting_low_assignments:
            continue
        if result_assignments[testcase_index].signature != incumbent_candidate.signature:
            return False
    return True


def _optimize_low_use_pool_stdlib(
    candidates: tuple[Candidate, ...],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> _ExactLowUseResult:
    indexed = _index_low_use_candidates(candidates, evaluator)
    group_count = len(evaluator.coverage_index.groups)
    weights = tuple(group.weight for group in evaluator.coverage_index.groups)
    by_group = _low_use_candidates_by_group(indexed, group_count)
    group_order = tuple(sorted(range(group_count), key=lambda group: (len(by_group[group]), -weights[group], group)))
    if any(not covering for covering in by_group):
        raise ValueError("bounded low-use refinement pool does not cover every testcase group")

    greedy_group_assignments = _greedy_low_use_group_assignments(indexed, by_group, weights)
    best_group_assignments = list(greedy_group_assignments)
    best_objective = _low_use_assignment_objective(indexed, best_group_assignments, weights, options)
    current_group_assignments = [-1 for _group in range(group_count)]
    completed = True

    def visit(position: int) -> None:
        nonlocal best_group_assignments, best_objective, completed
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            return
        if position == len(group_order):
            objective = _low_use_assignment_objective(indexed, current_group_assignments, weights, options)
            if objective < best_objective:
                best_objective = objective
                best_group_assignments = list(current_group_assignments)
            return
        group_index = group_order[position]
        for candidate_index in by_group[group_index]:
            if not completed:
                return
            current_group_assignments[group_index] = candidate_index
            visit(position + 1)
            current_group_assignments[group_index] = -1

    visit(0)
    return _exact_result_from_group_assignments(indexed, best_group_assignments, evaluator, completed)


def _optimize_low_use_pool_with_ortools(
    candidates: tuple[Candidate, ...],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> _ExactLowUseResult | None:
    from ortools.sat.python import cp_model

    indexed = _index_low_use_candidates(candidates, evaluator)
    group_count = len(evaluator.coverage_index.groups)
    weights = tuple(group.weight for group in evaluator.coverage_index.groups)
    by_group = _low_use_candidates_by_group(indexed, group_count)
    if any(not covering for covering in by_group):
        raise ValueError("bounded low-use refinement pool does not cover every testcase group")

    model = cp_model.CpModel()
    assignment_vars: dict[tuple[int, int], object] = {}
    selected_vars = [model.NewBoolVar(f"selected_{candidate_index}") for candidate_index in range(len(indexed))]
    for group_index, covering in enumerate(by_group):
        group_vars = []
        for candidate_index in covering:
            variable = model.NewBoolVar(f"assign_{candidate_index}_{group_index}")
            assignment_vars[(candidate_index, group_index)] = variable
            group_vars.append(variable)
            model.Add(variable <= selected_vars[candidate_index])
        model.Add(sum(group_vars) == 1)

    low_vars = []
    deficit_vars = []
    threshold = options.min_assigned_cases_per_spec
    for candidate_index, indexed_candidate in enumerate(indexed):
        covered_vars = [
            assignment_vars[(candidate_index, group_index)]
            for group_index in _iter_bits(indexed_candidate.group_mask)
            if (candidate_index, group_index) in assignment_vars
        ]
        if covered_vars:
            model.Add(sum(covered_vars) >= selected_vars[candidate_index])
        else:
            model.Add(selected_vars[candidate_index] == 0)
        assigned_count = sum(weights[group_index] * assignment_vars[(candidate_index, group_index)] for group_index in _iter_bits(indexed_candidate.group_mask) if (candidate_index, group_index) in assignment_vars)
        deficit = model.NewIntVar(0, threshold, f"deficit_{candidate_index}")
        low = model.NewBoolVar(f"low_{candidate_index}")
        model.Add(deficit >= threshold * selected_vars[candidate_index] - assigned_count)
        model.Add(deficit <= threshold * selected_vars[candidate_index])
        model.Add(deficit <= threshold * low)
        model.Add(deficit >= low)
        model.Add(low <= selected_vars[candidate_index])
        deficit_vars.append(deficit)
        low_vars.append(low)

    low_count_expr = sum(low_vars)
    deficit_expr = sum(deficit_vars)
    equipment_expr = sum(indexed_candidate.equipment_count * selected_vars[candidate_index] for candidate_index, indexed_candidate in enumerate(indexed))
    excess_expr = sum(
        weights[group_index] * indexed[candidate_index].excess_by_group.get(group_index, 0) * variable
        for (candidate_index, group_index), variable in assignment_vars.items()
    )
    selected_count_expr = sum(selected_vars)
    signature_rank_expr = sum(rank * selected_vars[candidate_index] for rank, candidate_index in enumerate(sorted(range(len(indexed)), key=lambda index: indexed[index].candidate.signature)))

    best_result: _ExactLowUseResult | None = None
    completed = True
    for expression in (low_count_expr, deficit_expr, equipment_expr, excess_expr, selected_count_expr, signature_rank_expr):
        if deadline is not None and not _has_refinement_time(deadline):
            return _mark_incomplete(best_result)
        model.Minimize(expression)
        solver = cp_model.CpSolver()
        if deadline is not None:
            solver.parameters.max_time_in_seconds = max(0.0, _remaining_seconds(deadline))
        solver.parameters.num_search_workers = _thread_count(options.solver_threads)
        solver.parameters.random_seed = 0
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return _mark_incomplete(best_result)
        group_assignments = [
            next(candidate_index for candidate_index in by_group[group_index] if solver.BooleanValue(assignment_vars[(candidate_index, group_index)]))
            for group_index in range(group_count)
        ]
        best_result = _exact_result_from_group_assignments(indexed, group_assignments, evaluator, completed=status == cp_model.OPTIMAL)
        if status != cp_model.OPTIMAL:
            completed = False
            break
        model.Add(expression == solver.Value(expression))
    if best_result is None:
        return None
    if not completed:
        return _mark_incomplete(best_result)
    return best_result


def _mark_incomplete(result: _ExactLowUseResult | None) -> _ExactLowUseResult | None:
    if result is None:
        return None
    return _ExactLowUseResult(result.candidates, result.assignments, result.evaluation, completed=False)


def _thread_count(solver_threads: int | None) -> int:
    if solver_threads is not None:
        return max(1, solver_threads)
    return max(1, os.cpu_count() or 1)


def _index_low_use_candidates(candidates: tuple[Candidate, ...], evaluator: SolutionEvaluator) -> tuple[_RefinementIndexedCandidate, ...]:
    indexed: list[_RefinementIndexedCandidate] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: item.signature):
        if candidate.signature in seen:
            continue
        seen.add(candidate.signature)
        evaluated = evaluator._evaluate_candidate(candidate)
        indexed.append(
            _RefinementIndexedCandidate(
                candidate=candidate,
                evaluated=evaluated,
                group_mask=evaluated.coverage.group_mask,
                excess_by_group=evaluated.coverage.excess_by_group,
                equipment_count=equipment_count(evaluated.spec),
            )
        )
    return tuple(indexed)


def _low_use_candidates_by_group(indexed: tuple[_RefinementIndexedCandidate, ...], group_count: int) -> list[list[int]]:
    by_group: list[list[int]] = [[] for _group in range(group_count)]
    for candidate_index, indexed_candidate in enumerate(indexed):
        for group_index in _iter_bits(indexed_candidate.group_mask):
            by_group[group_index].append(candidate_index)
    for group_index, covering in enumerate(by_group):
        covering.sort(
            key=lambda candidate_index: (
                indexed[candidate_index].excess_by_group.get(group_index, 0),
                indexed[candidate_index].equipment_count,
                indexed[candidate_index].candidate.signature,
            )
        )
    return by_group


def _greedy_low_use_group_assignments(
    indexed: tuple[_RefinementIndexedCandidate, ...],
    by_group: list[list[int]],
    weights: tuple[int, ...],
) -> list[int]:
    assignments = [-1 for _group in by_group]
    assigned_counts = [0 for _candidate in indexed]
    for group_index, covering in sorted(enumerate(by_group), key=lambda item: (-weights[item[0]], len(item[1]), item[0])):
        candidate_index = min(
            covering,
            key=lambda index: (
                assigned_counts[index] == 0,
                indexed[index].excess_by_group.get(group_index, 0),
                indexed[index].equipment_count,
                indexed[index].candidate.signature,
            ),
        )
        assignments[group_index] = candidate_index
        assigned_counts[candidate_index] += weights[group_index]
    return assignments


def _low_use_assignment_objective(
    indexed: tuple[_RefinementIndexedCandidate, ...],
    group_assignments: list[int],
    weights: tuple[int, ...],
    options: SolveOptions,
) -> tuple[int, int, int, int, int, tuple[str, ...]]:
    assigned_counts = [0 for _candidate in indexed]
    total_excess = 0
    for group_index, candidate_index in enumerate(group_assignments):
        if candidate_index < 0:
            continue
        assigned_counts[candidate_index] += weights[group_index]
        total_excess += weights[group_index] * indexed[candidate_index].excess_by_group.get(group_index, 0)
    selected_indexes = tuple(index for index, assigned_count in enumerate(assigned_counts) if assigned_count > 0)
    deficits = [max(0, options.min_assigned_cases_per_spec - assigned_counts[index]) for index in selected_indexes]
    return (
        sum(1 for deficit in deficits if deficit),
        sum(deficits),
        sum(indexed[index].equipment_count for index in selected_indexes),
        total_excess,
        len(selected_indexes),
        tuple(sorted(indexed[index].evaluated.output_signature for index in selected_indexes)),
    )


def _exact_result_from_group_assignments(
    indexed: tuple[_RefinementIndexedCandidate, ...],
    group_assignments: list[int],
    evaluator: SolutionEvaluator,
    completed: bool,
) -> _ExactLowUseResult:
    selected_indexes = tuple(sorted({candidate_index for candidate_index in group_assignments if candidate_index >= 0}, key=lambda index: indexed[index].candidate.signature))
    selected_candidates = tuple(indexed[index].candidate for index in selected_indexes)
    selected_by_original_index = {original_index: indexed[original_index].candidate for original_index in selected_indexes}
    assignments: dict[int, Candidate] = {}
    for group_index, original_candidate_index in enumerate(group_assignments):
        candidate = selected_by_original_index[original_candidate_index]
        for row_index in evaluator.coverage_index.groups[group_index].row_indexes:
            assignments[row_index] = candidate
    evaluation = evaluator.evaluate(selected_candidates, assignments)
    return _ExactLowUseResult(
        candidates=selected_candidates,
        assignments=assignments,
        evaluation=evaluation,
        completed=completed,
    )


def _assignments_from_evaluation(evaluation: SolutionEvaluation) -> dict[int, Candidate]:
    assignments: dict[int, Candidate] = {}
    for row in evaluation.rows:
        for testcase_index in row.assigned_indexes:
            assignments[testcase_index] = row.evaluated.candidate
    return assignments


def _solution_changed(solution: Solution, result: _ExactLowUseResult) -> bool:
    if tuple(candidate.signature for candidate in solution.candidates) != tuple(candidate.signature for candidate in result.candidates):
        return True
    return {
        index: candidate.signature for index, candidate in solution.assignments.items()
    } != {
        index: candidate.signature for index, candidate in result.assignments.items()
    }


def _low_use_refinement_objective(evaluation: SolutionEvaluation) -> tuple[int, int, int, int, int, tuple[str, ...]]:
    return (
        evaluation.low_use_spec_count,
        evaluation.low_use_deficit,
        evaluation.total_equipment,
        evaluation.total_assignment_excess,
        evaluation.selected_spec_count,
        tuple(sorted(row.evaluated.output_signature for row in evaluation.rows)),
    )


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
    if {candidate.signature for candidate in candidates} == {candidate.signature for candidate in original.candidates}:
        return Solution(candidates, original.assignments, original.status, original.main_status, original.refinement_status)
    return Solution(
        candidates=candidates,
        assignments=_assign_candidates(candidates, len(evaluator.parsed.rows)),
        status=original.status,
        main_status=original.main_status,
        refinement_status=original.refinement_status,
    )


def _solution_with_low_use_status(refinement: _LowUseRefinement, options: SolveOptions) -> Solution:
    solution = refinement.solution
    main_status = solution.main_status or solution.status
    refinement_status = _low_use_refinement_status(refinement, options)
    if options.min_assigned_cases_per_spec <= 0:
        return Solution(solution.candidates, solution.assignments, solution.status, main_status, refinement_status)
    if solution.status == "FEASIBLE_TIMEOUT" or not refinement.completed:
        status = "FEASIBLE_TIMEOUT"
    elif refinement.changed:
        status = "FEASIBLE_LOW_USE_REFINED"
    else:
        status = "FEASIBLE_LOW_USE_CHECKED"
    return Solution(solution.candidates, solution.assignments, status, main_status, refinement_status)


def _low_use_refinement_status(refinement: _LowUseRefinement, options: SolveOptions) -> str:
    if options.min_assigned_cases_per_spec <= 0:
        return "DISABLED"
    if not refinement.completed:
        return "FEASIBLE_TIMEOUT"
    if refinement.changed:
        return "COMPLETED_REFINED"
    return "COMPLETED_UNCHANGED"


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
