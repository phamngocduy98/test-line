"""
test_solve_lines.py — Test suite for the local-search line packer.

Mirrors the structure of test_solve_test_lines.py.
Run with:  python -m pytest test_solve_lines.py -v
"""

from __future__ import annotations

import csv
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solve_test_lines import (
    RuBandSupport,
    TestCase,
    parse_cell,
    render_cell,
)
from solve_lines import (
    Assignment,
    EquipmentWeights,
    Line,
    apply_merge,
    apply_split,
    apply_swap,
    apply_transfer,
    greedy_initial,
    local_search,
    main,
    parse_args,
    spec_cost,
    validate_assignment,
    write_output,
    _merge_spec_with_case,
    _ss_compatible,
    _try_merge_lines,
    _try_split_line,
    _try_swap,
    _try_transfer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_case(index: int, tc_id: str, **values: str) -> TestCase:
    return TestCase(
        index=index,
        tc_id=tc_id,
        raw=values,
        tokens={col: parse_cell(v) for col, v in values.items()},
    )


def make_support(
    rus: list[str] | None = None,
    lte_bands: list[str] | None = None,
    nr_bands: list[str] | None = None,
) -> RuBandSupport:
    rus = rus or ["rf-1", "rf-2"]
    lte_bands = lte_bands or ["b1", "b3"]
    nr_bands = nr_bands or ["n41", "n78"]
    return RuBandSupport(
        ru_names={r: r for r in rus},
        lte_band_names={b: b for b in lte_bands},
        nr_band_names={b: b for b in nr_bands},
        lte_by_ru={r: frozenset(lte_bands) for r in rus},
        nr_by_ru={r: frozenset(nr_bands) for r in rus},
    )


def make_weights(du=1.0, ru=1.0, ue=1.0) -> EquipmentWeights:
    return EquipmentWeights(du=du, ru=ru, ue=ue)


def make_line(cases: list[TestCase], *case_indices: int) -> Line:
    line = Line()
    for idx in case_indices:
        line.case_indices.add(idx)
    line.mark_dirty()
    return line


def simple_assignment(cases: list[TestCase], columns: list[str]) -> Assignment:
    """One line per case."""
    assignment = Assignment.empty()
    for case in cases:
        line = Line()
        assignment.add_line(line)
        assignment.assign(case.index, line)
    return assignment


# ---------------------------------------------------------------------------
# Cost model tests
# ---------------------------------------------------------------------------

class CostModelTests(unittest.TestCase):
    def setUp(self):
        self.columns = ["enb", "ru", "ue"]
        self.weights = make_weights()

    def test_empty_spec_has_zero_cost(self):
        spec = {"enb": (), "ru": (), "ue": ()}
        self.assertEqual(spec_cost(spec, self.columns, self.weights), 0.0)

    def test_cost_counts_du_ru_ue_with_unit_weights(self):
        spec = {"enb": ("2",), "ru": ("rf-1", "rf-2"), "ue": ("3",)}
        # du=2, ru=2, ue=3 → 7
        self.assertEqual(spec_cost(spec, self.columns, self.weights), 7.0)

    def test_cost_applies_custom_weights(self):
        spec = {"enb": ("1",), "ru": ("rf-1",), "ue": ("1",)}
        weights = make_weights(du=2.0, ru=3.0, ue=0.5)
        # du=1*2 + ru=1*3 + ue=1*0.5 = 5.5
        self.assertEqual(spec_cost(spec, self.columns, weights), 5.5)

    def test_cost_ignores_columns_not_in_requirement_columns(self):
        spec = {"enb": ("2",), "ru": ("rf-1",), "ue": ("1",), "lte band": ("b1",)}
        cost_with = spec_cost(spec, ["enb", "ru", "ue", "lte band"], self.weights)
        cost_without = spec_cost(spec, ["enb", "ru", "ue"], self.weights)
        self.assertEqual(cost_with, cost_without)  # lte band doesn't add cost

    def test_any_token_counts_as_one(self):
        spec = {"ru": ("any",), "enb": (), "ue": ()}
        # ru slot = 1
        self.assertEqual(spec_cost(spec, self.columns, self.weights), 1.0)


# ---------------------------------------------------------------------------
# Line state tests
# ---------------------------------------------------------------------------

class LineStateTests(unittest.TestCase):
    def setUp(self):
        self.columns = ["ru", "enb"]
        self.weights = make_weights()
        self.cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-2", enb="2"),
            make_case(2, "C", ru="rf-1", enb="1"),
        ]

    def test_empty_line_has_zero_cost(self):
        line = Line()
        cost = line.get_cost(self.cases, self.columns, self.weights)
        self.assertEqual(cost, 0.0)

    def test_single_case_cost_matches_spec_cost(self):
        line = make_line(self.cases, 0)
        cost = line.get_cost(self.cases, self.columns, self.weights)
        # enb=1 (du), ru=1 → cost=2
        self.assertEqual(cost, 2.0)

    def test_merging_two_cases_takes_max_enb(self):
        line = make_line(self.cases, 0, 1)
        cost = line.get_cost(self.cases, self.columns, self.weights)
        # enb=max(1,2)=2, ru=rf-1+rf-2=2 → 4
        self.assertEqual(cost, 4.0)

    def test_cost_if_add_does_not_mutate_line(self):
        line = make_line(self.cases, 0)
        original_cost = line.get_cost(self.cases, self.columns, self.weights)
        line.cost_if_add(self.cases[1], self.columns, self.weights, self.cases)
        self.assertEqual(line.get_cost(self.cases, self.columns, self.weights), original_cost)
        self.assertNotIn(1, line.case_indices)

    def test_cost_if_add_returns_correct_hypothetical(self):
        line = make_line(self.cases, 0)
        hypothetical = line.cost_if_add(self.cases[1], self.columns, self.weights, self.cases)
        # enb=2, ru=2 → 4
        self.assertEqual(hypothetical, 4.0)

    def test_cost_if_remove_returns_correct_hypothetical(self):
        line = make_line(self.cases, 0, 1)
        # Remove case 1 (rf-2, enb=2); remaining: case 0 (rf-1, enb=1) → cost=2
        hypothetical = line.cost_if_remove(self.cases[1], self.columns, self.weights, self.cases)
        self.assertEqual(hypothetical, 2.0)

    def test_cost_if_swap_returns_correct_hypothetical(self):
        line = make_line(self.cases, 0, 1)
        # Swap case 1 out, case 2 in: cases 0 and 2 both rf-1, enb max(1,1)=1 → cost=2
        hypothetical = line.cost_if_swap(
            self.cases[2], self.cases[1], self.columns, self.weights, self.cases
        )
        self.assertEqual(hypothetical, 2.0)

    def test_dirty_flag_triggers_recompute(self):
        line = make_line(self.cases, 0)
        _ = line.get_cost(self.cases, self.columns, self.weights)
        self.assertFalse(line._dirty)
        line.mark_dirty()
        self.assertTrue(line._dirty)
        _ = line.get_cost(self.cases, self.columns, self.weights)
        self.assertFalse(line._dirty)


