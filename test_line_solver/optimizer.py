"""Standard-library candidate-pool optimizer."""

from __future__ import annotations

from dataclasses import dataclass
import time

from .models import Candidate, Solution


@dataclass(frozen=True)
class _IndexedCandidate:
    candidate: Candidate
    coverage_mask: int
    assignment_excess: dict[int, int]


def optimize(
    candidates: tuple[Candidate, ...],
    testcase_count: int,
    timeout_seconds: float,
    *,
    _disable_pruning: bool = False,
) -> Solution:
    deadline = time.monotonic() + timeout_seconds
    if not candidates and testcase_count:
        raise ValueError("no candidates generated")

    indexed, weights = _index_candidates(candidates, testcase_count)
    if not _disable_pruning:
        indexed = _prune_dominated_same_coverage(indexed)
    case_count = len(weights)
    all_cases_mask = (1 << case_count) - 1

    by_case: list[list[int]] = [[] for _ in range(case_count)]
    for candidate_index, indexed_candidate in enumerate(indexed):
        for case_index in _iter_bits(indexed_candidate.coverage_mask):
            by_case[case_index].append(candidate_index)
    for index, covering in enumerate(by_case):
        if not covering:
            raise ValueError(f"testcase index {index} is not coverable")
        covering.sort(
            key=lambda candidate_index: (
                indexed[candidate_index].candidate.equipment_count,
                indexed[candidate_index].assignment_excess.get(index, 0),
                -_weighted_count(indexed[candidate_index].coverage_mask, weights),
                indexed[candidate_index].candidate.signature,
            )
        )

    selected_parts: list[int] = []
    timed_out = False
    for component_case_mask, component_candidates in _components(indexed, by_case, all_cases_mask):
        component_selected, component_timed_out = _optimize_component(
            indexed,
            weights,
            by_case,
            component_case_mask,
            component_candidates,
            deadline,
        )
        selected_parts.extend(component_selected)
        timed_out = timed_out or component_timed_out
    status = "FEASIBLE_TIMEOUT" if timed_out else "OPTIMAL"
    best_selected_indexes = tuple(selected_parts)
    best_selected = tuple(indexed[index].candidate for index in best_selected_indexes)
    assignments = _assign(best_selected, testcase_count)
    return Solution(candidates=best_selected, assignments=assignments, status=status)


def _index_candidates(candidates: tuple[Candidate, ...], testcase_count: int) -> tuple[tuple[_IndexedCandidate, ...], tuple[int, ...]]:
    group_weights = candidates[0].group_weights if candidates else ()
    if group_weights and all(candidate.group_weights == group_weights and candidate.group_coverage_mask for candidate in candidates):
        weights = group_weights
        indexed = tuple(
            _IndexedCandidate(
                candidate=candidate,
                coverage_mask=candidate.group_coverage_mask,
                assignment_excess=candidate.group_assignment_excess,
            )
            for candidate in candidates
        )
        return indexed, weights

    weights = tuple(1 for _ in range(testcase_count))
    indexed_candidates: list[_IndexedCandidate] = []
    for candidate in candidates:
        coverage_mask = 0
        for index in candidate.coverage:
            coverage_mask |= 1 << index
        indexed_candidates.append(
            _IndexedCandidate(
                candidate=candidate,
                coverage_mask=coverage_mask,
                assignment_excess=candidate.assignment_excess,
            )
        )
    return tuple(indexed_candidates), weights


def _prune_dominated_same_coverage(indexed: tuple[_IndexedCandidate, ...]) -> tuple[_IndexedCandidate, ...]:
    by_coverage: dict[int, list[_IndexedCandidate]] = {}
    for indexed_candidate in indexed:
        by_coverage.setdefault(indexed_candidate.coverage_mask, []).append(indexed_candidate)

    kept: list[_IndexedCandidate] = []
    for coverage_mask, bucket in by_coverage.items():
        if coverage_mask == 0:
            continue
        nondominated: list[_IndexedCandidate] = []
        for candidate in bucket:
            if any(_dominates_with_same_coverage(other, candidate, coverage_mask) for other in nondominated):
                continue
            nondominated = [
                other
                for other in nondominated
                if not _dominates_with_same_coverage(candidate, other, coverage_mask)
            ]
            nondominated.append(candidate)
        kept.extend(nondominated)
    return tuple(sorted(kept, key=lambda item: item.candidate.signature))


