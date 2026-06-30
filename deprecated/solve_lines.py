#!/usr/bin/env python3
"""
solve_lines.py — Direct line-packing optimizer for telecom test cases.

Goal: assign every test case to a physical test line such that
  - each line holds at most --max-cases-per-line test cases
  - each line's spec is the merged requirements of its assigned cases
  - total weighted equipment cost across all lines is minimized

Cost model (configurable weights):
  line_cost = du_weight * DU_slots + ru_weight * RU_slots + ue_weight * UE_capacity

Search strategy: Greedy initial solution → Simulated Annealing with
TRANSFER, SWAP, LINE_MERGE, LINE_SPLIT moves and periodic LNS restarts.
"""

from __future__ import annotations

import argparse
import copy
import csv
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from solve_test_lines import (
    ANY,
    DU_COLUMNS,
    NUMERIC_EQUIPMENT_COLUMNS,
    RELATION_TOKENS,
    RU_COLUMN,
    SINGLE_SELECT_COLUMNS,
    UE_COLUMN,
    RuBandSupport,
    TestCase,
    all_integer_tokens,
    alternatives,
    coverage_delta,
    is_any,
    is_temporarily_ignored_column,
    load_cases,
    load_ru_band_support,
    merge_cases,
    merge_column,
    numeric_equipment,
    parse_cell,
    render_cell,
    resolve_compatibility_variants,
    single_select_key,
    spec_has_compatible_ru_bands,
    spec_signature,
    validate_support_references,
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pack test cases into physical test lines minimizing weighted equipment cost."
    )
    parser.add_argument("--input", default="input.csv", help="Input testcase CSV. Default: input.csv")
    parser.add_argument("--output", default="output_lines.csv", help="Output lines CSV. Default: output_lines.csv")
    parser.add_argument(
        "--ru-band-support", required=True,
        help="RU-band support CSV (same format as solve_test_lines.py).",
    )
    parser.add_argument(
        "--max-cases-per-line", type=int, default=250,
        help="Maximum test cases per physical line. Default: 250.",
    )
    parser.add_argument("--du-weight", type=float, default=1.0, help="Cost weight for each DU slot. Default: 1.0")
    parser.add_argument("--ru-weight", type=float, default=1.0, help="Cost weight for each RU slot. Default: 1.0")
    parser.add_argument("--ue-weight", type=float, default=1.0, help="Cost weight for each UE slot. Default: 1.0")
    parser.add_argument("--time-limit", type=float, default=300.0, help="Search time limit in seconds. Default: 300.")
    parser.add_argument(
        "--temperature-start", type=float, default=2.0,
        help="Initial SA temperature. Default: 2.0",
    )
    parser.add_argument(
        "--cooling-rate", type=float, default=0.995,
        help="SA cooling rate per iteration. Default: 0.995",
    )
    parser.add_argument(
        "--restart-interval", type=int, default=10000,
        help="Restart from best solution every N iterations. Default: 10000",
    )
    parser.add_argument(
        "--ignore-tech-and-ue-capa", action="store_true",
        help="Ignore tech and ue capa columns (blank in output).",
    )
    parser.add_argument(
        "--initial-strategy",
        choices=("greedy",),
        default="greedy",
        help="Initial assignment strategy. Default: greedy.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed. Default: 42")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print search progress.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EquipmentWeights:
    du: float = 1.0
    ru: float = 1.0
    ue: float = 1.0


def spec_cost(
    spec: dict[str, tuple[str, ...]],
    requirement_columns: list[str],
    weights: EquipmentWeights,
) -> float:
    du = sum(
        numeric_equipment(spec.get(col, ()))
        for col in DU_COLUMNS
        if col in requirement_columns
    )
    ru = len(spec.get(RU_COLUMN, ())) if RU_COLUMN in requirement_columns else 0
    ue = numeric_equipment(spec.get(UE_COLUMN, ())) if UE_COLUMN in requirement_columns else 0
    return weights.du * du + weights.ru * ru + weights.ue * ue


def materialize_spec(
    raw_spec: dict[str, tuple[str, ...]],
    requirement_columns: list[str],
    weights: EquipmentWeights,
    support: RuBandSupport,
) -> dict[str, tuple[str, ...]] | None:
    """Resolve RU/LTE/NR wildcards into a concrete compatible line spec.

    Line packing keeps raw merged requirements for future moves, but every cost,
    coverage check, and output row must use a physically valid concrete spec.
    """
    variants = resolve_compatibility_variants(raw_spec, support)
    feasible = [
        variant
        for variant in variants
        if spec_has_compatible_ru_bands(variant, support)
    ]
    if not feasible:
        return None
    return min(
        feasible,
        key=lambda variant: (
            spec_cost(variant, requirement_columns, weights),
            spec_signature(variant),
        ),
    )