# ---------------------------------------------------------------------------
# Single-select compatibility tests
# ---------------------------------------------------------------------------

class SingleSelectTests(unittest.TestCase):
    def test_empty_ss_key_compatible_with_anything(self):
        case = make_case(0, "A", **{"cc location": "intra cc"})
        self.assertTrue(_ss_compatible((), case))

    def test_matching_concrete_value_is_compatible(self):
        case = make_case(0, "A", **{"cc location": "intra cc"})
        ss_key = (("cc location", ("intra cc",)),)
        self.assertTrue(_ss_compatible(ss_key, case))

    def test_conflicting_concrete_value_is_incompatible(self):
        case = make_case(0, "A", **{"cc location": "inter cc"})
        ss_key = (("cc location", ("intra cc",)),)
        self.assertFalse(_ss_compatible(ss_key, case))

    def test_any_in_case_is_always_compatible(self):
        case = make_case(0, "A", **{"cc location": "any"})
        ss_key = (("cc location", ("intra cc",)),)
        self.assertTrue(_ss_compatible(ss_key, case))


# ---------------------------------------------------------------------------
# Merge spec helper tests
# ---------------------------------------------------------------------------

class MergeSpecTests(unittest.TestCase):
    def test_merge_with_compatible_case(self):
        columns = ["ru", "enb"]
        spec = {"ru": ("rf-1",), "enb": ("1",)}
        case = make_case(0, "A", ru="rf-2", enb="2")
        result = _merge_spec_with_case(spec, case, columns)
        self.assertIsNotNone(result)
        self.assertEqual(result["enb"], ("2",))

    def test_merge_with_single_select_conflict_returns_none(self):
        columns = ["cc location"]
        spec = {"cc location": ("intra cc",)}
        case = make_case(0, "A", **{"cc location": "inter cc"})
        result = _merge_spec_with_case(spec, case, columns)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Move feasibility tests