def _dominates_with_same_coverage(left: _IndexedCandidate, right: _IndexedCandidate, coverage_mask: int) -> bool:
    if left.candidate.equipment_count > right.candidate.equipment_count:
        return False
    if left.candidate.equipment_count == right.candidate.equipment_count and left.candidate.signature > right.candidate.signature:
        return False
    for case_index in _iter_bits(coverage_mask):
        if left.assignment_excess.get(case_index, 0) > right.assignment_excess.get(case_index, 0):
            return False
    return True


def _optimize_component(
    indexed: tuple[_IndexedCandidate, ...],
    weights: tuple[int, ...],
    by_case: list[list[int]],
    component_case_mask: int,
    component_candidates: tuple[int, ...],
    deadline: float,
) -> tuple[tuple[int, ...], bool]:
    incumbent = _greedy(indexed, weights, component_case_mask, component_candidates)
    best_selected_indexes = tuple(incumbent)
    best_objective = _objective(indexed, best_selected_indexes, weights, component_case_mask)
    selected: list[int] = []
    selected_set: set[int] = set()
    best_excess_by_case: list[int | None] = [None for _ in range(len(weights))]
    timed_out = False

    def visit(uncovered_mask: int, start_equipment: int, current_excess: int) -> None:
        nonlocal best_selected_indexes, best_objective, timed_out
        if time.monotonic() >= deadline:
            timed_out = True
            return
        if not uncovered_mask:
            objective = (
                start_equipment,
                current_excess,
                len(selected),
                tuple(sorted(indexed[index].candidate.signature for index in selected)),
            )
            if objective < best_objective:
                best_objective = objective
                best_selected_indexes = tuple(selected)
            return
        if start_equipment > best_objective[0]:
            return

        target = min(_iter_bits(uncovered_mask), key=lambda index: len(by_case[index]))
        for candidate_index in by_case[target]:
            if timed_out:
                return
            if candidate_index in selected_set:
                continue
            indexed_candidate = indexed[candidate_index]
            selected.append(candidate_index)
            selected_set.add(candidate_index)
            next_excess, undo = _apply_assignment_excess(indexed_candidate, best_excess_by_case, current_excess, weights)
            visit(
                uncovered_mask & ~indexed_candidate.coverage_mask,
                start_equipment + indexed_candidate.candidate.equipment_count,
                next_excess,
            )
            _undo_assignment_excess(best_excess_by_case, undo)
            selected_set.remove(candidate_index)
            selected.pop()

    visit(component_case_mask, 0, 0)
    return best_selected_indexes, timed_out


def _greedy(
    indexed: tuple[_IndexedCandidate, ...],
    weights: tuple[int, ...],
    case_mask: int,
    candidate_indexes: tuple[int, ...],
) -> list[int]:
    uncovered_mask = case_mask
    selected: list[int] = []
    while uncovered_mask:
        best = min(
            (index for index in candidate_indexes if indexed[index].coverage_mask & uncovered_mask),
            key=lambda index: _greedy_key(indexed[index], uncovered_mask, weights),
        )
        selected.append(best)
        uncovered_mask &= ~indexed[best].coverage_mask
    return selected


def _greedy_key(indexed_candidate: _IndexedCandidate, uncovered_mask: int, weights: tuple[int, ...]) -> tuple[float, int, int, str]:
    newly_covered = indexed_candidate.coverage_mask & uncovered_mask
    newly_covered_weight = _weighted_count(newly_covered, weights)
    weighted_excess = sum(
        weights[index] * indexed_candidate.assignment_excess.get(index, 0)
        for index in _iter_bits(newly_covered)
    )
    return (
        indexed_candidate.candidate.equipment_count / max(1, newly_covered_weight),
        weighted_excess,
        -newly_covered_weight,
        indexed_candidate.candidate.signature,
    )