# ---------------------------------------------------------------------------
# Line state  (mutable, incrementally maintained)
# ---------------------------------------------------------------------------

_NEXT_LINE_ID = 0


def _next_line_id() -> int:
    global _NEXT_LINE_ID
    _NEXT_LINE_ID += 1
    return _NEXT_LINE_ID


@dataclass
class Line:
    """A physical test line with a mutable set of assigned test cases."""

    line_id: int = field(default_factory=_next_line_id)
    case_indices: set[int] = field(default_factory=set)

    # Maintained lazily: recomputed whenever _dirty is True. _raw_spec keeps
    # wildcard requirements flexible for future moves; _spec is the concrete
    # RU/band-compatible realization used for cost, validation, and output.
    _raw_spec: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    _spec: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    _cost: float = field(default=0.0, repr=False)
    _dirty: bool = field(default=True, repr=False)

    # Cached single-select key for fast compatibility checks
    _ss_key: tuple | None = field(default=None, repr=False)

    def _recompute(
        self,
        cases: list[TestCase],
        requirement_columns: list[str],
        weights: EquipmentWeights,
        support: RuBandSupport,
    ) -> None:
        if not self.case_indices:
            self._raw_spec = {col: () for col in requirement_columns}
            self._spec = {col: () for col in requirement_columns}
            self._cost = 0.0
            self._dirty = False
            self._ss_key = ()
            return

        assigned = [cases[i] for i in self.case_indices]
        raw_spec = merge_cases(requirement_columns, assigned)
        if raw_spec is None:
            raise SystemExit(
                f"line {self.line_id} has irreconcilable single-select conflict"
            )
        spec = materialize_spec(raw_spec, requirement_columns, weights, support)
        if spec is None:
            ids = ", ".join(cases[i].tc_id for i in sorted(self.case_indices))
            raise SystemExit(
                f"line {self.line_id} has no compatible RU-band realization: {ids}"
            )

        self._raw_spec = raw_spec
        self._spec = spec
        self._cost = spec_cost(spec, requirement_columns, weights)
        self._dirty = False

        # Single-select key comes from the raw merged requirements; RU/band
        # materialization must not affect single-select compatibility.
        ss: list[tuple[str, tuple[str, ...]]] = []
        for col in SINGLE_SELECT_COLUMNS:
            if col in raw_spec:
                concrete = tuple(t for t in raw_spec[col] if not is_any(t))
                ss.append((col, concrete))
        self._ss_key = tuple(sorted(ss))

    def get_raw_spec(
        self,
        cases: list[TestCase],
        requirement_columns: list[str],
        weights: EquipmentWeights,
        support: RuBandSupport,
    ) -> dict[str, tuple[str, ...]]:
        if self._dirty:
            self._recompute(cases, requirement_columns, weights, support)
        return self._raw_spec

    def get_spec(
        self,
        cases: list[TestCase],
        requirement_columns: list[str],
        weights: EquipmentWeights,
        support: RuBandSupport,
    ) -> dict[str, tuple[str, ...]]:
        if self._dirty:
            self._recompute(cases, requirement_columns, weights, support)
        return self._spec

    def get_cost(
        self,
        cases: list[TestCase],
        requirement_columns: list[str],
        weights: EquipmentWeights,
        support: RuBandSupport,
    ) -> float:
        if self._dirty:
            self._recompute(cases, requirement_columns, weights, support)
        return self._cost

    def get_ss_key(
        self,
        cases: list[TestCase],
        requirement_columns: list[str],
        weights: EquipmentWeights,
        support: RuBandSupport,
    ) -> tuple:
        if self._dirty:
            self._recompute(cases, requirement_columns, weights, support)
        return self._ss_key  # type: ignore[return-value]

    def mark_dirty(self) -> None:
        self._dirty = True

    # ------------------------------------------------------------------
    # Dry-run helpers — compute hypothetical cost without mutating state
    # ------------------------------------------------------------------

    def cost_if_add(
        self,
        case: TestCase,
        requirement_columns: list[str],
        weights: EquipmentWeights,
        cases: list[TestCase],
        support: RuBandSupport,
    ) -> float | None:
        """Return hypothetical concrete cost after adding *case*, or None if incompatible."""
        current_raw_spec = self.get_raw_spec(
            cases, requirement_columns, weights, support
        )
        new_raw_spec = _merge_spec_with_case(
            current_raw_spec, case, requirement_columns
        )
        if new_raw_spec is None:
            return None
        new_spec = materialize_spec(
            new_raw_spec, requirement_columns, weights, support
        )
        if new_spec is None:
            return None
        return spec_cost(new_spec, requirement_columns, weights)

    def cost_if_remove(
        self,
        case: TestCase,
        requirement_columns: list[str],
        weights: EquipmentWeights,
        cases: list[TestCase],
        support: RuBandSupport,
    ) -> float | None:
        """Return hypothetical concrete cost after removing *case*."""
        remaining = [cases[i] for i in self.case_indices if i != case.index]
        if not remaining:
            return 0.0
        raw_spec = merge_cases(requirement_columns, remaining)
        if raw_spec is None:
            return None
        spec = materialize_spec(raw_spec, requirement_columns, weights, support)
        if spec is None:
            return None
        return spec_cost(spec, requirement_columns, weights)

    def cost_if_swap(
        self,
        add_case: TestCase,
        remove_case: TestCase,
        requirement_columns: list[str],
        weights: EquipmentWeights,
        cases: list[TestCase],
        support: RuBandSupport,
    ) -> float | None:
        """Return hypothetical concrete cost after replacing *remove_case* with *add_case*."""
        remaining = [cases[i] for i in self.case_indices if i != remove_case.index]
        remaining.append(add_case)
        raw_spec = merge_cases(requirement_columns, remaining)
        if raw_spec is None:
            return None
        spec = materialize_spec(raw_spec, requirement_columns, weights, support)
        if spec is None:
            return None
        return spec_cost(spec, requirement_columns, weights)