# ---------------------------------------------------------------------------

class MoveTests(unittest.TestCase):
    def setUp(self):
        self.columns = ["ru", "enb"]
        self.weights = make_weights()
        self.cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-2", enb="2"),
            make_case(2, "C", ru="rf-1", enb="1"),
        ]

    def _make_assignment_two_lines(self):
        """line_a has cases 0,2; line_b has case 1."""
        assignment = Assignment.empty()
        line_a = Line()
        line_b = Line()
        assignment.add_line(line_a)
        assignment.add_line(line_b)
        assignment.assign(0, line_a)
        assignment.assign(2, line_a)
        assignment.assign(1, line_b)
        return assignment, line_a, line_b

    def test_transfer_feasible_reduces_cost(self):
        assignment, line_a, line_b = self._make_assignment_two_lines()
        # Move case 2 (rf-1,enb=1) from line_a to line_b
        # line_a before: rf-1+rf-1 merged→rf-1, enb=1 → cost=2
        # line_b before: rf-2, enb=2 → cost=3
        # line_a after (just case 0): rf-1, enb=1 → cost=2
        # line_b after (cases 1,2): rf-1+rf-2, enb=2 → cost=4
        # delta = (2+4)-(2+3) = +1 → not beneficial
        delta = _try_transfer(
            self.cases[2], line_a, line_b,
            self.cases, self.columns, self.weights, max_cases_per_line=10,
        )
        self.assertIsNotNone(delta)
        self.assertAlmostEqual(delta, 1.0)

    def test_transfer_rejected_when_at_capacity(self):
        assignment, line_a, line_b = self._make_assignment_two_lines()
        delta = _try_transfer(
            self.cases[2], line_a, line_b,
            self.cases, self.columns, self.weights, max_cases_per_line=1,
        )
        self.assertIsNone(delta)

    def test_swap_is_symmetric_in_cost_evaluation(self):
        assignment, line_a, line_b = self._make_assignment_two_lines()
        delta_ab = _try_swap(
            self.cases[0], line_a, self.cases[1], line_b,
            self.cases, self.columns, self.weights,
        )
        delta_ba = _try_swap(
            self.cases[1], line_b, self.cases[0], line_a,
            self.cases, self.columns, self.weights,
        )
        self.assertIsNotNone(delta_ab)
        self.assertIsNotNone(delta_ba)
        self.assertAlmostEqual(delta_ab, delta_ba)

    def test_merge_rejected_when_combined_exceeds_capacity(self):
        assignment, line_a, line_b = self._make_assignment_two_lines()
        delta = _try_merge_lines(
            line_a, line_b, self.cases, self.columns, self.weights, max_cases_per_line=2,
        )
        self.assertIsNone(delta)

    def test_merge_feasible_when_capacity_allows(self):
        assignment, line_a, line_b = self._make_assignment_two_lines()
        delta = _try_merge_lines(
            line_a, line_b, self.cases, self.columns, self.weights, max_cases_per_line=10,
        )
        self.assertIsNotNone(delta)

    def test_split_returns_none_for_single_case_line(self):
        line = make_line(self.cases, 0)
        rng = random.Random(1)
        result = _try_split_line(line, self.cases, self.columns, self.weights, rng)
        self.assertIsNone(result)

    def test_split_reduces_cost_when_cases_are_dissimilar(self):
        """A line with rf-1 and rf-2 costs more than two single-ru lines."""
        line = make_line(self.cases, 0, 1)
        rng = random.Random(1)
        # line cost: rf-1+rf-2, enb=2 → cost=4
        # split: (rf-1,enb=1)+(rf-2,enb=2) → cost=2+3=5? No, split merges groups.
        # group_a=[case0], group_b=[case1]: cost=2+3=5 > 4 → no improvement
        result = _try_split_line(line, self.cases, self.columns, self.weights, rng)
        # With only ru+enb, the merged cost is 4, split is 5; no improvement
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Move application tests
# ---------------------------------------------------------------------------