def _objective(
    indexed: tuple[_IndexedCandidate, ...],
    selected_indexes: tuple[int, ...],
    weights: tuple[int, ...],
    case_mask: int,
) -> tuple[int, int, int, tuple[str, ...]]:
    total_equipment = sum(indexed[index].candidate.equipment_count for index in selected_indexes)
    total_excess = 0
    for case_index in _iter_bits(case_mask):
        weight = weights[case_index]
        covering = [
            indexed[index]
            for index in selected_indexes
            if indexed[index].coverage_mask & (1 << case_index)
        ]
        best = min(
            covering,
            key=lambda indexed_candidate: (
                indexed_candidate.assignment_excess.get(case_index, 0),
                indexed_candidate.candidate.equipment_count,
                indexed_candidate.candidate.signature,
            ),
        )
        total_excess += weight * best.assignment_excess.get(case_index, 0)
    return (
        total_equipment,
        total_excess,
        len(selected_indexes),
        tuple(sorted(indexed[index].candidate.signature for index in selected_indexes)),
    )


def _components(
    indexed: tuple[_IndexedCandidate, ...],
    by_case: list[list[int]],
    all_cases_mask: int,
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    components: list[tuple[int, tuple[int, ...]]] = []
    remaining_cases = all_cases_mask
    seen_candidates: set[int] = set()

    while remaining_cases:
        start_case = (remaining_cases & -remaining_cases).bit_length() - 1
        case_queue = [start_case]
        component_case_mask = 0
        component_candidates: set[int] = set()

        while case_queue:
            case_index = case_queue.pop()
            case_bit = 1 << case_index
            if component_case_mask & case_bit:
                continue
            component_case_mask |= case_bit
            remaining_cases &= ~case_bit
            for candidate_index in by_case[case_index]:
                if candidate_index in seen_candidates:
                    continue
                seen_candidates.add(candidate_index)
                component_candidates.add(candidate_index)
                for next_case in _iter_bits(indexed[candidate_index].coverage_mask):
                    if not component_case_mask & (1 << next_case):
                        case_queue.append(next_case)

        components.append((component_case_mask, tuple(sorted(component_candidates))))
    return tuple(components)


def _apply_assignment_excess(
    indexed_candidate: _IndexedCandidate,
    best_excess_by_case: list[int | None],
    current_excess: int,
    weights: tuple[int, ...],
) -> tuple[int, list[tuple[int, int | None]]]:
    undo: list[tuple[int, int | None]] = []
    for case_index in _iter_bits(indexed_candidate.coverage_mask):
        candidate_excess = indexed_candidate.assignment_excess.get(case_index, 0)
        previous_excess = best_excess_by_case[case_index]
        if previous_excess is not None and candidate_excess >= previous_excess:
            continue
        undo.append((case_index, previous_excess))
        best_excess_by_case[case_index] = candidate_excess
        current_excess += weights[case_index] * candidate_excess
        if previous_excess is not None:
            current_excess -= weights[case_index] * previous_excess
    return current_excess, undo


def _undo_assignment_excess(best_excess_by_case: list[int | None], undo: list[tuple[int, int | None]]) -> None:
    for case_index, previous_excess in reversed(undo):
        best_excess_by_case[case_index] = previous_excess


def _assign(selected: tuple[Candidate, ...], testcase_count: int) -> dict[int, Candidate]:
    assignments: dict[int, Candidate] = {}
    for index in range(testcase_count):
        covering = [candidate for candidate in selected if index in candidate.coverage]
        assignments[index] = min(covering, key=lambda candidate: (candidate.assignment_excess[index], candidate.equipment_count, candidate.signature))
    return assignments


def _iter_bits(mask: int):
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


def _weighted_count(mask: int, weights: tuple[int, ...]) -> int:
    if all(weight == 1 for weight in weights):
        return mask.bit_count()
    return sum(weights[index] for index in _iter_bits(mask))