def _merge_spec_with_case(
    spec: dict[str, tuple[str, ...]],
    case: TestCase,
    requirement_columns: list[str],
) -> dict[str, tuple[str, ...]] | None:
    """Merge an existing spec with one additional case. Returns None on conflict."""
    new_spec: dict[str, tuple[str, ...]] = {}
    for col in requirement_columns:
        merged = merge_column(col, (spec.get(col, ()), case.tokens.get(col, ())))
        if merged is None:
            return None
        new_spec[col] = merged
    return new_spec


def _ss_compatible(line_ss_key: tuple, case: TestCase) -> bool:
    """Fast check: would adding *case* to a line with *line_ss_key* cause a single-select conflict?"""
    for col in SINGLE_SELECT_COLUMNS:
        if col not in case.tokens:
            continue
        case_concrete = frozenset(t for t in case.tokens[col] if not is_any(t))
        if not case_concrete:
            continue
        for ss_col, ss_vals in line_ss_key:
            if ss_col == col and ss_vals and not case_concrete.issubset(frozenset(ss_vals)):
                return False
    return True


# ---------------------------------------------------------------------------
# Assignment state — owns all lines, provides the global view
# ---------------------------------------------------------------------------

@dataclass
class Assignment:
    lines: list[Line]
    case_to_line: dict[int, int]  # case_index → line_id
    line_by_id: dict[int, Line]

    @classmethod
    def empty(cls) -> Assignment:
        return cls(lines=[], case_to_line={}, line_by_id={})

    def add_line(self, line: Line) -> None:
        self.lines.append(line)
        self.line_by_id[line.line_id] = line

    def remove_line(self, line: Line) -> None:
        self.lines.remove(line)
        del self.line_by_id[line.line_id]

    def assign(self, case_index: int, line: Line) -> None:
        line.case_indices.add(case_index)
        line.mark_dirty()
        self.case_to_line[case_index] = line.line_id

    def unassign(self, case_index: int, line: Line) -> None:
        line.case_indices.discard(case_index)
        line.mark_dirty()
        del self.case_to_line[case_index]

    def total_cost(
        self,
        cases: list[TestCase],
        requirement_columns: list[str],
        weights: EquipmentWeights,
        support: RuBandSupport,
    ) -> float:
        return sum(
            line.get_cost(cases, requirement_columns, weights, support)
            for line in self.lines
            if line.case_indices
        )

    def active_lines(self) -> list[Line]:
        return [line for line in self.lines if line.case_indices]

    def deep_copy(self) -> Assignment:
        new_lines = []
        new_line_by_id: dict[int, Line] = {}
        for line in self.lines:
            new_line = Line(
                line_id=line.line_id,
                case_indices=set(line.case_indices),
                _raw_spec=dict(line._raw_spec),
                _spec=dict(line._spec),
                _cost=line._cost,
                _dirty=line._dirty,
                _ss_key=line._ss_key,
            )
            new_lines.append(new_line)
            new_line_by_id[new_line.line_id] = new_line
        new_case_to_line = dict(self.case_to_line)
        return Assignment(
            lines=new_lines,
            case_to_line=new_case_to_line,
            line_by_id=new_line_by_id,
        )