class ApplyMoveTests(unittest.TestCase):
    def setUp(self):
        self.columns = ["ru", "enb"]
        self.weights = make_weights()
        self.cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-2", enb="2"),
            make_case(2, "C", ru="rf-1", enb="1"),
        ]

    def _two_line_assignment(self):
        assignment = Assignment.empty()
        line_a = Line()
        line_b = Line()
        assignment.add_line(line_a)
        assignment.add_line(line_b)
        assignment.assign(0, line_a)
        assignment.assign(2, line_a)
        assignment.assign(1, line_b)
        return assignment, line_a, line_b

    def test_apply_transfer_moves_case(self):
        assignment, line_a, line_b = self._two_line_assignment()
        apply_transfer(self.cases[2], line_a, line_b, assignment)
        self.assertNotIn(2, line_a.case_indices)
        self.assertIn(2, line_b.case_indices)
        self.assertEqual(assignment.case_to_line[2], line_b.line_id)

    def test_apply_transfer_removes_empty_line(self):
        assignment = Assignment.empty()
        line_a = Line()
        line_b = Line()
        assignment.add_line(line_a)
        assignment.add_line(line_b)
        assignment.assign(0, line_a)
        assignment.assign(1, line_b)
        # Transfer the only case from line_a to line_b → line_a should be removed
        apply_transfer(self.cases[0], line_a, line_b, assignment)
        self.assertNotIn(line_a, assignment.lines)

    def test_apply_swap_exchanges_cases(self):
        assignment, line_a, line_b = self._two_line_assignment()
        apply_swap(self.cases[0], line_a, self.cases[1], line_b, assignment)
        self.assertIn(1, line_a.case_indices)
        self.assertIn(0, line_b.case_indices)
        self.assertNotIn(0, line_a.case_indices)
        self.assertNotIn(1, line_b.case_indices)

    def test_apply_merge_combines_and_removes_source(self):
        assignment, line_a, line_b = self._two_line_assignment()
        apply_merge(line_a, line_b, assignment)
        self.assertNotIn(line_b, assignment.lines)
        self.assertEqual(line_a.case_indices, {0, 1, 2})

    def test_apply_split_creates_new_line(self):
        assignment = Assignment.empty()
        line = Line()
        assignment.add_line(line)
        for i in range(4):
            assignment.assign(i, line)
        n_before = len(assignment.lines)
        apply_split(line, [0, 1], [2, 3], assignment)
        self.assertEqual(len(assignment.lines), n_before + 1)
        self.assertEqual(line.case_indices, {0, 1})
        # Cases 2,3 must be on a new line
        all_assigned = set()
        for l in assignment.lines:
            all_assigned |= l.case_indices
        self.assertEqual(all_assigned, {0, 1, 2, 3})


# ---------------------------------------------------------------------------
# Greedy initial solution tests
# ---------------------------------------------------------------------------

class GreedyInitialTests(unittest.TestCase):
    def setUp(self):
        self.columns = ["ru", "enb"]
        self.weights = make_weights()
        self.support = make_support()
        self.rng = random.Random(1)

    def test_all_cases_are_assigned(self):
        cases = [make_case(i, str(i), ru="rf-1", enb="1") for i in range(10)]
        assignment = greedy_initial(
            cases, self.columns, self.weights, 5, self.support, self.rng
        )
        assigned = set()
        for line in assignment.active_lines():
            assigned |= line.case_indices
        self.assertEqual(assigned, {c.index for c in cases})

    def test_no_line_exceeds_capacity(self):
        cases = [make_case(i, str(i), ru="rf-1", enb="1") for i in range(20)]
        assignment = greedy_initial(
            cases, self.columns, self.weights, 5, self.support, self.rng
        )
        for line in assignment.active_lines():
            self.assertLessEqual(len(line.case_indices), 5)

    def test_single_case_produces_one_line(self):
        cases = [make_case(0, "A", ru="rf-1", enb="1")]
        assignment = greedy_initial(
            cases, self.columns, self.weights, 10, self.support, self.rng
        )
        self.assertEqual(len(assignment.active_lines()), 1)

    def test_incompatible_single_select_cases_go_to_different_lines(self):
        cases = [
            make_case(0, "A", **{"cc location": "intra cc"}),
            make_case(1, "B", **{"cc location": "inter cc"}),
        ]
        columns = ["cc location"]
        assignment = greedy_initial(
            cases, columns, self.weights, 10, self.support, self.rng
        )
        lines = assignment.active_lines()
        for line in lines:
            # Each line must not have both cases
            self.assertLess(len(line.case_indices), 2)

    def test_greedy_packs_identical_cases_together(self):
        """Identical cases should go on the same line (zero marginal cost)."""
        cases = [make_case(i, str(i), ru="rf-1", enb="1") for i in range(5)]
        assignment = greedy_initial(
            cases, self.columns, self.weights, 10, self.support, self.rng
        )
        # All 5 fit on one line with no marginal cost increase
        self.assertEqual(len(assignment.active_lines()), 1)


