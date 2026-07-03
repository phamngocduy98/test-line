"""Top-level solving orchestration."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from itertools import product
from pathlib import Path
import sys
import time

from .candidates import candidate_from_spec, generate_candidates
from .errors import InputError
from .evaluation import AssignedCandidate, SolutionEvaluation, SolutionEvaluator
from .merge import exact_spec, merge_specs
from .models import Candidate, ParsedCsv, ParsedRow, Solution, SolveOptions, SupportTable, Token
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
class _LowUseMergeResult:
    candidates: tuple[Candidate, ...]
    assignments: dict[int, Candidate]
    evaluation: SolutionEvaluation
    completed: bool


@dataclass(frozen=True)
class _ReceiverMergeMove:
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
        refinement = _run_low_use_refinement(solution, evaluator, options, solve_deadline)
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
        refinement = _run_low_use_refinement(solution, evaluator, options, solve_deadline=None)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    solution = _solution_with_low_use_status(refinement, options)
    _report_low_use(refinement.evaluation, options)
    write_solution_csv(output_path, parsed, support, solution, options)


def _run_low_use_refinement(
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
    refinement = _refine_low_use_specs(solution, evaluator, options, deadline)
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
    result = _run_fast_low_use_merge_pass(
        best_solution,
        best_evaluation,
        starting_evaluation,
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
        completed=result.completed,
    )


def _run_fast_low_use_merge_pass(
    solution: Solution,
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> _LowUseMergeResult:
    current_solution = solution
    current_evaluation = evaluation
    completed = True

    while current_evaluation.low_use_spec_count:
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            break
        move, move_completed = _best_receiver_merge_move(
            current_evaluation,
            starting_evaluation,
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

    return _LowUseMergeResult(
        candidates=current_solution.candidates,
        assignments=current_solution.assignments,
        evaluation=current_evaluation,
        completed=completed,
    )


def _best_receiver_merge_move(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> tuple[_ReceiverMergeMove | None, bool]:
    low_rows = tuple(row for row in evaluation.rows if row.assigned_count < options.min_assigned_cases_per_spec)
    receiver_rows = tuple(row for row in evaluation.rows if row.assigned_count >= options.min_assigned_cases_per_spec)
    low_case_indexes = tuple(sorted(index for row in low_rows for index in row.assigned_indexes))
    if not low_rows:
        return None, True
    if not receiver_rows and low_case_indexes:
        return _best_remove_unassigned_low_specs(evaluation, starting_evaluation, evaluator, options)
    if not _combination_count_exceeds(len(receiver_rows), len(low_case_indexes), options.max_low_use_merge_combinations):
        exact_move, exact_completed = _best_exact_receiver_merge_move(
            evaluation,
            starting_evaluation,
            low_rows,
            receiver_rows,
            low_case_indexes,
            evaluator,
            options,
            deadline,
        )
        if exact_move is not None or not exact_completed:
            return exact_move, exact_completed
    return _best_greedy_receiver_merge_move(
        evaluation,
        starting_evaluation,
        low_rows,
        receiver_rows,
        evaluator,
        options,
        deadline,
    )


def _best_remove_unassigned_low_specs(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    evaluator: SolutionEvaluator,
    options: SolveOptions,
) -> tuple[_ReceiverMergeMove | None, bool]:
    unassigned = tuple(row for row in evaluation.rows if row.assigned_count == 0)
    if not unassigned:
        return None, True
    trial = _receiver_merge_trial(
        evaluation,
        receiver_rows=(),
        case_to_receiver={},
        removed_low_signatures={row.evaluated.candidate.signature for row in unassigned},
        evaluator=evaluator,
    )
    if trial is None or not _receiver_merge_move_is_accepted(trial.evaluation, evaluation, starting_evaluation, options):
        return None, True
    return trial, True


def _best_exact_receiver_merge_move(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    low_rows: tuple[AssignedCandidate, ...],
    receiver_rows: tuple[AssignedCandidate, ...],
    low_case_indexes: tuple[int, ...],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> tuple[_ReceiverMergeMove | None, bool]:
    best_move: _ReceiverMergeMove | None = None
    best_objective: tuple[int, int, int, int, tuple[str, ...]] | None = None
    completed = True
    removed_low_signatures = {row.evaluated.candidate.signature for row in low_rows}

    for receiver_choices in product(range(len(receiver_rows)), repeat=len(low_case_indexes)):
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            break
        case_to_receiver = {
            testcase_index: receiver_rows[receiver_index]
            for testcase_index, receiver_index in zip(low_case_indexes, receiver_choices)
        }
        trial = _receiver_merge_trial(evaluation, receiver_rows, case_to_receiver, removed_low_signatures, evaluator)
        best_move, best_objective = _choose_receiver_merge_move(
            best_move,
            best_objective,
            trial,
            evaluation,
            starting_evaluation,
            options,
        )

    return best_move, completed


def _best_greedy_receiver_merge_move(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    low_rows: tuple[AssignedCandidate, ...],
    receiver_rows: tuple[AssignedCandidate, ...],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> tuple[_ReceiverMergeMove | None, bool]:
    best_move: _ReceiverMergeMove | None = None
    best_objective: tuple[int, int, int, int, tuple[str, ...]] | None = None
    completed = True

    unassigned_move, _completed = _best_remove_unassigned_low_specs(evaluation, starting_evaluation, evaluator, options)
    if unassigned_move is not None:
        best_move, best_objective = _choose_receiver_merge_move(
            best_move,
            best_objective,
            unassigned_move,
            evaluation,
            starting_evaluation,
            options,
        )

    for low_row in sorted(low_rows, key=lambda row: row.evaluated.output_signature):
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            break
        if not low_row.assigned_indexes:
            continue
        if not _combination_count_exceeds(len(receiver_rows), len(low_row.assigned_indexes), options.max_low_use_merge_combinations):
            row_move, row_completed = _best_exact_receiver_merge_move(
                evaluation,
                starting_evaluation,
                (low_row,),
                receiver_rows,
                tuple(sorted(low_row.assigned_indexes)),
                evaluator,
                options,
                deadline,
            )
            completed = completed and row_completed
        else:
            row_move, row_completed = _best_greedy_low_row_merge_move(
                evaluation,
                starting_evaluation,
                low_row,
                receiver_rows,
                evaluator,
                options,
                deadline,
            )
            completed = completed and row_completed
        best_move, best_objective = _choose_receiver_merge_move(
            best_move,
            best_objective,
            row_move,
            evaluation,
            starting_evaluation,
            options,
        )

    return best_move, completed


def _best_greedy_low_row_merge_move(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    low_row: AssignedCandidate,
    receiver_rows: tuple[AssignedCandidate, ...],
    evaluator: SolutionEvaluator,
    options: SolveOptions,
    deadline: float | None,
) -> tuple[_ReceiverMergeMove | None, bool]:
    case_to_receiver: dict[int, AssignedCandidate] = {}
    completed = True
    for testcase_index in sorted(low_row.assigned_indexes):
        if deadline is not None and not _has_refinement_time(deadline):
            completed = False
            break
        best_partial: _ReceiverMergeMove | None = None
        best_receiver: AssignedCandidate | None = None
        best_partial_objective: tuple[int, int, tuple[str, ...]] | None = None
        for receiver_row in receiver_rows:
            trial_mapping = dict(case_to_receiver)
            trial_mapping[testcase_index] = receiver_row
            trial = _receiver_merge_trial(
                evaluation,
                receiver_rows,
                trial_mapping,
                removed_low_signatures=set(),
                evaluator=evaluator,
            )
            if trial is None or not _receiver_merge_is_affordable(trial.evaluation, starting_evaluation, options):
                continue
            objective = _receiver_merge_partial_objective(trial.evaluation, starting_evaluation)
            if best_partial_objective is None or objective < best_partial_objective:
                best_partial = trial
                best_receiver = receiver_row
                best_partial_objective = objective
        if best_partial is None or best_receiver is None:
            return None, completed
        case_to_receiver[testcase_index] = best_receiver

    if not completed:
        return None, False
    trial = _receiver_merge_trial(
        evaluation,
        receiver_rows,
        case_to_receiver,
        removed_low_signatures={low_row.evaluated.candidate.signature},
        evaluator=evaluator,
    )
    if trial is None or not _receiver_merge_move_is_accepted(trial.evaluation, evaluation, starting_evaluation, options):
        return None, completed
    return trial, completed


def _choose_receiver_merge_move(
    best_move: _ReceiverMergeMove | None,
    best_objective: tuple[int, int, int, int, tuple[str, ...]] | None,
    trial: _ReceiverMergeMove | None,
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    options: SolveOptions,
) -> tuple[_ReceiverMergeMove | None, tuple[int, int, int, int, tuple[str, ...]] | None]:
    if trial is None or not _receiver_merge_move_is_accepted(trial.evaluation, evaluation, starting_evaluation, options):
        return best_move, best_objective
    objective = _receiver_merge_objective(trial.evaluation, starting_evaluation)
    if best_objective is None or objective < best_objective:
        return trial, objective
    return best_move, best_objective


def _receiver_merge_trial(
    evaluation: SolutionEvaluation,
    receiver_rows: tuple[AssignedCandidate, ...],
    case_to_receiver: dict[int, AssignedCandidate],
    removed_low_signatures: set[str],
    evaluator: SolutionEvaluator,
) -> _ReceiverMergeMove | None:
    current_candidates = tuple(row.evaluated.candidate for row in evaluation.rows)
    current_assignments = _assignments_from_evaluation(evaluation)
    receiver_cases: dict[str, list[int]] = {}
    for testcase_index, receiver_row in case_to_receiver.items():
        receiver_cases.setdefault(receiver_row.evaluated.candidate.signature, []).append(testcase_index)

    updated_receivers: dict[str, Candidate] = {}
    for receiver_row in receiver_rows:
        testcase_indexes = tuple(sorted(receiver_cases.get(receiver_row.evaluated.candidate.signature, ())))
        if not testcase_indexes:
            continue
        updated = _receiver_candidate_with_cases(receiver_row.evaluated.candidate, testcase_indexes, evaluator)
        if updated is None:
            return None
        updated_receivers[receiver_row.evaluated.candidate.signature] = updated

    removed_signatures = set(removed_low_signatures) | set(updated_receivers)
    trial_candidates = _replace_selected_candidates(current_candidates, removed_signatures, tuple(updated_receivers.values()))
    selected_by_signature = {candidate.signature: candidate for candidate in trial_candidates}
    updated_by_original_signature = {
        original_signature: selected_by_signature[updated.signature]
        for original_signature, updated in updated_receivers.items()
    }

    trial_assignments: dict[int, Candidate] = {}
    for testcase_index, assigned_candidate in current_assignments.items():
        if testcase_index in case_to_receiver:
            original_signature = case_to_receiver[testcase_index].evaluated.candidate.signature
            trial_assignments[testcase_index] = updated_by_original_signature[original_signature]
        elif assigned_candidate.signature in updated_by_original_signature:
            trial_assignments[testcase_index] = updated_by_original_signature[assigned_candidate.signature]
        elif assigned_candidate.signature in removed_low_signatures:
            return None
        else:
            trial_assignments[testcase_index] = selected_by_signature.get(assigned_candidate.signature, assigned_candidate)

    try:
        trial_evaluation = evaluator.evaluate(trial_candidates, trial_assignments)
    except ValueError:
        return None
    return _ReceiverMergeMove(trial_candidates, trial_assignments, trial_evaluation)


def _receiver_candidate_with_cases(
    receiver: Candidate,
    testcase_indexes: tuple[int, ...],
    evaluator: SolutionEvaluator,
) -> Candidate | None:
    columns = evaluator.coverage_index.columns
    merged_spec = receiver.spec
    source_indexes = set(receiver.source_indexes)
    for testcase_index in testcase_indexes:
        testcase_spec = exact_spec(evaluator.parsed.rows[testcase_index].tokens, columns)
        merged_spec = merge_specs(merged_spec, testcase_spec, columns)
        source_indexes.add(testcase_index)
    return candidate_from_spec(merged_spec, tuple(sorted(source_indexes)), evaluator.coverage_index)


def _receiver_merge_move_is_accepted(
    trial_evaluation: SolutionEvaluation,
    current_evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    options: SolveOptions,
) -> bool:
    if (trial_evaluation.low_use_spec_count, trial_evaluation.low_use_deficit) >= (
        current_evaluation.low_use_spec_count,
        current_evaluation.low_use_deficit,
    ):
        return False
    return _receiver_merge_is_affordable(trial_evaluation, starting_evaluation, options)


def _receiver_merge_is_affordable(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
    options: SolveOptions,
) -> bool:
    return evaluation.total_equipment - starting_evaluation.total_equipment <= options.low_use_affordable_equipment_delta


def _receiver_merge_objective(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
) -> tuple[int, int, int, int, tuple[str, ...]]:
    return (
        evaluation.low_use_spec_count,
        evaluation.low_use_deficit,
        evaluation.total_equipment - starting_evaluation.total_equipment,
        evaluation.selected_spec_count,
        tuple(sorted(row.evaluated.output_signature for row in evaluation.rows)),
    )


def _receiver_merge_partial_objective(
    evaluation: SolutionEvaluation,
    starting_evaluation: SolutionEvaluation,
) -> tuple[int, int, tuple[str, ...]]:
    return (
        evaluation.total_equipment - starting_evaluation.total_equipment,
        evaluation.selected_spec_count,
        tuple(sorted(row.evaluated.output_signature for row in evaluation.rows)),
    )


def _combination_count_exceeds(base: int, exponent: int, limit: int) -> bool:
    count = 1
    for _index in range(exponent):
        count *= base
        if count > limit:
            return True
    return False


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


def _assignments_from_evaluation(evaluation: SolutionEvaluation) -> dict[int, Candidate]:
    assignments: dict[int, Candidate] = {}
    for row in evaluation.rows:
        for testcase_index in row.assigned_indexes:
            assignments[testcase_index] = row.evaluated.candidate
    return assignments


def _solution_changed(solution: Solution, result: _LowUseMergeResult) -> bool:
    if tuple(candidate.signature for candidate in solution.candidates) != tuple(candidate.signature for candidate in result.candidates):
        return True
    return {
        index: candidate.signature for index, candidate in solution.assignments.items()
    } != {
        index: candidate.signature for index, candidate in result.assignments.items()
    }


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
