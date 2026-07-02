from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from test_line_solver.cli import run


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


if __name__ == "__main__":
    unittest.main()