# ---------------------------------------------------------------------------
# Deep copy tests
# ---------------------------------------------------------------------------

class AssignmentCopyTests(unittest.TestCase):
    def test_deep_copy_is_independent(self):
        cases = [make_case(0, "A", ru="rf-1", enb="1"), make_case(1, "B", ru="rf-2", enb="2")]
        columns = ["ru", "enb"]
        weights = make_weights()
        support = make_support()
        rng = random.Random(1)
        original = greedy_initial(cases, columns, weights, 10, support, rng)
        copy = original.deep_copy()

        # Mutate original — copy should not change
        first_line = original.active_lines()[0]
        first_line.mark_dirty()

        copy_line = copy.active_lines()[0]
        # Copy's dirty flag should be independent
        self.assertIsNot(first_line, copy_line)


# ---------------------------------------------------------------------------
# Local search integration tests
# ---------------------------------------------------------------------------

class LocalSearchTests(unittest.TestCase):
    def setUp(self):
        self.columns = ["ru", "enb"]
        self.weights = make_weights()
        self.support = make_support()
        self.rng = random.Random(42)

    def _run_search(self, cases, max_per_line=10, time_limit=2.0):
        initial = greedy_initial(
            cases, self.columns, self.weights, max_per_line, self.support, self.rng
        )
        return local_search(
            initial=initial,
            cases=cases,
            requirement_columns=self.columns,
            weights=self.weights,
            max_cases_per_line=max_per_line,
            time_limit=time_limit,
            temperature_start=2.0,
            cooling_rate=0.99,
            restart_interval=5000,
            rng=self.rng,
        )

    def test_search_assigns_all_cases(self):
        cases = [make_case(i, str(i), ru="rf-1", enb="1") for i in range(20)]
        result = self._run_search(cases)
        assigned = set()
        for line in result.active_lines():
            assigned |= line.case_indices
        self.assertEqual(assigned, {c.index for c in cases})

    def test_search_respects_capacity(self):
        cases = [make_case(i, str(i), ru="rf-1", enb="1") for i in range(15)]
        result = self._run_search(cases, max_per_line=5)
        for line in result.active_lines():
            self.assertLessEqual(len(line.case_indices), 5)

    def test_search_does_not_increase_cost_vs_initial(self):
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-2", enb="2"),
            make_case(2, "C", ru="rf-1", enb="1"),
            make_case(3, "D", ru="rf-2", enb="2"),
        ]
        initial = greedy_initial(
            cases, self.columns, self.weights, 10, self.support, self.rng
        )
        initial_cost = initial.total_cost(cases, self.columns, self.weights)
        result = self._run_search(cases)
        final_cost = result.total_cost(cases, self.columns, self.weights)
        self.assertLessEqual(final_cost, initial_cost + 1e-6)

    def test_search_merges_compatible_lines(self):
        """Two lines of identical cases should be merged into one."""
        cases = [make_case(i, str(i), ru="rf-1", enb="1") for i in range(6)]
        result = self._run_search(cases, max_per_line=10, time_limit=5.0)
        # All cases identical → should end up on one line
        self.assertEqual(len(result.active_lines()), 1)

    def test_weighted_cost_prefers_fewer_ru_when_ru_weight_high(self):
        """With very high RU weight, search should consolidate RU slots."""
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-1", enb="1"),
            make_case(2, "C", ru="rf-1", enb="1"),
        ]
        weights_high_ru = EquipmentWeights(du=1.0, ru=100.0, ue=1.0)
        initial = greedy_initial(
            cases, self.columns, weights_high_ru, 10, self.support, random.Random(1)
        )
        result = local_search(
            initial=initial,
            cases=cases,
            requirement_columns=self.columns,
            weights=weights_high_ru,
            max_cases_per_line=10,
            time_limit=3.0,
            temperature_start=2.0,
            cooling_rate=0.99,
            restart_interval=5000,
            rng=random.Random(1),
        )
        # All cases have rf-1; they should merge onto one line
        self.assertEqual(len(result.active_lines()), 1)


