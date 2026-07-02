"""OR-Tools CP-SAT optimizer for the generated candidate pool."""

from __future__ import annotations

from dataclasses import dataclass
import os
import time

from .models import Candidate, Solution
from .optimizer import (
    _IndexedCandidate,
    _assign,
    _greedy,
    _index_candidates,
    _iter_bits,
    _prune_dominated_same_coverage,
)


class OrtoolsUnavailableError(RuntimeError):
    """Raised when the optional OR-Tools dependency is not importable."""


@dataclass(frozen=True)
class _ModelParts:
    model: object
    selected: list[object]
    equipment_expr: object
    excess_expr: object
    count_expr: object
    primary_expr: object


def optimize(
    candidates: tuple[Candidate, ...],
    testcase_count: int,
    timeout_seconds: float,
    *,
    solver_threads: int | None = None,
) -> Solution:
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:
        raise OrtoolsUnavailableError("OR-Tools is not installed; install requirements.txt or use --solver stdlib") from exc

    if not candidates and testcase_count:
        raise ValueError("no candidates generated")

    deadline = time.monotonic() + timeout_seconds
    indexed, weights = _index_candidates(candidates, testcase_count)
    indexed = _prune_dominated_same_coverage(indexed)
    by_case = _covering_candidates(indexed, len(weights))
    greedy_indexes = tuple(_greedy(indexed, weights, (1 << len(weights)) - 1, tuple(range(len(indexed)))))

    parts = _build_model(cp_model, indexed, weights, by_case)
    parts.model.Minimize(parts.primary_expr)

    for candidate_index in greedy_indexes:
        parts.model.AddHint(parts.selected[candidate_index], 1)
    for candidate_index in range(len(indexed)):
        if candidate_index not in greedy_indexes:
            parts.model.AddHint(parts.selected[candidate_index], 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.0, deadline - time.monotonic())
    solver.parameters.num_search_workers = _thread_count(solver_threads)
    solver.parameters.random_seed = 0

    status = solver.Solve(parts.model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        selected_indexes = tuple(index for index, variable in enumerate(parts.selected) if solver.BooleanValue(variable))
        status_name = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"
    else:
        selected_indexes = greedy_indexes
        status_name = "FEASIBLE"

    if status == cp_model.OPTIMAL:
        objective_fields = _objective_fields(indexed, selected_indexes, weights)
        refined = _refine_signature_tie(
            cp_model,
            indexed,
            weights,
            by_case,
            objective_fields,
            deadline,
            solver_threads,
        )
        if refined is None:
            status_name = "FEASIBLE"
        else:
            selected_indexes = refined

    selected_candidates = tuple(indexed[index].candidate for index in selected_indexes)
    return Solution(candidates=selected_candidates, assignments=_assign(selected_candidates, testcase_count), status=status_name)


def _build_model(
    cp_model,
    indexed: tuple[_IndexedCandidate, ...],
    weights: tuple[int, ...],
    by_case: list[list[int]],
    *,
    forced_in: frozenset[int] = frozenset(),
    forced_out: frozenset[int] = frozenset(),
    objective_fields: tuple[int, int, int] | None = None,
) -> _ModelParts:
    model = cp_model.CpModel()
    selected = [model.NewBoolVar(f"candidate_{index}") for index in range(len(indexed))]
    for case_index, covering in enumerate(by_case):
        if not covering:
            raise ValueError(f"testcase index {case_index} is not coverable")
        model.Add(sum(selected[candidate_index] for candidate_index in covering) >= 1)

    for candidate_index in forced_in:
        model.Add(selected[candidate_index] == 1)
    for candidate_index in forced_out:
        model.Add(selected[candidate_index] == 0)

    max_weighted_excess = 0
    threshold_terms: list[tuple[int, object]] = []
    for case_index, covering in enumerate(by_case):
        levels = sorted({indexed[candidate_index].assignment_excess.get(case_index, 0) for candidate_index in covering})
        if not levels:
            continue
        max_excess = levels[-1]
        max_weighted_excess += weights[case_index] * max_excess
        for lower, upper in zip(levels, levels[1:]):
            eligible = [
                selected[candidate_index]
                for candidate_index in covering
                if indexed[candidate_index].assignment_excess.get(case_index, 0) <= lower
            ]
            threshold = model.NewBoolVar(f"case_{case_index}_excess_le_{lower}")
            model.AddMaxEquality(threshold, eligible)
            threshold_terms.append((-weights[case_index] * (upper - lower), threshold))

    equipment_expr = sum(indexed_candidate.candidate.equipment_count * selected[candidate_index] for candidate_index, indexed_candidate in enumerate(indexed))
    count_expr = sum(selected)
    excess_expr = max_weighted_excess + sum(coefficient * threshold for coefficient, threshold in threshold_terms)

    excess_scale = len(indexed) + 1
    equipment_scale = (max_weighted_excess + 1) * excess_scale + len(indexed) + 1
    primary_expr = equipment_scale * equipment_expr + excess_scale * excess_expr + count_expr

    if objective_fields is not None:
        equipment, excess, count = objective_fields
        model.Add(equipment_expr == equipment)
        model.Add(excess_expr == excess)
        model.Add(count_expr == count)

    return _ModelParts(
        model=model,
        selected=selected,
        equipment_expr=equipment_expr,
        excess_expr=excess_expr,
        count_expr=count_expr,
        primary_expr=primary_expr,
    )


def _refine_signature_tie(
    cp_model,
    indexed: tuple[_IndexedCandidate, ...],
    weights: tuple[int, ...],
    by_case: list[list[int]],
    objective_fields: tuple[int, int, int],
    deadline: float,
    solver_threads: int | None,
) -> tuple[int, ...] | None:
    target_count = objective_fields[2]
    forced_in: set[int] = set()
    forced_out: set[int] = set()
    sorted_indexes = tuple(sorted(range(len(indexed)), key=lambda index: indexed[index].candidate.signature))

    for candidate_index in sorted_indexes:
        if len(forced_in) == target_count:
            return tuple(sorted(forced_in))
        if time.monotonic() >= deadline:
            return None
        candidate_in = frozenset((*forced_in, candidate_index))
        parts = _build_model(
            cp_model,
            indexed,
            weights,
            by_case,
            forced_in=candidate_in,
            forced_out=frozenset(forced_out),
            objective_fields=objective_fields,
        )
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.0, deadline - time.monotonic())
        solver.parameters.num_search_workers = _thread_count(solver_threads)
        solver.parameters.random_seed = 0
        status = solver.Solve(parts.model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            forced_in.add(candidate_index)
        elif status == cp_model.INFEASIBLE:
            forced_out.add(candidate_index)
        else:
            return None

    if len(forced_in) == target_count:
        return tuple(sorted(forced_in))
    return None


def _objective_fields(indexed: tuple[_IndexedCandidate, ...], selected_indexes: tuple[int, ...], weights: tuple[int, ...]) -> tuple[int, int, int]:
    selected = tuple(indexed[index] for index in selected_indexes)
    equipment = sum(candidate.candidate.equipment_count for candidate in selected)
    excess = 0
    for case_index, weight in enumerate(weights):
        best = min(
            (candidate.assignment_excess.get(case_index, 0) for candidate in selected if candidate.coverage_mask & (1 << case_index)),
            default=0,
        )
        excess += weight * best
    return equipment, excess, len(selected_indexes)


def _covering_candidates(indexed: tuple[_IndexedCandidate, ...], case_count: int) -> list[list[int]]:
    by_case: list[list[int]] = [[] for _ in range(case_count)]
    for candidate_index, indexed_candidate in enumerate(indexed):
        for case_index in _iter_bits(indexed_candidate.coverage_mask):
            by_case[case_index].append(candidate_index)
    return by_case


def _thread_count(solver_threads: int | None) -> int:
    if solver_threads is not None:
        return max(1, solver_threads)
    return max(1, os.cpu_count() or 1)
