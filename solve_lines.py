#!/usr/bin/env python3
"""Direct line-packing optimizer for telecom testcase requirements.

This is the Approach 2B implementation from review.md: testcases are assigned
straight to physical test lines, each line spec is the merge of its assigned
cases, and a local-search optimizer improves the line assignment directly.

The older solve_test_lines.py set-cover solver is intentionally left unchanged.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from solve_test_lines import (
    DU_COLUMNS,
    RELATION_TOKENS,
    RU_COLUMN,
    UE_COLUMN,
    Candidate,
    RuBandSupport,
    TestCase,
    alternatives,
    coverage_delta,
    equipment_count,
    is_any,
    is_temporarily_ignored_column,
    load_cases,
    load_ru_band_support,
    merge_cases,
    numeric_equipment,
    parse_cell,
    render_cell,
    resolve_compatibility_variants,
    spec_has_compatible_ru_bands,
    split_band_tokens,
    validate_support_references,
)


DEFAULT_MAX_COMPATIBILITY_VARIANTS = 128


@dataclass(frozen=True)
class EquipmentWeights:
    """Weights used by the direct line-packing objective."""

    du: float = 1.0
    ru: float = 1.0
    ue: float = 1.0


@dataclass(frozen=True)
class EquipmentBreakdown:
    """Unweighted equipment units in a rendered line spec."""

    du: int = 0
    ru: int = 0
    ue: int = 0


@dataclass(frozen=True)
class MergeCost:
    """Pairwise merge cost used by the clustering warm start."""

    compatible: bool
    delta_du: int = 0
    delta_ru: int = 0
    delta_ue: int = 0
    delta_cost: float = math.inf
    reason: str = ""


@dataclass(frozen=True)
class GraphEdge:
    """A bounded compatibility-graph edge."""

    source: int
    target: int
    cost: MergeCost


@dataclass(frozen=True)
class LineState:
    """A physical test line and its recomputed merged spec."""

    case_indices: tuple[int, ...]
    spec: dict[str, tuple[str, ...]]
    breakdown: EquipmentBreakdown
    cost: float


@dataclass(frozen=True)
class LocalSearchResult:
    """Final local-search result and summary counters."""

    lines: tuple[LineState, ...]
    total_cost: float
    iterations: int
    accepted_moves: int
    transfer_moves: int
    swap_moves: int
    merge_moves: int
    started_cost: float
    solve_status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pack testcase requirements directly into physical test lines."
    )
    parser.add_argument("--input", default="input.csv", help="Input testcase CSV path.")
    parser.add_argument(
        "--output",
        default="line_output_specs.csv",
        help="Output line specs CSV path. Default: line_output_specs.csv",
    )
    parser.add_argument(
        "--ru-band-support",
        required=True,
        help="Required CSV mapping RUs to supported LTE and NR bands.",
    )
    parser.add_argument(
        "--max-cases-per-line",
        type=int,
        required=True,
        help="Maximum number of testcases assigned to one physical line.",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=0,
        help="Optional upper bound on physical lines. 0 means unbounded.",
    )
    parser.add_argument(
        "--du-weight",
        type=float,
        default=1.0,
        help="Weight for DU equipment units. Default: 1.0.",
    )
    parser.add_argument(
        "--ru-weight",
        type=float,
        default=1.0,
        help="Weight for RU slots. Default: 1.0.",
    )
    parser.add_argument(
        "--ue-weight",
        type=float,
        default=1.0,
        help="Weight for UE equipment units. Default: 1.0.",
    )
    parser.add_argument(
        "--search-time-limit",
        type=float,
        default=60.0,
        help="Local-search time budget in seconds. Default: 60.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=200,
        help="Maximum local-search passes. Default: 200.",
    )
    parser.add_argument(
        "--initial-strategy",
        choices=("greedy", "cluster", "random"),
        default="cluster",
        help="Warm-start assignment strategy. Default: cluster.",
    )
    parser.add_argument(
        "--max-neighbors",
        type=int,
        default=30,
        help="Compatibility-graph neighbors retained per testcase. Default: 30.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help=(
            "Initial simulated-annealing temperature. 0 disables worse-move "
            "acceptance and uses strict-improvement local search."
        ),
    )
    parser.add_argument(
        "--cooling",
        type=float,
        default=0.995,
        help="Temperature multiplier after each accepted worse move. Default: 0.995.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1,
        help="Random seed for random warm starts and annealing. Default: 1.",
    )
    parser.add_argument(
        "--ignore-tech-and-ue-capa",
        action="store_true",
        help=(
            "Ignore all tech and ue capa columns during optimization. "
            "Ignored columns are blank in the output."
        ),
    )
    parser.add_argument(
        "--max-compatibility-variants",
        type=int,
        default=DEFAULT_MAX_COMPATIBILITY_VARIANTS,
        help="Maximum RU/band wildcard realizations considered per line. Default: 128.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print accepted local-search moves and line details.",
    )
    return parser.parse_args()


def equipment_breakdown(
    requirement_columns: Sequence[str],
    spec: dict[str, tuple[str, ...]],
) -> EquipmentBreakdown:
    du = sum(
        numeric_equipment(spec.get(column, ()))
        for column in DU_COLUMNS
        if column in requirement_columns
    )
    ru = len(spec.get(RU_COLUMN, ())) if RU_COLUMN in requirement_columns else 0
    ue = numeric_equipment(spec.get(UE_COLUMN, ())) if UE_COLUMN in requirement_columns else 0
    return EquipmentBreakdown(du=du, ru=ru, ue=ue)


def line_cost(
    spec: dict[str, tuple[str, ...]],
    requirement_columns: Sequence[str],
    weights: EquipmentWeights,
) -> float:
    breakdown = equipment_breakdown(requirement_columns, spec)
    return (
        weights.du * breakdown.du
        + weights.ru * breakdown.ru
        + weights.ue * breakdown.ue
    )


def assignment_cost(lines: Iterable[LineState]) -> float:
    return sum(line.cost for line in lines)


def _selected_ru_keys(spec: dict[str, tuple[str, ...]], support: RuBandSupport) -> set[str]:
    ru_tokens = spec.get(RU_COLUMN, ())
    if ru_tokens:
        return {
            value
            for token in ru_tokens
            if not is_any(token)
            for value in alternatives(token)
            if value in support.ru_names
        }
    return set(support.ru_names)


def relations_are_physically_supported(
    spec: dict[str, tuple[str, ...]],
    support: RuBandSupport,
) -> bool:
    """Validate relation-only band requirements against selected RU capabilities."""

    selected_rus = _selected_ru_keys(spec, support)
    for column, support_by_ru in (
        ("lte band", support.lte_by_ru),
        ("nr band", support.nr_by_ru),
    ):
        tokens = spec.get(column, ())
        if not tokens:
            continue
        _, relations, _ = split_band_tokens(tokens)
        if not relations:
            continue
        supported_bands = set().union(
            *(support_by_ru.get(ru, frozenset()) for ru in selected_rus)
        )
        if "intra" in relations and not supported_bands:
            return False
        if "inter" in relations and len(supported_bands) < 2:
            return False
    return True


def line_spec_variants(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    case_indices: Iterable[int],
    support: RuBandSupport,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> list[dict[str, tuple[str, ...]]]:
    indices = tuple(sorted(set(case_indices)))
    if not indices:
        return []
    merged = merge_cases(list(requirement_columns), (cases[index] for index in indices))
    if merged is None:
        return []

    variants = resolve_compatibility_variants(
        merged,
        support,
        max_variants=max(1, max_compatibility_variants),
    )
    return [
        variant
        for variant in variants
        if spec_has_compatible_ru_bands(variant, support)
        and relations_are_physically_supported(variant, support)
    ]


def build_line_state(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    case_indices: Iterable[int],
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> LineState | None:
    indices = tuple(sorted(set(case_indices)))
    if not indices:
        return None
    variants = line_spec_variants(
        requirement_columns,
        cases,
        indices,
        support,
        max_compatibility_variants=max_compatibility_variants,
    )
    if not variants:
        return None
    best_spec = min(
        variants,
        key=lambda spec: (
            line_cost(spec, requirement_columns, weights),
            equipment_count(list(requirement_columns), spec),
            tuple((column, spec[column]) for column in sorted(spec)),
        ),
    )
    breakdown = equipment_breakdown(requirement_columns, best_spec)
    return LineState(
        case_indices=indices,
        spec=best_spec,
        breakdown=breakdown,
        cost=line_cost(best_spec, requirement_columns, weights),
    )


def _components_delta(left: EquipmentBreakdown, right: EquipmentBreakdown) -> tuple[int, int, int]:
    return (left.du - right.du, left.ru - right.ru, left.ue - right.ue)


def merge_cost(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    left_index: int,
    right_index: int,
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> MergeCost:
    left = build_line_state(
        requirement_columns,
        cases,
        [left_index],
        support,
        weights,
        max_compatibility_variants=max_compatibility_variants,
    )
    right = build_line_state(
        requirement_columns,
        cases,
        [right_index],
        support,
        weights,
        max_compatibility_variants=max_compatibility_variants,
    )
    merged = build_line_state(
        requirement_columns,
        cases,
        [left_index, right_index],
        support,
        weights,
        max_compatibility_variants=max_compatibility_variants,
    )
    if left is None or right is None:
        return MergeCost(False, reason="single_case_incompatible")
    if merged is None:
        return MergeCost(False, reason="merge_incompatible")

    base_breakdown = left.breakdown if left.cost >= right.cost else right.breakdown
    delta_du, delta_ru, delta_ue = _components_delta(merged.breakdown, base_breakdown)
    return MergeCost(
        compatible=True,
        delta_du=delta_du,
        delta_ru=delta_ru,
        delta_ue=delta_ue,
        delta_cost=merged.cost - max(left.cost, right.cost),
        reason="compatible",
    )


def build_cost_graph(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_neighbors: int = 30,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> dict[int, list[GraphEdge]]:
    """Build a bounded weighted compatibility graph for clustering warm start."""

    max_neighbors = max(0, max_neighbors)
    adjacency: dict[int, list[GraphEdge]] = {case.index: [] for case in cases}
    if max_neighbors == 0:
        return adjacency

    retained: dict[int, list[GraphEdge]] = defaultdict(list)
    n = len(cases)
    for i in range(n):
        for j in range(i + 1, n):
            cost = merge_cost(
                requirement_columns,
                cases,
                i,
                j,
                support,
                weights,
                max_compatibility_variants=max_compatibility_variants,
            )
            if not cost.compatible:
                continue
            retained[i].append(GraphEdge(i, j, cost))
            retained[j].append(GraphEdge(j, i, cost))

    for index in range(n):
        adjacency[index] = sorted(
            retained.get(index, []),
            key=lambda edge: (
                edge.cost.delta_cost,
                edge.cost.delta_du,
                edge.cost.delta_ru,
                edge.cost.delta_ue,
                edge.target,
            ),
        )[:max_neighbors]
    return adjacency


def _append_best_line(
    lines: list[LineState],
    candidate: LineState,
) -> None:
    lines.append(candidate)


def _best_insertion(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    lines: Sequence[LineState],
    case_index: int,
    max_cases_per_line: int,
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_compatibility_variants: int,
) -> tuple[int | None, LineState | None, float]:
    best_line_index: int | None = None
    best_line: LineState | None = None
    best_delta = math.inf
    for line_index, line in enumerate(lines):
        if len(line.case_indices) >= max_cases_per_line:
            continue
        if case_index in line.case_indices:
            continue
        candidate = build_line_state(
            requirement_columns,
            cases,
            (*line.case_indices, case_index),
            support,
            weights,
            max_compatibility_variants=max_compatibility_variants,
        )
        if candidate is None:
            continue
        delta = candidate.cost - line.cost
        key = (delta, candidate.cost, len(candidate.case_indices), line_index)
        best_key = (
            best_delta,
            best_line.cost if best_line is not None else math.inf,
            len(best_line.case_indices) if best_line is not None else math.inf,
            best_line_index if best_line_index is not None else math.inf,
        )
        if key < best_key:
            best_line_index = line_index
            best_line = candidate
            best_delta = delta
    return best_line_index, best_line, best_delta


def initial_clustering(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_cases_per_line: int,
    strategy: str = "cluster",
    max_neighbors: int = 30,
    seed: int = 1,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> tuple[LineState, ...]:
    """Create a deterministic warm-start line assignment."""

    rng = random.Random(seed)
    order = list(range(len(cases)))
    graph: dict[int, list[GraphEdge]] = {}

    if strategy == "random":
        rng.shuffle(order)
    elif strategy == "cluster":
        graph = build_cost_graph(
            requirement_columns,
            cases,
            support,
            weights,
            max_neighbors=max_neighbors,
            max_compatibility_variants=max_compatibility_variants,
        )
        def singleton_sort_cost(index: int) -> float:
            state = build_line_state(
                requirement_columns,
                cases,
                [index],
                support,
                weights,
                max_compatibility_variants=max_compatibility_variants,
            )
            return state.cost if state is not None else math.inf

        order.sort(
            key=lambda index: (
                -len(graph.get(index, [])),
                singleton_sort_cost(index),
                index,
            )
        )
    else:
        order.sort(
            key=lambda index: (
                -sum(len(cases[index].tokens[column]) for column in requirement_columns),
                index,
            )
        )

    unassigned = set(order)
    order_rank = {index: rank for rank, index in enumerate(order)}
    lines: list[LineState] = []

    while unassigned:
        seed_index = min(unassigned, key=lambda index: order_rank[index])
        seed_line = build_line_state(
            requirement_columns,
            cases,
            [seed_index],
            support,
            weights,
            max_compatibility_variants=max_compatibility_variants,
        )
        if seed_line is None:
            raise SystemExit(
                f"testcase {cases[seed_index].tc_id} has no compatible line realization"
            )
        current = seed_line
        unassigned.remove(seed_index)

        if strategy == "cluster" and graph:
            candidates = [edge.target for edge in graph.get(seed_index, []) if edge.target in unassigned]
            candidates.extend(index for index in order if index in unassigned and index not in candidates)
        else:
            candidates = [index for index in order if index in unassigned]

        improved = True
        while len(current.case_indices) < max_cases_per_line and improved:
            improved = False
            best_index: int | None = None
            best_state: LineState | None = None
            best_delta = math.inf
            for index in candidates:
                if index not in unassigned:
                    continue
                candidate_state = build_line_state(
                    requirement_columns,
                    cases,
                    (*current.case_indices, index),
                    support,
                    weights,
                    max_compatibility_variants=max_compatibility_variants,
                )
                if candidate_state is None:
                    continue
                delta = candidate_state.cost - current.cost
                key = (delta, candidate_state.cost, index)
                best_key = (
                    best_delta,
                    best_state.cost if best_state is not None else math.inf,
                    best_index if best_index is not None else math.inf,
                )
                if key < best_key:
                    best_index = index
                    best_state = candidate_state
                    best_delta = delta
            if best_index is not None and best_state is not None:
                current = best_state
                unassigned.remove(best_index)
                improved = True
        _append_best_line(lines, current)

    return tuple(_normalize_lines(lines))


def _normalize_lines(lines: Iterable[LineState]) -> list[LineState]:
    return sorted(
        lines,
        key=lambda line: (
            line.cost,
            line.breakdown.du,
            line.breakdown.ru,
            line.breakdown.ue,
            -len(line.case_indices),
            min(line.case_indices),
        ),
    )


def _candidate_transfer(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    lines: Sequence[LineState],
    source_index: int,
    target_index: int,
    case_index: int,
    max_cases_per_line: int,
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_compatibility_variants: int,
) -> tuple[list[LineState], float] | None:
    if source_index == target_index:
        return None
    source = lines[source_index]
    target = lines[target_index]
    if case_index not in source.case_indices or len(target.case_indices) >= max_cases_per_line:
        return None
    if len(source.case_indices) <= 1:
        return None

    new_source_indices = tuple(index for index in source.case_indices if index != case_index)
    new_target_indices = (*target.case_indices, case_index)
    new_source = build_line_state(
        requirement_columns,
        cases,
        new_source_indices,
        support,
        weights,
        max_compatibility_variants=max_compatibility_variants,
    )
    new_target = build_line_state(
        requirement_columns,
        cases,
        new_target_indices,
        support,
        weights,
        max_compatibility_variants=max_compatibility_variants,
    )
    if new_source is None or new_target is None:
        return None

    updated = list(lines)
    updated[source_index] = new_source
    updated[target_index] = new_target
    delta = new_source.cost + new_target.cost - source.cost - target.cost
    return _normalize_lines(updated), delta


def _candidate_swap(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    lines: Sequence[LineState],
    left_index: int,
    right_index: int,
    left_case: int,
    right_case: int,
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_compatibility_variants: int,
) -> tuple[list[LineState], float] | None:
    if left_index == right_index:
        return None
    left = lines[left_index]
    right = lines[right_index]
    if left_case not in left.case_indices or right_case not in right.case_indices:
        return None

    new_left_indices = tuple(
        right_case if index == left_case else index for index in left.case_indices
    )
    new_right_indices = tuple(
        left_case if index == right_case else index for index in right.case_indices
    )
    new_left = build_line_state(
        requirement_columns,
        cases,
        new_left_indices,
        support,
        weights,
        max_compatibility_variants=max_compatibility_variants,
    )
    new_right = build_line_state(
        requirement_columns,
        cases,
        new_right_indices,
        support,
        weights,
        max_compatibility_variants=max_compatibility_variants,
    )
    if new_left is None or new_right is None:
        return None

    updated = list(lines)
    updated[left_index] = new_left
    updated[right_index] = new_right
    delta = new_left.cost + new_right.cost - left.cost - right.cost
    return _normalize_lines(updated), delta


def _candidate_merge_lines(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    lines: Sequence[LineState],
    left_index: int,
    right_index: int,
    max_cases_per_line: int,
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_compatibility_variants: int,
) -> tuple[list[LineState], float] | None:
    if left_index == right_index:
        return None
    left = lines[left_index]
    right = lines[right_index]
    if len(left.case_indices) + len(right.case_indices) > max_cases_per_line:
        return None
    merged = build_line_state(
        requirement_columns,
        cases,
        (*left.case_indices, *right.case_indices),
        support,
        weights,
        max_compatibility_variants=max_compatibility_variants,
    )
    if merged is None:
        return None
    updated = [line for index, line in enumerate(lines) if index not in {left_index, right_index}]
    updated.append(merged)
    delta = merged.cost - left.cost - right.cost
    return _normalize_lines(updated), delta


def _accept_move(delta: float, temperature: float, rng: random.Random) -> bool:
    if delta < -1e-9:
        return True
    if temperature <= 0:
        return False
    if delta <= 1e-9:
        return True
    return rng.random() < math.exp(-delta / temperature)


def local_search(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    initial_lines: Sequence[LineState],
    support: RuBandSupport,
    weights: EquipmentWeights,
    max_cases_per_line: int,
    search_time_limit: float = 60.0,
    max_iterations: int = 200,
    temperature: float = 0.0,
    cooling: float = 0.995,
    seed: int = 1,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
    verbose: bool = False,
) -> LocalSearchResult:
    rng = random.Random(seed)
    started = time.monotonic()
    lines = tuple(_normalize_lines(initial_lines))
    best_lines = lines
    best_cost = assignment_cost(best_lines)
    started_cost = best_cost
    accepted = 0
    transfer_moves = 0
    swap_moves = 0
    merge_moves = 0
    iterations = 0
    current_temperature = max(0.0, temperature)

    for iteration in range(max(0, max_iterations)):
        if time.monotonic() - started >= search_time_limit:
            break
        iterations = iteration + 1
        current_cost = assignment_cost(lines)
        best_candidate: tuple[str, list[LineState], float] | None = None

        # 1) Merge-line neighborhood. This is cheap and usually gives the largest wins.
        for left_index in range(len(lines)):
            for right_index in range(left_index + 1, len(lines)):
                candidate = _candidate_merge_lines(
                    requirement_columns,
                    cases,
                    lines,
                    left_index,
                    right_index,
                    max_cases_per_line,
                    support,
                    weights,
                    max_compatibility_variants,
                )
                if candidate is None:
                    continue
                candidate_lines, delta = candidate
                if best_candidate is None or delta < best_candidate[2]:
                    best_candidate = ("merge", candidate_lines, delta)

        # 2) Transfer neighborhood.
        if best_candidate is None or best_candidate[2] >= -1e-9:
            for source_index, source in enumerate(lines):
                if len(source.case_indices) <= 1:
                    continue
                for case_index in source.case_indices:
                    for target_index, target in enumerate(lines):
                        if source_index == target_index:
                            continue
                        if len(target.case_indices) >= max_cases_per_line:
                            continue
                        candidate = _candidate_transfer(
                            requirement_columns,
                            cases,
                            lines,
                            source_index,
                            target_index,
                            case_index,
                            max_cases_per_line,
                            support,
                            weights,
                            max_compatibility_variants,
                        )
                        if candidate is None:
                            continue
                        candidate_lines, delta = candidate
                        if best_candidate is None or delta < best_candidate[2]:
                            best_candidate = ("transfer", candidate_lines, delta)

        # 3) Swap neighborhood. Limit work naturally by the wall clock.
        if best_candidate is None or best_candidate[2] >= -1e-9:
            for left_index in range(len(lines)):
                if time.monotonic() - started >= search_time_limit:
                    break
                for right_index in range(left_index + 1, len(lines)):
                    left = lines[left_index]
                    right = lines[right_index]
                    for left_case in left.case_indices:
                        for right_case in right.case_indices:
                            candidate = _candidate_swap(
                                requirement_columns,
                                cases,
                                lines,
                                left_index,
                                right_index,
                                left_case,
                                right_case,
                                support,
                                weights,
                                max_compatibility_variants,
                            )
                            if candidate is None:
                                continue
                            candidate_lines, delta = candidate
                            if best_candidate is None or delta < best_candidate[2]:
                                best_candidate = ("swap", candidate_lines, delta)

        if best_candidate is None:
            break
        move_name, candidate_lines, delta = best_candidate
        if not _accept_move(delta, current_temperature, rng):
            break

        lines = tuple(candidate_lines)
        accepted += 1
        if move_name == "merge":
            merge_moves += 1
        elif move_name == "transfer":
            transfer_moves += 1
        else:
            swap_moves += 1
        if delta >= -1e-9 and current_temperature > 0:
            current_temperature *= cooling

        new_cost = current_cost + delta
        if new_cost < best_cost - 1e-9:
            best_lines = lines
            best_cost = new_cost
        if verbose:
            print(
                f"MOVE iteration={iterations} type={move_name} "
                f"delta={delta:.4f} cost={assignment_cost(lines):.4f} "
                f"lines={len(lines)}"
            )

    return LocalSearchResult(
        lines=tuple(_normalize_lines(best_lines)),
        total_cost=assignment_cost(best_lines),
        iterations=iterations,
        accepted_moves=accepted,
        transfer_moves=transfer_moves,
        swap_moves=swap_moves,
        merge_moves=merge_moves,
        started_cost=started_cost,
        solve_status="LOCAL_SEARCH",
    )


def validate_assignment(
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    lines: Sequence[LineState],
    max_cases_per_line: int,
    support: RuBandSupport,
) -> None:
    seen: dict[int, int] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line.case_indices:
            raise SystemExit(f"line {line_number} has no assigned testcases")
        if len(line.case_indices) > max_cases_per_line:
            raise SystemExit(
                f"line {line_number} exceeds --max-cases-per-line: "
                f"{len(line.case_indices)} > {max_cases_per_line}"
            )
        if not spec_has_compatible_ru_bands(line.spec, support):
            raise SystemExit(f"line {line_number} has incompatible RU-band spec")
        if not relations_are_physically_supported(line.spec, support):
            raise SystemExit(f"line {line_number} has infeasible band relation tokens")
        expected_breakdown = equipment_breakdown(requirement_columns, line.spec)
        if expected_breakdown != line.breakdown:
            raise SystemExit(f"line {line_number} has stale equipment breakdown")
        for case_index in line.case_indices:
            if case_index in seen:
                raise SystemExit(
                    f"testcase {cases[case_index].tc_id} assigned to multiple lines"
                )
            seen[case_index] = line_number
            ok, _ = coverage_delta(
                list(requirement_columns),
                line.spec,
                cases[case_index],
                enforce_delta=False,
                support=support,
            )
            if not ok:
                raise SystemExit(
                    f"line {line_number} spec does not cover testcase "
                    f"{cases[case_index].tc_id}"
                )

    missing = [case.tc_id for case in cases if case.index not in seen]
    if missing:
        raise SystemExit("unassigned testcases: " + ", ".join(missing))


def write_lines_output(
    path: Path,
    input_columns: Sequence[str],
    requirement_columns: Sequence[str],
    cases: Sequence[TestCase],
    lines: Sequence[LineState],
    result: LocalSearchResult,
) -> tuple[int, float, int]:
    output_columns = [
        "spec_id",
        "assigned_tc_ids",
        "assigned_count",
        "covered_tc_ids",
        "covered_count",
        "line_cost",
        "equipment_count",
        "du_count",
        "ru_count",
        "ue_count",
        "solve_status",
    ]
    output_columns.extend(column for column in input_columns if column != "tc_id")

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_columns)
        writer.writeheader()
        for line_number, line in enumerate(_normalize_lines(lines), start=1):
            assigned_ids = [cases[index].tc_id for index in line.case_indices]
            row = {
                "spec_id": f"line_{line_number}",
                "assigned_tc_ids": " + ".join(assigned_ids),
                "assigned_count": len(assigned_ids),
                "covered_tc_ids": " + ".join(assigned_ids),
                "covered_count": len(assigned_ids),
                "line_cost": f"{line.cost:.6g}",
                "equipment_count": equipment_count(list(requirement_columns), line.spec),
                "du_count": line.breakdown.du,
                "ru_count": line.breakdown.ru,
                "ue_count": line.breakdown.ue,
                "solve_status": result.solve_status,
            }
            for column in requirement_columns:
                row[column] = render_cell(line.spec[column])
            writer.writerow(row)

    total_equipment = sum(equipment_count(list(requirement_columns), line.spec) for line in lines)
    return len(lines), result.total_cost, total_equipment


def _require_positive(name: str, value: float) -> None:
    if value <= 0:
        raise SystemExit(f"--{name} must be positive")


def _require_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise SystemExit(f"--{name} must be non-negative")


def main() -> int:
    args = parse_args()
    _require_positive("max-cases-per-line", args.max_cases_per_line)
    _require_non_negative("max-lines", args.max_lines)
    _require_non_negative("du-weight", args.du_weight)
    _require_non_negative("ru-weight", args.ru_weight)
    _require_non_negative("ue-weight", args.ue_weight)
    _require_non_negative("search-time-limit", args.search_time_limit)
    _require_non_negative("max-iterations", args.max_iterations)
    _require_non_negative("max-neighbors", args.max_neighbors)
    _require_non_negative("temperature", args.temperature)
    if not 0 < args.cooling <= 1:
        raise SystemExit("--cooling must be in the interval (0, 1]")

    started_at = time.monotonic()
    input_path = Path(args.input)
    output_path = Path(args.output)
    support_path = Path(args.ru_band_support)

    input_columns, cases = load_cases(input_path)
    support = load_ru_band_support(support_path)
    requirement_columns = [column for column in input_columns if column != "tc_id"]
    if args.ignore_tech_and_ue_capa:
        requirement_columns = [
            column
            for column in requirement_columns
            if not is_temporarily_ignored_column(column)
        ]
    validate_support_references(requirement_columns, cases, support)

    if args.max_lines and args.max_lines * args.max_cases_per_line < len(cases):
        raise SystemExit(
            "--max-lines is too small for --max-cases-per-line and testcase count"
        )

    weights = EquipmentWeights(
        du=args.du_weight,
        ru=args.ru_weight,
        ue=args.ue_weight,
    )
    initial_lines = initial_clustering(
        requirement_columns=requirement_columns,
        cases=cases,
        support=support,
        weights=weights,
        max_cases_per_line=args.max_cases_per_line,
        strategy=args.initial_strategy,
        max_neighbors=args.max_neighbors,
        seed=args.seed,
        max_compatibility_variants=max(1, args.max_compatibility_variants),
    )
    if args.max_lines and len(initial_lines) > args.max_lines:
        raise SystemExit(
            f"initial assignment needs {len(initial_lines)} compatible lines, "
            f"which exceeds --max-lines={args.max_lines}"
        )

    result = local_search(
        requirement_columns=requirement_columns,
        cases=cases,
        initial_lines=initial_lines,
        support=support,
        weights=weights,
        max_cases_per_line=args.max_cases_per_line,
        search_time_limit=args.search_time_limit,
        max_iterations=args.max_iterations,
        temperature=args.temperature,
        cooling=args.cooling,
        seed=args.seed,
        max_compatibility_variants=max(1, args.max_compatibility_variants),
        verbose=args.verbose,
    )
    if args.max_lines and len(result.lines) > args.max_lines:
        raise SystemExit(
            f"local search produced {len(result.lines)} lines, exceeding --max-lines={args.max_lines}"
        )

    validate_assignment(
        requirement_columns,
        cases,
        result.lines,
        args.max_cases_per_line,
        support,
    )
    line_count, total_cost, total_equipment = write_lines_output(
        output_path,
        input_columns,
        requirement_columns,
        cases,
        result.lines,
        result,
    )

    elapsed = time.monotonic() - started_at
    print(f"status={result.solve_status}")
    print(f"runtime_seconds={elapsed:.2f}")
    print(f"input_testcases={len(cases)}")
    print(f"initial_lines={len(initial_lines)}")
    print(f"output_lines={line_count}")
    print(f"started_cost={result.started_cost:.6g}")
    print(f"total_cost={total_cost:.6g}")
    print(f"total_equipment={total_equipment}")
    print(f"iterations={result.iterations}")
    print(f"accepted_moves={result.accepted_moves}")
    print(f"merge_moves={result.merge_moves}")
    print(f"transfer_moves={result.transfer_moves}")
    print(f"swap_moves={result.swap_moves}")
    print(f"output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