# ---------------------------------------------------------------------------
# Key scenario: 400 cases, 250-per-line limit (from the problem statement)
# ---------------------------------------------------------------------------

class LinePackingScenarioTests(unittest.TestCase):
    """
    Reproduce the motivating example:
    400 test cases, max 250 per line.
    Spec A (1DU, rf-1, 1UE) covers 200 cases.
    Spec B (2DU, rf-2, 1UE) covers 200 cases.

    Current (set-cover) approach might produce:
      2 lines with 1 spec (2DU, rf-1+rf-2, 1UE): cost = 2*(2+2+1) = 10
    Optimal packing:
      3 lines with 2 specs:
        line1: spec A, 100 cases → cost = 1+1+1 = 3
        line2: spec A, 100 cases → cost = 3
        line3: spec B, 200 cases → cost = 2+1+1 = 4
      Total = 10 ... same! But with different distribution.

    Actually, the optimal is:
      line1: spec A, 200 cases → cost 3 (fits, 200 ≤ 250)
      line2: spec B, 200 cases → cost 4
      Total = 7

    The spec-count solver would merge into 1 spec (2DU, rf-1+rf-2, 1UE)
    needing 2 physical lines: cost = 2*5 = 10.
    Our packer should find cost = 7.
    """

    def test_packer_beats_merged_single_spec(self):
        columns = ["enb", "ru", "ue"]
        weights = make_weights()
        support = make_support(
            rus=["rf-1", "rf-2"],
            lte_bands=["b1"],
            nr_bands=["n1"],
        )
        rng = random.Random(42)

        # 200 cases requiring rf-1, 1DU, 1UE
        cases_a = [make_case(i, f"A{i}", enb="1", ru="rf-1", ue="1") for i in range(200)]
        # 200 cases requiring rf-2, 2DU, 1UE
        cases_b = [
            make_case(200 + i, f"B{i}", enb="2", ru="rf-2", ue="1") for i in range(200)
        ]
        all_cases = cases_a + cases_b

        assignment = greedy_initial(
            all_cases, columns, weights, 250, support, rng
        )
        result = local_search(
            initial=assignment,
            cases=all_cases,
            requirement_columns=columns,
            weights=weights,
            max_cases_per_line=250,
            time_limit=10.0,
            temperature_start=2.0,
            cooling_rate=0.999,
            restart_interval=10000,
            rng=rng,
        )

        final_cost = result.total_cost(all_cases, columns, weights)
        # Optimal: line(rf-1,1DU,1UE) + line(rf-2,2DU,1UE) = 3+4 = 7
        # Merged spec would cost: (rf-1+rf-2, 2DU, 1UE) × 2 lines = 5*2 = 10
        self.assertLessEqual(final_cost, 8.0)  # allow slight suboptimality

        # Verify no line exceeds capacity
        for line in result.active_lines():
            self.assertLessEqual(len(line.case_indices), 250)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.columns = ["ru", "enb"]
        self.weights = make_weights()
        self.support = make_support()

    def test_valid_assignment_passes(self):
        cases = [make_case(0, "A", ru="rf-1", enb="1")]
        assignment = Assignment.empty()
        line = Line()
        assignment.add_line(line)
        assignment.assign(0, line)
        line.get_spec(cases, self.columns, self.weights)
        validate_assignment(assignment, cases, self.columns, self.support, 10)

    def test_unassigned_case_fails(self):
        cases = [make_case(0, "A", ru="rf-1", enb="1"), make_case(1, "B", ru="rf-2", enb="2")]
        assignment = Assignment.empty()
        line = Line()
        assignment.add_line(line)
        assignment.assign(0, line)
        line.get_spec(cases, self.columns, self.weights)
        with self.assertRaisesRegex(SystemExit, "unassigned"):
            validate_assignment(assignment, cases, self.columns, self.support, 10)

    def test_over_capacity_line_fails(self):
        cases = [make_case(i, str(i), ru="rf-1", enb="1") for i in range(5)]
        assignment = Assignment.empty()
        line = Line()
        assignment.add_line(line)
        for case in cases:
            assignment.assign(case.index, line)
        line.get_spec(cases, self.columns, self.weights)
        with self.assertRaisesRegex(SystemExit, "limit"):
            validate_assignment(assignment, cases, self.columns, self.support, 3)


