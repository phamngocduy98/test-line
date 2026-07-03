from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_line_solver.cli import run
from test_line_solver.models import Candidate, Solution, Token


class CliTests(unittest.TestCase):
    def write(self, directory: Path, name: str, text: str) -> Path:
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_parse_only_prints_json_and_does_not_write_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band\nT1,\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nany,any,\n")
            output_path = directory / "output.csv"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--output", str(output_path), "--parse-only"])
            self.assertEqual(0, code)
            self.assertIn('"input"', stdout.getvalue())
            self.assertIn("Reading testcase CSV", stderr.getvalue())
            self.assertIn("Completed in", stderr.getvalue())
            self.assertFalse(output_path.exists())

    def test_normal_solve_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            output_path = directory / "output.csv"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band-support", str(support_path), "--output", str(output_path)])
            self.assertEqual(0, code)
            with output_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual("spec_1", rows[0]["spec_id"])
            self.assertEqual("any", rows[0]["ru"])
            self.assertIn("Solving selected test-line specs", stderr.getvalue())
            self.assertIn("Completed in", stderr.getvalue())

    def test_explicit_stdlib_solver_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            output_path = directory / "output.csv"
            with redirect_stderr(io.StringIO()):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--output", str(output_path), "--solver", "stdlib"])
            self.assertEqual(0, code)
            self.assertTrue(output_path.exists())

    def test_ortools_solver_threads_option_writes_output_when_available(self):
        try:
            import ortools  # noqa: F401
        except ImportError:
            self.skipTest("OR-Tools is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            output_path = directory / "output.csv"
            with redirect_stderr(io.StringIO()):
                code = run(
                    [
                        "--input",
                        str(input_path),
                        "--ru-band",
                        str(support_path),
                        "--output",
                        str(output_path),
                        "--solver",
                        "ortools",
                        "--solver-threads",
                        "1",
                    ]
                )
            self.assertEqual(0, code)
            with output_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertIn(row["solve_status"], {"OPTIMAL", "FEASIBLE_TIMEOUT"})

    def test_solver_threads_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            for value in ("0", "-2"):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = run(["--input", str(input_path), "--ru-band", str(support_path), "--solver-threads", value])
                self.assertEqual(2, code)
                self.assertIn("--solver-threads must be a positive integer", stderr.getvalue())

    def test_min_assigned_cases_per_spec_must_not_be_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--min-assigned-cases-per-spec", "-1"])
            self.assertEqual(2, code)
            self.assertIn("--min-assigned-cases-per-spec must be zero or a positive integer", stderr.getvalue())

    def test_low_use_refinement_timeout_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--low-use-refinement-timeout", "0"])
            self.assertEqual(2, code)
            self.assertIn("--low-use-refinement-timeout must be positive", stderr.getvalue())

    def test_low_use_refinement_bounds_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            cases = (
                ("--max-low-use-merge-combinations", "--max-low-use-merge-combinations must be a positive integer"),
            )
            for option, message in cases:
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = run(["--input", str(input_path), "--ru-band", str(support_path), option, "0"])
                self.assertEqual(2, code)
                self.assertIn(message, stderr.getvalue())

    def test_low_use_affordable_bounds_must_not_be_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            cases = (
                ("--low-use-affordable-equipment-delta", "--low-use-affordable-equipment-delta must be zero or a positive integer"),
            )
            for option, message in cases:
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = run(["--input", str(input_path), "--ru-band", str(support_path), option, "-1"])
                self.assertEqual(2, code)
                self.assertIn(message, stderr.getvalue())

    def test_refine_output_rejects_parse_only_and_disabled_low_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            previous_path = self.write(directory, "previous.csv", "spec_id,ru,lte band\nspec_1,RU1,b1\n")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--refine-output", str(previous_path), "--parse-only"])
            self.assertEqual(2, code)
            self.assertIn("--refine-output cannot be used with --parse-only", stderr.getvalue())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(
                    [
                        "--input",
                        str(input_path),
                        "--ru-band",
                        str(support_path),
                        "--refine-output",
                        str(previous_path),
                        "--min-assigned-cases-per-spec",
                        "0",
                    ]
                )
            self.assertEqual(2, code)
            self.assertIn("--refine-output requires --min-assigned-cases-per-spec greater than zero", stderr.getvalue())

    def test_refine_output_rejects_unexpected_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,RU1\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            previous_path = self.write(directory, "previous.csv", "spec_id,ru,lte band,extra\nspec_1,RU1,b1,nope\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--refine-output", str(previous_path)])
            self.assertEqual(2, code)
            self.assertIn("unexpected output column", stderr.getvalue())

    def test_low_use_summary_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            output_path = directory / "output.csv"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(
                    [
                        "--input",
                        str(input_path),
                        "--ru-band",
                        str(support_path),
                        "--output",
                        str(output_path),
                        "--min-assigned-cases-per-spec",
                        "0",
                    ]
                )
            self.assertEqual(0, code)
            with output_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual("OPTIMAL", row["solve_status"])
            self.assertNotIn("Low-use specs remain", stderr.getvalue())

    def test_low_use_summary_reports_small_assigned_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            output_path = directory / "output.csv"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--output", str(output_path)])
            self.assertEqual(0, code)
            with output_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual("FEASIBLE_LOW_USE_CHECKED", row["solve_status"])
            self.assertEqual("OPTIMAL", row["main_solve_status"])
            self.assertEqual("COMPLETED_UNCHANGED", row["low_use_refinement_status"])
            self.assertIn("Low-use specs remain: 1 selected specs have fewer than 10 assigned testcases", stderr.getvalue())

    def test_auto_solver_uses_ortools_when_available(self):
        try:
            import ortools  # noqa: F401
        except ImportError:
            self.skipTest("OR-Tools is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            output_path = directory / "output.csv"
            with patch("test_line_solver.ortools_optimizer.optimize", side_effect=_first_candidate_solution) as ortools_optimize:
                with patch("test_line_solver.optimizer.optimize", side_effect=AssertionError("stdlib should not be used")):
                    with redirect_stderr(io.StringIO()):
                        code = run(["--input", str(input_path), "--ru-band", str(support_path), "--output", str(output_path)])
            self.assertEqual(0, code)
            self.assertTrue(ortools_optimize.called)

    def test_ortools_feasible_timeout_status_survives_low_use_refinement(self):
        try:
            import ortools  # noqa: F401
        except ImportError:
            self.skipTest("OR-Tools is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,RU1\nT2,b1,RU1\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            output_path = directory / "output.csv"

            with patch("test_line_solver.ortools_optimizer.optimize", side_effect=_duplicated_feasible_timeout_solution):
                with redirect_stderr(io.StringIO()):
                    code = run(
                        [
                            "--input",
                            str(input_path),
                            "--ru-band",
                            str(support_path),
                            "--output",
                            str(output_path),
                            "--solver",
                            "ortools",
                            "--low-use-refinement-timeout",
                            "1",
                            "--min-assigned-cases-per-spec",
                            "2",
                        ]
                    )
            self.assertEqual(0, code)
            with output_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual("FEASIBLE_TIMEOUT", rows[0]["solve_status"])
            self.assertEqual("FEASIBLE_TIMEOUT", rows[0]["main_solve_status"])
            self.assertEqual("COMPLETED_REFINED", rows[0]["low_use_refinement_status"])

    def test_auto_solver_falls_back_only_when_ortools_is_unavailable(self):
        try:
            from test_line_solver.ortools_optimizer import OrtoolsUnavailableError
        except ImportError:
            self.skipTest("OR-Tools optimizer module is not importable")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            output_path = directory / "output.csv"
            with patch("test_line_solver.ortools_optimizer.optimize", side_effect=OrtoolsUnavailableError("missing")):
                with patch("test_line_solver.optimizer.optimize", side_effect=_first_candidate_solution) as stdlib_optimize:
                    with redirect_stderr(io.StringIO()):
                        code = run(["--input", str(input_path), "--ru-band", str(support_path), "--output", str(output_path)])
            self.assertEqual(0, code)
            self.assertTrue(stdlib_optimize.called)

    def test_auto_solver_does_not_fallback_on_ortools_runtime_error(self):
        try:
            import ortools  # noqa: F401
        except ImportError:
            self.skipTest("OR-Tools is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            stderr = io.StringIO()
            with patch("test_line_solver.ortools_optimizer.optimize", side_effect=RuntimeError("model exploded")):
                with patch("test_line_solver.optimizer.optimize", side_effect=AssertionError("stdlib should not be used")):
                    with redirect_stderr(stderr):
                        code = run(["--input", str(input_path), "--ru-band", str(support_path)])
            self.assertEqual(2, code)
            self.assertIn("model exploded", stderr.getvalue())

    def test_explicit_ortools_reports_unavailable_error(self):
        try:
            from test_line_solver.ortools_optimizer import OrtoolsUnavailableError
        except ImportError:
            self.skipTest("OR-Tools optimizer module is not importable")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            stderr = io.StringIO()
            with patch("test_line_solver.ortools_optimizer.optimize", side_effect=OrtoolsUnavailableError("missing")):
                with redirect_stderr(stderr):
                    code = run(["--input", str(input_path), "--ru-band", str(support_path), "--solver", "ortools"])
            self.assertEqual(2, code)
            self.assertIn("missing", stderr.getvalue())

    def test_limit_rows_limits_parse_only_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band\nT1,b1\nT2,b2\nT3,b3\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1 + b2 + b3,\n")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--parse-only", "--limit-rows", "2"])
            self.assertEqual(0, code)
            self.assertIn('"tc_id": "T1"', stdout.getvalue())
            self.assertIn('"tc_id": "T2"', stdout.getvalue())
            self.assertNotIn('"tc_id": "T3"', stdout.getvalue())
            self.assertIn("Limited testcase rows: 2 of 3", stderr.getvalue())

    def test_limit_rows_limits_solve_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\nT2,b2,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\nRU2,b2,\n")
            output_path = directory / "output.csv"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(
                    [
                        "--input",
                        str(input_path),
                        "--ru-band",
                        str(support_path),
                        "--output",
                        str(output_path),
                        "--limit-rows",
                        "1",
                    ]
                )
            self.assertEqual(0, code)
            with output_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(["T1"], [row["covered_tc_ids"] for row in rows])
            self.assertIn("Limited testcase rows: 1 of 2", stderr.getvalue())

    def test_limit_rows_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band,ru\nT1,b1,any\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path), "--limit-rows", "0"])
            self.assertEqual(2, code)
            self.assertIn("--limit-rows must be a positive integer", stderr.getvalue())

    def test_normal_solve_reports_input_error_with_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band\nT1,b1\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRU1,b1,\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run(["--input", str(input_path), "--ru-band", str(support_path)])
            self.assertEqual(2, code)
            self.assertIn("missing required column", stderr.getvalue())


def _first_candidate_solution(candidates, testcase_count, *args, **kwargs):
    candidate = candidates[0]
    return Solution((candidate,), {index: candidate for index in range(testcase_count)}, "OPTIMAL")


def _duplicated_feasible_timeout_solution(candidates, testcase_count, *args, **kwargs):
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


if __name__ == "__main__":
    unittest.main()
