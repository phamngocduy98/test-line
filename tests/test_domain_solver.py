from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_line_solver.candidates import generate_candidates
from test_line_solver.coverage import active_requirement_columns, coverage_excess
from test_line_solver.evaluation import SolutionEvaluator
from test_line_solver.merge import merge_specs
from test_line_solver.models import Candidate, Solution, SolveOptions, Token
from test_line_solver.output import write_solution_csv
from test_line_solver.optimizer import optimize
from test_line_solver.parsing import read_ru_band_csv, read_testcase_csv
from test_line_solver.solver import solve_to_csv
from test_line_solver.solver import _refine_low_use_specs, _solution_with_low_use_status
from test_line_solver.support import build_support_table
from test_line_solver.validation import validate_testcases


class DomainSolverTests(unittest.TestCase):
    def write(self, directory: Path, name: str, text: str) -> Path:
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def parsed(self, directory: Path, input_text: str, support_text: str):
        input_csv = read_testcase_csv(self.write(directory, "input.csv", input_text), require_ru=True)
        support = build_support_table(read_ru_band_csv(self.write(directory, "ru-band.csv", support_text)))
        validate_testcases(input_csv, support, final_solver=True)
        return input_csv, support

    def test_lowest_excess_distinct_slot_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(Path(tmp), "tc_id,ru,lte band,cc location\nT1,RU1,b1,B\n", "ru,lte_band,nr_band\nRU1,b1,\n")
            options = SolveOptions()
            columns = active_requirement_columns(parsed.columns, options)
            spec = {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)),), "cc location": (Token(("A", "B")), Token(("B",)))}
            result = coverage_excess(parsed.rows[0].tokens, spec, columns, support, options)
            self.assertIsNotNone(result)
            self.assertEqual(1, result.excess)

    def test_ru_slot_count_and_one_ru_multiple_bands(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,b1 + b2\nT2,any + any,b1 + b2\n",
                "ru,lte_band,nr_band\nRU1,b1 + b2,\n",
            )
            options = SolveOptions()
            columns = active_requirement_columns(parsed.columns, options)
            one_ru_spec = {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)), Token(("b2",)))}
            self.assertIsNotNone(coverage_excess(parsed.rows[0].tokens, one_ru_spec, columns, support, options))
            self.assertIsNone(coverage_excess(parsed.rows[1].tokens, one_ru_spec, columns, support, options))

    def test_no_ru_spec_is_valid_only_without_band_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band,nr band,cc location\nT1,,,,A\nT2,any,b1,,A\n",
                "ru,lte_band,nr_band\nRU1,b1,\n",
            )
            options = SolveOptions()
            columns = active_requirement_columns(parsed.columns, options)
            no_ru_spec = {"ru": (), "lte band": (), "nr band": (), "cc location": (Token(("A",)),)}
            band_spec_without_ru = {"ru": (), "lte band": (Token(("b1",)),), "nr band": (), "cc location": (Token(("A",)),)}
            self.assertIsNotNone(coverage_excess(parsed.rows[0].tokens, no_ru_spec, columns, support, options))
            self.assertIsNone(coverage_excess(parsed.rows[1].tokens, no_ru_spec, columns, support, options))
            self.assertIsNone(coverage_excess(parsed.rows[1].tokens, band_spec_without_ru, columns, support, options))

    def test_band_relationship_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,intra\nT2,RU1,inter\nT3,RU1,b1\n",
                "ru,lte_band,nr_band\nRU1,b1 + b2,\n",
            )
            options = SolveOptions()
            columns = active_requirement_columns(parsed.columns, options)
            intra_spec = {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)), Token(("b1",)))}
            inter_spec = {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)), Token(("b2",)))}
            one_band_spec = {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)),)}
            self.assertIsNotNone(coverage_excess(parsed.rows[0].tokens, intra_spec, columns, support, options))
            self.assertIsNotNone(coverage_excess(parsed.rows[1].tokens, inter_spec, columns, support, options))
            self.assertIsNone(coverage_excess(parsed.rows[0].tokens, one_band_spec, columns, support, options))
            self.assertIsNone(coverage_excess(parsed.rows[1].tokens, one_band_spec, columns, support, options))
            self.assertIsNotNone(coverage_excess(parsed.rows[2].tokens, one_band_spec, columns, support, options))

    def test_merge_does_not_invent_alternatives_for_disjoint_slots(self):
        columns = ("x",)
        merged = merge_specs({"x": (Token(("A", "B")),)}, {"x": (Token(("C",)),)}, columns)
        self.assertEqual((Token(("A", "B")), Token(("C",))), merged["x"])
        merged = merge_specs({"x": (Token(("A", "B")),)}, {"x": (Token(("B", "C")),)}, columns)
        self.assertEqual((Token(("A", "B", "C")),), merged["x"])

    def test_optimizer_prefers_focused_specs_over_same_equipment_broad_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,b1\nT2,RU2,b2\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            options = SolveOptions(max_merge_width=5)
            candidates = generate_candidates(parsed, support, options)
            solution = optimize(candidates, len(parsed.rows), 10.0)
            self.assertEqual(2, len(solution.candidates))
            self.assertEqual(0, sum(solution.assignments[index].assignment_excess[index] for index in range(2)))

    def test_optimizer_reports_feasible_timeout_after_incumbent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,b1\nT2,RU2,b2\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            candidates = generate_candidates(parsed, support, SolveOptions(max_merge_width=5))
            solution = optimize(candidates, len(parsed.rows), 0.0)
            self.assertEqual("FEASIBLE_TIMEOUT", solution.status)
            self.assertEqual({0, 1}, set(solution.assignments))

    def test_candidate_generation_retains_exact_candidates_before_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,b1\nT2,RU2,b2\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            candidates = generate_candidates(parsed, support, SolveOptions(max_candidates=1))
            self.assertGreaterEqual(len(candidates), 2)
            self.assertTrue(any(candidate.coverage == frozenset({0}) for candidate in candidates))
            self.assertTrue(any(candidate.coverage == frozenset({1}) for candidate in candidates))

    def test_candidate_generation_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,b1\nT2,RU2,b2\nT3,RU3,b3\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\nRU3,b3,\n",
            )
            options = SolveOptions(max_merge_width=3)
            first = [candidate.signature for candidate in generate_candidates(parsed, support, options)]
            second = [candidate.signature for candidate in generate_candidates(parsed, support, options)]
            self.assertEqual(first, second)

    def test_end_to_end_writes_expanded_domains_and_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,enb,lte band,ru,ue\nT1,1,b1,any,1\nT2,1,b2,any,1\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            output = directory / "output.csv"
            solve_to_csv(parsed, support, output, SolveOptions(auto_assign=True, max_merge_width=5, min_assigned_cases_per_spec=0))
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertEqual(["spec_1", "spec_2"], [row["spec_id"] for row in rows])
            self.assertIn("assigned_tc_ids", rows[0])
            self.assertTrue(all(row["solve_status"] == "OPTIMAL" for row in rows))
            self.assertEqual({"RU1", "RU2"}, {row["ru"] for row in rows})
            self.assertEqual({"T1", "T2"}, {row["covered_tc_ids"] for row in rows})

    def test_output_filters_ru_wildcard_to_band_compatible_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,lte band,ru\nT1,b1,any\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            output = directory / "output.csv"
            solve_to_csv(parsed, support, output, SolveOptions())
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual("RU1", row["ru"])
            self.assertEqual("T1", row["covered_tc_ids"])

    def test_output_compacts_full_ru_and_band_domains_to_any(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,ru,lte band,nr band\nT1,any,any,any\n",
                "ru,lte_band,nr_band\nRU1,b1,n1\nRU2,b2,n2\n",
            )
            output = directory / "output.csv"
            solve_to_csv(parsed, support, output, SolveOptions())
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual("any", row["ru"])
            self.assertEqual("any", row["lte band"])
            self.assertEqual("any", row["nr band"])

    def test_output_compacts_each_full_domain_slot_to_any(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,ru,lte band\nT1,any + any,b1\nT2,RU1,b2\n",
                "ru,lte_band,nr_band\nRU1,b1 + b2,\nRU2,b1 + b2,\n",
            )
            output = directory / "output.csv"
            solve_to_csv(parsed, support, output, SolveOptions(max_merge_width=5))
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("any + any", {row["ru"] for row in rows})

    def test_solver_scores_candidates_against_expanded_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,ru,lte band\nT1,RU1,any\nT2,any,b2\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            candidates = generate_candidates(parsed, support, SolveOptions(max_merge_width=5))
            exact = next(candidate for candidate in candidates if candidate.signature == "ru=RU1|lte band=any")
            self.assertEqual(frozenset({0}), exact.coverage)

            output = directory / "output.csv"
            solve_to_csv(parsed, support, output, SolveOptions(max_merge_width=5))
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({"T1", "T2"}, set(" + ".join(row["covered_tc_ids"] for row in rows).split(" + ")))

    def test_ignored_optional_columns_remain_blank_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,tech lte,lte band,ru\nT1,lte,b1,RU1\n",
                "ru,lte_band,nr_band\nRU1,b1,\n",
            )
            output = directory / "output.csv"
            solve_to_csv(parsed, support, output, SolveOptions(ignore_optional_columns=True))
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertIn("tech lte", row)
            self.assertEqual("", row["tech lte"])

    def test_blank_requirements_render_blank_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,ru,lte band,nr band,cc location\nT1,,,,A\n",
                "ru,lte_band,nr_band\nRU1,b1,\n",
            )
            output = directory / "output.csv"
            solve_to_csv(parsed, support, output, SolveOptions())
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual("", row["ru"])
            self.assertEqual("", row["lte band"])
            self.assertEqual("", row["nr band"])
            self.assertEqual("A", row["cc location"])

    def test_output_rejects_selected_specs_that_lose_expanded_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,ru,lte band\nT1,any,b2\n",
                "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n",
            )
            candidate = Candidate(
                signature="bad",
                spec={"ru": (Token(("RU1",)),), "lte band": (Token(("b2",)),)},
                source_indexes=(0,),
                equipment_count=1,
                coverage=frozenset({0}),
                assignment_excess={0: 0},
            )
            with self.assertRaisesRegex(ValueError, "expanded solution does not cover"):
                write_solution_csv(directory / "output.csv", parsed, support, Solution((candidate,), {0: candidate}, "OPTIMAL"), SolveOptions())

    def test_low_use_refinement_removes_unassigned_redundant_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,b1\nT2,RU1,b1\n",
                "ru,lte_band,nr_band\nRU1,b1,\n",
            )
            spec = {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)),)}
            first = Candidate("a", spec, (0,), 1, frozenset({0, 1}), {0: 0, 1: 0})
            duplicate = Candidate("a-copy", spec, (1,), 1, frozenset({0, 1}), {0: 0, 1: 0})
            options = SolveOptions(min_assigned_cases_per_spec=2)
            refinement = _refine_low_use_specs(
                (first, duplicate),
                Solution((first, duplicate), {0: first, 1: first}, "OPTIMAL"),
                SolutionEvaluator(parsed, support, options),
                options,
            )
            refined = refinement.solution
            self.assertTrue(refinement.completed)
            self.assertTrue(refinement.changed)
            self.assertEqual(1, len(refined.candidates))
            self.assertEqual(0, refinement.evaluation.low_use_spec_count)
            self.assertEqual(0, refinement.evaluation.total_assignment_excess)
            self.assertEqual("FEASIBLE_LOW_USE_REFINED", _solution_with_low_use_status(refinement, options).status)

    def test_low_use_refinement_stops_when_deadline_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed, support = self.parsed(
                Path(tmp),
                "tc_id,ru,lte band\nT1,RU1,b1\n",
                "ru,lte_band,nr_band\nRU1,b1,\n",
            )
            spec = {"ru": (Token(("RU1",)),), "lte band": (Token(("b1",)),)}
            candidate = Candidate("a", spec, (0,), 1, frozenset({0}), {0: 0})
            options = SolveOptions(min_assigned_cases_per_spec=10)
            refinement = _refine_low_use_specs(
                (candidate,),
                Solution((candidate,), {0: candidate}, "OPTIMAL"),
                SolutionEvaluator(parsed, support, options),
                options,
                deadline=0.0,
            )
            self.assertFalse(refinement.completed)
            self.assertFalse(refinement.changed)
            self.assertEqual("FEASIBLE_TIMEOUT", _solution_with_low_use_status(refinement, options).status)

    def test_dedicated_low_use_timeout_refines_after_primary_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,ru,lte band\nT1,RU1,b1\nT2,RU1,b1\n",
                "ru,lte_band,nr_band\nRU1,b1,\n",
            )

            def timed_out_solution(candidates, testcase_count, timeout_seconds):
                primary = candidates[0]
                duplicate = Candidate(
                    "manual-broader",
                    {"ru": (Token(("RU1",)),), "lte band": (Token(("any",)),)},
                    (),
                    primary.equipment_count,
                    primary.coverage,
                    primary.assignment_excess,
                    primary.group_coverage_mask,
                    primary.group_assignment_excess,
                    primary.group_weights,
                )
                return Solution((primary, duplicate), {index: primary for index in range(testcase_count)}, "FEASIBLE_TIMEOUT")

            output = directory / "output.csv"
            options = SolveOptions(
                solver="stdlib",
                timeout_seconds=0.0,
                low_use_refinement_timeout_seconds=1.0,
                min_assigned_cases_per_spec=2,
            )
            with patch("test_line_solver.optimizer.optimize", side_effect=timed_out_solution):
                solve_to_csv(parsed, support, output, options)

            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual("FEASIBLE_TIMEOUT", rows[0]["solve_status"])

    def test_refine_output_mode_removes_low_use_imported_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            parsed, support = self.parsed(
                directory,
                "tc_id,ru,lte band\nT1,RU1,b1\nT2,RU1,b1\n",
                "ru,lte_band,nr_band\nRU1,b1,\n",
            )
            previous = directory / "previous.csv"
            previous.write_text(
                "spec_id,covered_tc_ids,covered_count,equipment_count,solve_status,ru,lte band\n"
                "spec_1,wrong,999,999,FEASIBLE_TIMEOUT,RU1,b1\n"
                "spec_2,wrong,999,999,FEASIBLE_TIMEOUT,RU1,any\n",
                encoding="utf-8",
            )

            output = directory / "refined.csv"
            options = SolveOptions(low_use_refinement_timeout_seconds=1.0, min_assigned_cases_per_spec=2)
            from test_line_solver.solver import refine_output_to_csv

            refine_output_to_csv(parsed, support, previous, output, options)

            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual("FEASIBLE_LOW_USE_REFINED", rows[0]["solve_status"])
            self.assertEqual("T1 + T2", rows[0]["covered_tc_ids"])


if __name__ == "__main__":
    unittest.main()