# ---------------------------------------------------------------------------
# Phase 1: Greedy initial solution (First-Fit Decreasing by equipment cost)
# ---------------------------------------------------------------------------

def _case_base_cost(
    case: TestCase,
    requirement_columns: list[str],
    weights: EquipmentWeights,
    support: RuBandSupport,
) -> float:
    """Cost of a single test case as a standalone concrete line."""
    raw_spec = {col: case.tokens.get(col, ()) for col in requirement_columns}
    spec = materialize_spec(raw_spec, requirement_columns, weights, support)
    if spec is None:
        raise SystemExit(
            f"testcase {case.tc_id} has no compatible RU-band realization"
        )
    return spec_cost(spec, requirement_columns, weights)


def greedy_initial(
    cases: list[TestCase],
    requirement_columns: list[str],
    weights: EquipmentWeights,
    max_cases_per_line: int,
    support: RuBandSupport,
    rng: random.Random,
) -> Assignment:
    """
    Build initial assignment using First-Fit Decreasing.
    Sort cases by descending standalone cost, then greedily assign each
    to the existing line that accepts it with minimum marginal cost increase.
    """
    assignment = Assignment.empty()

    # Sort: heaviest cases first so expensive cases claim lines early
    sorted_cases = sorted(
        cases,
        key=lambda c: (_case_base_cost(c, requirement_columns, weights, support), c.index),
        reverse=True,
    )

    for case in sorted_cases:
        best_line: Line | None = None
        best_delta: float = math.inf

        active = assignment.active_lines()
        # Shuffle candidate lines slightly to break ties non-deterministically
        rng.shuffle(active)

        for line in active:
            if len(line.case_indices) >= max_cases_per_line:
                continue
            # Fast reject: single-select conflict
            ss_key = line.get_ss_key(cases, requirement_columns, weights, support)
            if not _ss_compatible(ss_key, case):
                continue
            cost_before = line.get_cost(cases, requirement_columns, weights, support)
            cost_after = line.cost_if_add(case, requirement_columns, weights, cases, support)
            if cost_after is None:
                continue  # merge conflict or no RU-band-compatible realization
            delta = cost_after - cost_before
            if delta < best_delta:
                best_delta = delta
                best_line = line

        if best_line is None:
            # Open a new line
            new_line = Line()
            assignment.add_line(new_line)
            best_line = new_line

        assignment.assign(case.index, best_line)

    return assignment


# ---------------------------------------------------------------------------
# Phase 2: Local search moves
# ---------------------------------------------------------------------------

MoveResult = tuple[float, object]  # (delta_cost, move_descriptor)


def _try_transfer(
    case: TestCase,
    from_line: Line,
    to_line: Line,
    cases: list[TestCase],
    requirement_columns: list[str],
    weights: EquipmentWeights,
    support: RuBandSupport,
    max_cases_per_line: int,
) -> float | None:
    """
    Compute cost delta for moving *case* from *from_line* to *to_line*.
    Returns None if move is infeasible.
    """
    if len(to_line.case_indices) >= max_cases_per_line:
        return None
    if len(from_line.case_indices) <= 1:
        # Would empty from_line; allow only if we then remove it (handled by caller)
        pass
    ss_key = to_line.get_ss_key(cases, requirement_columns, weights, support)
    if not _ss_compatible(ss_key, case):
        return None

    cost_from_before = from_line.get_cost(cases, requirement_columns, weights, support)
    cost_to_before = to_line.get_cost(cases, requirement_columns, weights, support)

    cost_from_after = from_line.cost_if_remove(case, requirement_columns, weights, cases, support)
    cost_to_after = to_line.cost_if_add(case, requirement_columns, weights, cases, support)
    if cost_from_after is None or cost_to_after is None:
        return None

    return (cost_from_after + cost_to_after) - (cost_from_before + cost_to_before)


def _try_swap(
    case_a: TestCase,
    line_a: Line,
    case_b: TestCase,
    line_b: Line,
    cases: list[TestCase],
    requirement_columns: list[str],
    weights: EquipmentWeights,
    support: RuBandSupport,
) -> float | None:
    """
    Compute cost delta for swapping *case_a* (on *line_a*) with *case_b* (on *line_b*).
    Returns None if infeasible.
    """
    # Check single-select compatibility after swap
    # line_a would lose case_a, gain case_b
    # Simulate: check if line_a without case_a can accept case_b
    # and line_b without case_b can accept case_a
    # (Full check is done via cost_if_swap which calls merge_cases)

    cost_a_before = line_a.get_cost(cases, requirement_columns, weights, support)
    cost_b_before = line_b.get_cost(cases, requirement_columns, weights, support)

    cost_a_after = line_a.cost_if_swap(case_b, case_a, requirement_columns, weights, cases, support)
    cost_b_after = line_b.cost_if_swap(case_a, case_b, requirement_columns, weights, cases, support)

    if cost_a_after is None or cost_b_after is None:
        return None

    return (cost_a_after + cost_b_after) - (cost_a_before + cost_b_before)


