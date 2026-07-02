from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_line_solver.cli import run
from test_line_solver.models import Solution


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
            self.assertIn(row["solve_status"], {"OPTIMAL", "FEASIBLE"})

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


if __name__ == "__main__":
    unittest.main()
