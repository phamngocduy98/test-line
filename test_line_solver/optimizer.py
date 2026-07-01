"""Standard-library candidate-pool optimizer."""

from __future__ import annotations

import time

from .models import Candidate, Solution


def optimize(candidates: tuple[Candidate, ...], testcase_count: int, timeout_seconds: float) -> Solution:
    deadline = time.monotonic() + timeout_seconds
    if not candidates and testcase_count:
        raise ValueError("no candidates generated")

    by_testcase: list[list[Candidate]] = [[] for _ in range(testcase_count)]
    for candidate in candidates:
        for index in candidate.coverage:
            by_testcase[index].append(candidate)
    for index, covering in enumerate(by_testcase):
        if not covering:
            raise ValueError(f"testcase index {index} is not coverable")
        covering.sort(key=lambda candidate: (candidate.equipment_count, candidate.assignment_excess[index], candidate.signature))

    incumbent = _greedy(candidates, testcase_count)
    best_selected = tuple(incumbent)
    best_objective = _objective(best_selected, testcase_count)
    selected: list[Candidate] = []
    timed_out = False

    def visit(covered: set[int], start_equipment: int) -> None:
        nonlocal best_selected, best_objective, timed_out
        if time.monotonic() >= deadline:
            timed_out = True
            return
        if len(covered) == testcase_count:
            objective = _objective(tuple(selected), testcase_count)
            if objective < best_objective:
                best_objective = objective
                best_selected = tuple(selected)
            return
        if start_equipment > best_objective[0]:
            return

        uncovered = [index for index in range(testcase_count) if index not in covered]
        target = min(uncovered, key=lambda index: len(by_testcase[index]))
        for candidate in by_testcase[target]:
            if timed_out:
                return
            if candidate in selected:
                continue
            selected.append(candidate)
            visit(covered | set(candidate.coverage), start_equipment + candidate.equipment_count)
            selected.pop()

    visit(set(), 0)
    status = "FEASIBLE_TIMEOUT" if timed_out else "OPTIMAL"

    assignments = _assign(best_selected, testcase_count)
    return Solution(candidates=best_selected, assignments=assignments, status=status)


def _greedy(candidates: tuple[Candidate, ...], testcase_count: int) -> list[Candidate]:
    uncovered = set(range(testcase_count))
    selected: list[Candidate] = []
    while uncovered:
        best = min(
            (candidate for candidate in candidates if candidate.coverage & uncovered),
            key=lambda candidate: (
                candidate.equipment_count / max(1, len(candidate.coverage & uncovered)),
                sum(candidate.assignment_excess[index] for index in candidate.coverage & uncovered),
                -len(candidate.coverage & uncovered),
                candidate.signature,
            ),
        )
        selected.append(best)
        uncovered -= set(best.coverage)
    return selected


def _objective(selected: tuple[Candidate, ...], testcase_count: int) -> tuple[int, int, int, tuple[str, ...]]:
    assignments = _assign(selected, testcase_count)
    total_equipment = sum(candidate.equipment_count for candidate in selected)
    total_excess = sum(assignments[index].assignment_excess[index] for index in range(testcase_count))
    return (total_equipment, total_excess, len(selected), tuple(sorted(candidate.signature for candidate in selected)))


def _assign(selected: tuple[Candidate, ...], testcase_count: int) -> dict[int, Candidate]:
    assignments: dict[int, Candidate] = {}
    for index in range(testcase_count):
        covering = [candidate for candidate in selected if index in candidate.coverage]
        assignments[index] = min(covering, key=lambda candidate: (candidate.assignment_excess[index], candidate.equipment_count, candidate.signature))
    return assignments