def _try_merge_lines(
    line_a: Line,
    line_b: Line,
    cases: list[TestCase],
    requirement_columns: list[str],
    weights: EquipmentWeights,
    support: RuBandSupport,
    max_cases_per_line: int,
) -> float | None:
    """
    Compute cost delta for merging all cases from *line_b* into *line_a*.
    Returns None if infeasible (capacity or compatibility).
    """
    if len(line_a.case_indices) + len(line_b.case_indices) > max_cases_per_line:
        return None

    combined_cases = [cases[i] for i in line_a.case_indices | line_b.case_indices]
    raw_spec = merge_cases(requirement_columns, combined_cases)
    if raw_spec is None:
        return None
    merged_spec = materialize_spec(raw_spec, requirement_columns, weights, support)
    if merged_spec is None:
        return None

    cost_combined = spec_cost(merged_spec, requirement_columns, weights)
    cost_before = (
        line_a.get_cost(cases, requirement_columns, weights, support)
        + line_b.get_cost(cases, requirement_columns, weights, support)
    )
    return cost_combined - cost_before


def _try_split_line(
    line: Line,
    cases: list[TestCase],
    requirement_columns: list[str],
    weights: EquipmentWeights,
    support: RuBandSupport,
    rng: random.Random,
) -> tuple[float, list[int], list[int]] | None:
    """
    Try splitting *line* into two halves. Returns (delta, group_a_indices, group_b_indices)
    or None if split does not reduce cost.

    Strategy: sort cases on line by their standalone cost, split at median.
    Also try a random split and keep the better.
    """
    if len(line.case_indices) < 2:
        return None

    line_cases = [cases[i] for i in line.case_indices]
    cost_before = line.get_cost(cases, requirement_columns, weights, support)

    def eval_split(
        group_a: list[TestCase], group_b: list[TestCase]
    ) -> float | None:
        raw_a = merge_cases(requirement_columns, group_a)
        raw_b = merge_cases(requirement_columns, group_b)
        if raw_a is None or raw_b is None:
            return None
        spec_a = materialize_spec(raw_a, requirement_columns, weights, support)
        spec_b = materialize_spec(raw_b, requirement_columns, weights, support)
        if spec_a is None or spec_b is None:
            return None
        return (
            spec_cost(spec_a, requirement_columns, weights)
            + spec_cost(spec_b, requirement_columns, weights)
        )

    # Split 1: by standalone cost
    sorted_by_cost = sorted(
        line_cases,
        key=lambda c: _case_base_cost(c, requirement_columns, weights, support),
    )
    mid = len(sorted_by_cost) // 2
    cost_split1 = eval_split(sorted_by_cost[:mid], sorted_by_cost[mid:])

    # Split 2: random
    shuffled = list(line_cases)
    rng.shuffle(shuffled)
    cost_split2 = eval_split(shuffled[:mid], shuffled[mid:])

    best_cost = min(
        (c for c in (cost_split1, cost_split2) if c is not None),
        default=None,
    )
    if best_cost is None:
        return None

    delta = best_cost - cost_before
    if delta >= 0:
        return None

    # Return the better split's indices
    if cost_split1 is not None and (cost_split2 is None or cost_split1 <= cost_split2):
        group_a = [c.index for c in sorted_by_cost[:mid]]
        group_b = [c.index for c in sorted_by_cost[mid:]]
    else:
        group_a = [c.index for c in shuffled[:mid]]
        group_b = [c.index for c in shuffled[mid:]]

    return delta, group_a, group_b


# ---------------------------------------------------------------------------
# Move application helpers
# ---------------------------------------------------------------------------

def apply_transfer(
    case: TestCase,
    from_line: Line,
    to_line: Line,
    assignment: Assignment,
) -> None:
    assignment.unassign(case.index, from_line)
    assignment.assign(case.index, to_line)
    if not from_line.case_indices:
        assignment.remove_line(from_line)


def apply_swap(
    case_a: TestCase,
    line_a: Line,
    case_b: TestCase,
    line_b: Line,
    assignment: Assignment,
) -> None:
    assignment.unassign(case_a.index, line_a)
    assignment.unassign(case_b.index, line_b)
    assignment.assign(case_b.index, line_a)
    assignment.assign(case_a.index, line_b)