# ---------------------------------------------------------------------------
# Output tests
# ---------------------------------------------------------------------------

class OutputTests(unittest.TestCase):
    def test_write_output_produces_correct_columns(self):
        columns = ["tc_id", "ru", "enb"]
        requirement_columns = ["ru", "enb"]
        weights = make_weights()
        support = make_support()
        cases = [make_case(0, "A", ru="rf-1", enb="1"), make_case(1, "B", ru="rf-1", enb="1")]

        assignment = Assignment.empty()
        line = Line()
        assignment.add_line(line)
        assignment.assign(0, line)
        assignment.assign(1, line)
        line.get_spec(cases, requirement_columns, weights)

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.csv"
            write_output(
                path, columns, requirement_columns, cases, assignment,
                weights, support, initial_cost=5.0, solve_status="LOCAL_SEARCH",
            )
            with path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["solve_status"], "LOCAL_SEARCH")
        self.assertIn("line_id", rows[0])
        self.assertIn("spec_id", rows[0])
        self.assertEqual(rows[0]["covered_count"], "2")

    def test_multiple_lines_same_spec_share_spec_id(self):
        columns = ["tc_id", "ru"]
        requirement_columns = ["ru"]
        weights = make_weights()
        support = make_support()
        cases = [make_case(i, str(i), ru="rf-1") for i in range(4)]

        assignment = Assignment.empty()
        for i in range(0, 4, 2):
            line = Line()
            assignment.add_line(line)
            assignment.assign(i, line)
            assignment.assign(i + 1, line)
            line.get_spec(cases, requirement_columns, weights)

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.csv"
            write_output(
                path, columns, requirement_columns, cases, assignment,
                weights, support, initial_cost=4.0, solve_status="LOCAL_SEARCH",
            )
            with path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        spec_ids = {row["spec_id"] for row in rows}
        self.assertEqual(len(spec_ids), 1)  # both lines share the same spec


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):
    def test_parse_args_defaults(self):
        with patch("sys.argv", ["solve_lines.py", "--ru-band-support", "s.csv"]):
            args = parse_args()
        self.assertEqual(args.input, "input.csv")
        self.assertEqual(args.output, "output_lines.csv")
        self.assertEqual(args.max_cases_per_line, 250)
        self.assertEqual(args.du_weight, 1.0)
        self.assertEqual(args.ru_weight, 1.0)
        self.assertEqual(args.ue_weight, 1.0)
        self.assertEqual(args.time_limit, 300.0)
        self.assertEqual(args.seed, 42)

    def test_parse_args_accepts_custom_weights(self):
        with patch("sys.argv", [
            "solve_lines.py", "--ru-band-support", "s.csv",
            "--du-weight", "2.0", "--ru-weight", "3.0", "--ue-weight", "0.5",
        ]):
            args = parse_args()
        self.assertEqual(args.du_weight, 2.0)
        self.assertEqual(args.ru_weight, 3.0)
        self.assertEqual(args.ue_weight, 0.5)

    def test_main_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            inp = root / "input.csv"
            out = root / "output.csv"
            sup = root / "support.csv"

            inp.write_text(
                "tc_id,ru,enb\nA,rf-1,1\nB,rf-1,1\nC,rf-2,2\nD,rf-2,2\n",
                encoding="utf-8",
            )
            sup.write_text(
                "ru,lte_band,nr_band\nrf-1,b1,n1\nrf-2,b1,n1\n",
                encoding="utf-8",
            )

            with patch("sys.argv", [
                "solve_lines.py",
                "--input", str(inp),
                "--output", str(out),
                "--ru-band-support", str(sup),
                "--max-cases-per-line", "3",
                "--time-limit", "5",
                "--seed", "1",
            ]):
                self.assertEqual(main(), 0)

            with out.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertTrue(rows)
        covered = set()
        for row in rows:
            for tc_id in row["covered_tc_ids"].split(" + "):
                covered.add(tc_id.strip())
        self.assertEqual(covered, {"A", "B", "C", "D"})

        for row in rows:
            self.assertLessEqual(int(row["covered_count"]), 3)


if __name__ == "__main__":
    unittest.main()
