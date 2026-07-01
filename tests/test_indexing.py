from __future__ import annotations

import tempfile
import unittest
from itertools import combinations
from pathlib import Path

from test_line_solver.coverage import coverage_excess
from test_line_solver.candidates import generate_candidates
from test_line_solver.indexing import CoverageIndex
from test_line_solver.models import Candidate, SolveOptions, Token
from test_line_solver.optimizer import optimize
from test_line_solver.parsing import read_ru_band_csv, read_testcase_csv
from test_line_solver.support import build_support_table
from test_line_solver.validation import validate_testcases


class CoverageIndexTests(unittest.TestCase):
    def write(self, directory: Path, name: str, text: str) -> Path:
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def parsed(self, directory: Path, input_text: str, support_text: str):
        input_csv = read_testcase_csv(self.write(directory, "input.csv", input_text), require_ru=True)
        support = build_support_table(read_ru_band_csv(self.write(directory, "ru-band.csv", support_text)))
        validate_testcases(input_csv, support, final_solver=True)
        return input_csv, support

    def test_groups_identical_active_requirements_and_expands_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band,tech lte\nT1,RU1,b1,lte\nT2,RU1,b1,nr\nT3,RU2,b2,lte\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            index = CoverageIndex.build(parsed, support, SolveOptions(ignore_optional_columns=True))
            self.assertEqual(2, len(index.groups))
            self.assertEqual((0, 1), index.groups[0].row_indexes)
            self.assertEqual((2,), index.groups[1].row_indexes)
            self.assertEqual((0, 0, 1), index.row_to_group)
            self.assertEqual((0, 1), index.expand_group_mask(1))
            self.assertEqual((0, 1, 2), index.expand_group_mask(0b11))

    def test_indexed_coverage_matches_raw_coverage_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band,cc location,ue\nT1,RU1,b1,A,1\nT2,RU1,b1,A,1\nT3,RU2,b2,B,2\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            options = SolveOptions()
            index = CoverageIndex.build(parsed, support, options)
            spec = {
                "ru": (Token(("RU1", "RU2")),),
                "lte band": (Token(("b1", "b2")),),
                "cc location": (Token(("A", "B")),),
                "ue": (Token(("2",)),),
            }
            indexed = index.coverage_for_spec(spec)

            raw_rows = []
            raw_excess_by_group = {}
            for row_index, row in enumerate(parsed.rows):
                result = coverage_excess(row.tokens, spec, index.columns, support, options)
                if result is None:
                    continue
                raw_rows.append(row_index)
                group_index = index.row_to_group[row_index]
                if result.excess:
                    raw_excess_by_group[group_index] = result.excess

            self.assertEqual(tuple(raw_rows), indexed.row_indexes)
            self.assertEqual(raw_excess_by_group, indexed.excess_by_group)
            self.assertEqual(sum(raw_excess_by_group.get(index.row_to_group[row], 0) for row in raw_rows), indexed.weighted_excess(index.groups))

    def test_indexed_prefilters_preserve_raw_coverage_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band,cc location,ue\nT1,RU1,b1,A,1\nT2,RU2,b2,B,2\nT3,RU1,b1 + b2,intra,1\n",
                "ru,lte_band,nr_band\nRU1,b1 + b2,\nRU2,b2,\n",
            )
            options = SolveOptions(reject_spec_side_wildcard=("cc location",))
            index = CoverageIndex.build(parsed, support, options)
            specs = (
                {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)),), "cc location": (Token(("A",)),), "ue": (Token(("1",)),)},
                {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)),), "cc location": (Token(("any",)),), "ue": (Token(("1",)),)},
                {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)),), "cc location": (Token(("A",)),), "ue": (Token(("5",)),)},
                {"ru": (Token(("RU2",)),), "lte band": (Token(("b1",)),), "cc location": (Token(("A",)),), "ue": (Token(("1",)),)},
                {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)), Token(("b1",))), "cc location": (Token(("intra",)),), "ue": (Token(("1",)),)},
            )

            for spec in specs:
                indexed = index.coverage_for_spec(spec)
                raw_rows = []
                raw_excess = {}
                for row_index, row in enumerate(parsed.rows):
                    result = coverage_excess(row.tokens, spec, index.columns, support, options)
                    if result is None:
                        continue
                    raw_rows.append(row_index)
                    if result.excess:
                        raw_excess[index.row_to_group[row_index]] = result.excess
                self.assertEqual(tuple(raw_rows), indexed.row_indexes)
                self.assertEqual(raw_excess, indexed.excess_by_group)

            self.assertGreaterEqual(len(index.spec_compatibility_cache), 1)

    def test_grouped_optimizer_weights_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,b1\nT2,RU1,b1\nT3,RU2,b2\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            candidates = generate_candidates(parsed, support, SolveOptions(max_merge_width=5))
            solution = optimize(candidates, len(parsed.rows), 10.0)
            self.assertEqual(2, len(solution.candidates))
            self.assertEqual({0, 1, 2}, set(solution.assignments))
            self.assertEqual(0, sum(solution.assignments[index].assignment_excess[index] for index in range(3)))

    def test_optimizer_accepts_candidates_without_group_index_fields(self):
        candidates = (
            Candidate("a", {}, (0,), 1, frozenset({0}), {0: 0}),
            Candidate("b", {}, (1,), 1, frozenset({1}), {1: 0}),
            Candidate("ab", {}, (0, 1), 2, frozenset({0, 1}), {0: 1, 1: 1}),
        )
        solution = optimize(candidates, 2, 10.0)
        self.assertEqual(("a", "b"), tuple(candidate.signature for candidate in solution.candidates))
        self.assertEqual({0, 1}, set(solution.assignments))

    def test_optimizer_matches_bruteforce_on_tiny_candidate_pool(self):
        candidates = (
            Candidate("a", {}, (0,), 1, frozenset({0}), {0: 0}),
            Candidate("b", {}, (1,), 1, frozenset({1}), {1: 0}),
            Candidate("c", {}, (2,), 1, frozenset({2}), {2: 0}),
            Candidate("ab", {}, (0, 1), 2, frozenset({0, 1}), {0: 1, 1: 1}),
            Candidate("bc", {}, (1, 2), 2, frozenset({1, 2}), {1: 0, 2: 1}),
            Candidate("abc", {}, (0, 1, 2), 3, frozenset({0, 1, 2}), {0: 1, 1: 1, 2: 1}),
        )
        solution = optimize(candidates, 3, 10.0)
        self.assertEqual(_bruteforce_objective(candidates, 3), _solution_objective(solution.candidates, 3))

    def test_optimizer_pruned_and_unpruned_paths_match(self):
        candidates = (
            Candidate("a", {}, (0,), 1, frozenset({0}), {0: 0}),
            Candidate("a-copy", {}, (0,), 1, frozenset({0}), {0: 0}),
            Candidate("b", {}, (1,), 1, frozenset({1}), {1: 0}),
            Candidate("ab", {}, (0, 1), 2, frozenset({0, 1}), {0: 1, 1: 1}),
        )
        pruned = optimize(candidates, 2, 10.0)
        unpruned = optimize(candidates, 2, 10.0, _disable_pruning=True)
        self.assertEqual(_solution_objective(unpruned.candidates, 2), _solution_objective(pruned.candidates, 2))

def _bruteforce_objective(candidates: tuple[Candidate, ...], testcase_count: int):
    best = None
    for size in range(1, len(candidates) + 1):
        for selected in combinations(candidates, size):
            if set().union(*(candidate.coverage for candidate in selected)) != set(range(testcase_count)):
                continue
            objective = _solution_objective(selected, testcase_count)
            if best is None or objective < best:
                best = objective
    return best


def _solution_objective(selected: tuple[Candidate, ...], testcase_count: int):
    assignments = {}
    for index in range(testcase_count):
        covering = [candidate for candidate in selected if index in candidate.coverage]
        assignments[index] = min(covering, key=lambda candidate: (candidate.assignment_excess[index], candidate.equipment_count, candidate.signature))
    return (
        sum(candidate.equipment_count for candidate in selected),
        sum(assignments[index].assignment_excess[index] for index in range(testcase_count)),
        len(selected),
        tuple(sorted(candidate.signature for candidate in selected)),
    )


if __name__ == "__main__":
    unittest.main()