def apply_merge(
    line_a: Line,
    line_b: Line,
    assignment: Assignment,
) -> None:
    for idx in list(line_b.case_indices):
        assignment.unassign(idx, line_b)
        assignment.assign(idx, line_a)
    assignment.remove_line(line_b)


def apply_split(
    line: Line,
    group_a: list[int],
    group_b: list[int],
    assignment: Assignment,
) -> None:
    new_line = Line()
    assignment.add_line(new_line)
    for idx in group_b:
        assignment.unassign(idx, line)
        assignment.assign(idx, new_line)


# ---------------------------------------------------------------------------
# Phase 2: Simulated annealing main loop
# ---------------------------------------------------------------------------

def local_search(
    initial: Assignment,
    cases: list[TestCase],
    requirement_columns: list[str],
    weights: EquipmentWeights,
    max_cases_per_line: int,
    support: RuBandSupport,
    time_limit: float,
    temperature_start: float,
    cooling_rate: float,
    restart_interval: int,
    rng: random.Random,
    verbose: bool = False,
) -> Assignment:
    current = initial.deep_copy()
    best = initial.deep_copy()
    best_cost = best.total_cost(cases, requirement_columns, weights, support)
    current_cost = best_cost

    T = temperature_start
    iteration = 0
    start_time = time.monotonic()
    last_report = start_time
    improvements = 0

    # Move type weights (adaptive)
    move_weights = {"transfer": 40, "swap": 40, "merge": 15, "split": 5}

    def pick_move_type() -> str:
        total = sum(move_weights.values())
        r = rng.randint(1, total)
        cumulative = 0
        for move, w in move_weights.items():
            cumulative += w
            if r <= cumulative:
                return move
        return "transfer"

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed >= time_limit:
            break

        # Periodic restart from best
        if iteration > 0 and iteration % restart_interval == 0:
            current = best.deep_copy()
            current_cost = best_cost
            T = temperature_start * 0.5

        iteration += 1
        active = current.active_lines()
        if not active:
            break

        move_type = pick_move_type()
        delta: float | None = None
        accepted = False

        if move_type == "transfer" and len(active) >= 1:
            from_line = rng.choice(active)
            if not from_line.case_indices:
                continue
            case_idx = rng.choice(list(from_line.case_indices))
            case = cases[case_idx]

            # Try K random target lines (plus possibly a new line)
            candidates = rng.sample(active, min(10, len(active)))
            best_delta = math.inf
            best_target = None
            for to_line in candidates:
                if to_line is from_line:
                    continue
                d = _try_transfer(
                    case, from_line, to_line, cases,
                    requirement_columns, weights, support, max_cases_per_line,
                )
                if d is not None and d < best_delta:
                    best_delta = d
                    best_target = to_line

            if best_target is not None:
                delta = best_delta
                if delta < 0 or (T > 1e-9 and rng.random() < math.exp(-delta / T)):
                    apply_transfer(case, from_line, best_target, current)
                    current_cost += delta
                    accepted = True

        elif move_type == "swap" and len(active) >= 2:
            line_a, line_b = rng.sample(active, 2)
            if not line_a.case_indices or not line_b.case_indices:
                continue
            case_a = cases[rng.choice(list(line_a.case_indices))]
            case_b = cases[rng.choice(list(line_b.case_indices))]

            delta = _try_swap(
                case_a, line_a, case_b, line_b,
                cases, requirement_columns, weights, support,
            )
            if delta is not None:
                if delta < 0 or (T > 1e-9 and rng.random() < math.exp(-delta / T)):
                    apply_swap(case_a, line_a, case_b, line_b, current)
                    current_cost += delta
                    accepted = True

        elif move_type == "merge" and len(active) >= 2:
            # Prefer merging small lines
            by_size = sorted(active, key=lambda l: len(l.case_indices))
            line_a = by_size[0]
            candidates = rng.sample(by_size[1:], min(5, len(by_size) - 1))
            best_delta = math.inf
            best_partner = None
            for line_b in candidates:
                d = _try_merge_lines(
                    line_a, line_b, cases, requirement_columns, weights, support, max_cases_per_line,
                )
                if d is not None and d < best_delta:
                    best_delta = d
                    best_partner = line_b
            if best_partner is not None:
                delta = best_delta
                if delta < 0 or (T > 1e-9 and rng.random() < math.exp(-delta / T)):
                    apply_merge(line_a, best_partner, current)
                    current_cost += delta
                    accepted = True

        elif move_type == "split":
            # Pick highest-cost line to split
            by_cost = sorted(
                active,
                key=lambda l: l.get_cost(cases, requirement_columns, weights, support),
                reverse=True,
            )
            for line in by_cost[:3]:
                result = _try_split_line(line, cases, requirement_columns, weights, support, rng)
                if result is not None:
                    delta, group_a, group_b = result
                    # Split always reduces cost (delta < 0 guaranteed by _try_split_line)
                    apply_split(line, group_a, group_b, current)
                    current_cost += delta
                    accepted = True
                    break

        if accepted and current_cost < best_cost - 1e-9:
            best = current.deep_copy()
            best_cost = current_cost
            improvements += 1
            # Reward this move type
            move_weights[move_type] = min(80, move_weights[move_type] + 2)

        T *= cooling_rate

        # Progress report
        if verbose and time.monotonic() - last_report >= 10.0:
            last_report = time.monotonic()
            elapsed = last_report - start_time
            n_lines = len(current.active_lines())
            print(
                f"  t={elapsed:.0f}s iter={iteration} T={T:.4f} "
                f"current_cost={current_cost:.2f} best_cost={best_cost:.2f} "
                f"lines={n_lines} improvements={improvements}"
            )

    return best


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_assignment(
    assignment: Assignment,
    cases: list[TestCase],
    requirement_columns: list[str],
    weights: EquipmentWeights,
    support: RuBandSupport,
    max_cases_per_line: int,
) -> None:
    # 1. Every case appears on exactly one line
    assigned = set()
    for line in assignment.active_lines():
        for idx in line.case_indices:
            if idx in assigned:
                raise SystemExit(f"validation failed: case index {idx} appears on multiple lines")
            assigned.add(idx)
    all_indices = {case.index for case in cases}
    if assigned != all_indices:
        missing = all_indices - assigned
        missing_ids = [cases[i].tc_id for i in sorted(missing)]
        raise SystemExit(f"validation failed: unassigned cases: {missing_ids}")

    for line in assignment.active_lines():
        # 2. Capacity
        if len(line.case_indices) > max_cases_per_line:
            raise SystemExit(
                f"validation failed: line {line.line_id} has {len(line.case_indices)} cases "
                f"(limit {max_cases_per_line})"
            )
        spec = line.get_spec(cases, requirement_columns, weights, support)
        raw_spec = line.get_raw_spec(cases, requirement_columns, weights, support)
        # 3. Single-select columns must not contain multiple concrete values.
        for column in SINGLE_SELECT_COLUMNS:
            if column not in raw_spec:
                continue
            concrete = [token for token in raw_spec[column] if not is_any(token)]
            if len(concrete) > 1:
                raise SystemExit(
                    f"validation failed: line {line.line_id} has multiple concrete values in {column}"
                )
        # 4. RU-band compatibility
        if not spec_has_compatible_ru_bands(spec, support):
            raise SystemExit(f"validation failed: line {line.line_id} has incompatible RU-band spec")
        # 5. Every case is covered by its line's spec
        for idx in line.case_indices:
            case = cases[idx]
            ok, _ = coverage_delta(
                requirement_columns, spec, case, enforce_delta=False, support=support,
            )
            if not ok:
                raise SystemExit(
                    f"validation failed: case {case.tc_id} not covered by line {line.line_id}"
                )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(
    path: Path,
    input_columns: list[str],
    requirement_columns: list[str],
    cases: list[TestCase],
    assignment: Assignment,
    weights: EquipmentWeights,
    support: RuBandSupport,
    initial_cost: float,
    solve_status: str,
) -> None:
    # Force recompute all specs before writing
    for line in assignment.active_lines():
        line.get_spec(cases, requirement_columns, weights, support)

    # Group lines by spec signature for spec_id assignment
    from solve_test_lines import spec_signature

    sig_to_spec_id: dict[tuple, str] = {}
    spec_counter = [0]

    def get_spec_id(spec: dict) -> str:
        sig = spec_signature(spec)
        if sig not in sig_to_spec_id:
            spec_counter[0] += 1
            sig_to_spec_id[sig] = f"spec_{spec_counter[0]}"
        return sig_to_spec_id[sig]

    output_columns = [
        "line_id",
        "spec_id",
        "line_cost",
        "equipment_count",
        "du_count",
        "ru_count",
        "ue_count",
        "covered_tc_ids",
        "covered_count",
        "solve_status",
    ] + [col for col in input_columns if col != "tc_id"]

    # Sort lines: by spec then by first case index
    sorted_lines = sorted(
        assignment.active_lines(),
        key=lambda l: (
            l.get_cost(cases, requirement_columns, weights, support),
            -len(l.case_indices),
            min(l.case_indices),
        ),
    )

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_columns)
        writer.writeheader()

        for line_number, line in enumerate(sorted_lines, start=1):
            spec = line.get_spec(cases, requirement_columns, weights, support)
            covered_ids = sorted(line.case_indices)
            du = sum(
                numeric_equipment(spec.get(col, ()))
                for col in DU_COLUMNS
                if col in requirement_columns
            )
            ru = len(spec.get(RU_COLUMN, ())) if RU_COLUMN in requirement_columns else 0
            ue = numeric_equipment(spec.get(UE_COLUMN, ())) if UE_COLUMN in requirement_columns else 0
            cost = line.get_cost(cases, requirement_columns, weights, support)
            eq_count = du + ru + ue

            row: dict[str, object] = {
                "line_id": f"line_{line_number}",
                "spec_id": get_spec_id(spec),
                "line_cost": f"{cost:.2f}",
                "equipment_count": eq_count,
                "du_count": du,
                "ru_count": ru,
                "ue_count": ue,
                "covered_tc_ids": " + ".join(cases[i].tc_id for i in covered_ids),
                "covered_count": len(covered_ids),
                "solve_status": solve_status,
            }
            for col in input_columns:
                if col == "tc_id":
                    continue
                if col in requirement_columns:
                    row[col] = render_cell(spec.get(col, ()))
                else:
                    row[col] = ""
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)
    started_at = time.monotonic()

    input_path = Path(args.input)
    output_path = Path(args.output)
    support_path = Path(args.ru_band_support)

    print(f"Loading cases from {input_path} ...")
    input_columns, cases = load_cases(input_path)
    support = load_ru_band_support(support_path)

    requirement_columns = [col for col in input_columns if col != "tc_id"]
    if args.ignore_tech_and_ue_capa:
        requirement_columns = [
            col for col in requirement_columns
            if not is_temporarily_ignored_column(col)
        ]

    validate_support_references(requirement_columns, cases, support)
    weights = EquipmentWeights(du=args.du_weight, ru=args.ru_weight, ue=args.ue_weight)

    print(
        f"Cases: {len(cases)}  |  max_cases_per_line: {args.max_cases_per_line}  |  "
        f"weights DU={weights.du} RU={weights.ru} UE={weights.ue}"
    )

    # Phase 1: greedy initial solution
    print("Building greedy initial solution ...")
    initial = greedy_initial(
        cases, requirement_columns, weights, args.max_cases_per_line, support, rng,
    )
    initial_cost = initial.total_cost(cases, requirement_columns, weights, support)
    n_initial_lines = len(initial.active_lines())
    print(f"Initial solution: {n_initial_lines} lines, total cost = {initial_cost:.2f}")

    # Phase 2: local search
    print(f"Running local search for {args.time_limit:.0f}s ...")
    best = local_search(
        initial=initial,
        cases=cases,
        requirement_columns=requirement_columns,
        weights=weights,
        max_cases_per_line=args.max_cases_per_line,
        support=support,
        time_limit=args.time_limit,
        temperature_start=args.temperature_start,
        cooling_rate=args.cooling_rate,
        restart_interval=args.restart_interval,
        rng=rng,
        verbose=args.verbose,
    )

    final_cost = best.total_cost(cases, requirement_columns, weights, support)
    final_lines = best.active_lines()
    n_final_lines = len(final_lines)

    # Force recompute specs before validation
    for line in final_lines:
        line.get_spec(cases, requirement_columns, weights, support)

    print("Validating solution ...")
    validate_assignment(best, cases, requirement_columns, weights, support, args.max_cases_per_line)

    # Gather unique specs
    unique_specs: set[tuple] = set()
    for line in final_lines:
        unique_specs.add(
            spec_signature(line.get_spec(cases, requirement_columns, weights, support))
        )

    improvement_pct = 100.0 * (initial_cost - final_cost) / initial_cost if initial_cost > 0 else 0.0
    elapsed = time.monotonic() - started_at

    write_output(
        output_path,
        input_columns,
        requirement_columns,
        cases,
        best,
        weights,
        support,
        initial_cost,
        solve_status="LOCAL_SEARCH",
    )

    print(f"\n{'='*50}")
    print(f"status=LOCAL_SEARCH")
    print(f"runtime_seconds={elapsed:.2f}")
    print(f"input_testcases={len(cases)}")
    print(f"total_lines={n_final_lines}")
    print(f"unique_specs={len(unique_specs)}")
    print(f"initial_cost={initial_cost:.2f}")
    print(f"final_cost={final_cost:.2f}")
    print(f"improvement={improvement_pct:.1f}%")
    print(f"output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
