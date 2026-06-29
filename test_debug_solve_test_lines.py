import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import debug_solve_test_lines
import solve_test_lines


def read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class DebugSolveTestLinesTests(unittest.TestCase):
    def test_debug_cli_matches_solver_output_and_writes_trace_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.csv"
            support_path = root / "support.csv"
            solver_output = root / "solver_output.csv"
            debug_output = root / "debug_output.csv"
            debug_log = root / "debug.jsonl"

            input_path.write_text(
                "tc_id,lte band,ru,ue\nA,b1,any,1\n",
                encoding="utf-8",
            )
            support_path.write_text(
                "ru,lte_band,nr_band\nrf-1,b1,n1\n",
                encoding="utf-8",
            )

            with patch(
                "sys.argv",
                [
                    "solve_test_lines.py",
                    "--input",
                    str(input_path),
                    "--output",
                    str(solver_output),
                    "--ru-band-support",
                    str(support_path),
                    "--timeout",
                    "10",
                ],
            ), redirect_stdout(StringIO()):
                self.assertEqual(solve_test_lines.main(), 0)

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    debug_solve_test_lines.main(
                        [
                            "--input",
                            str(input_path),
                            "--output",
                            str(debug_output),
                            "--ru-band-support",
                            str(support_path),
                            "--debug-log",
                            str(debug_log),
                            "--timeout",
                            "10",
                        ]
                    ),
                    0,
                )

            self.assertEqual(
                debug_output.read_text(encoding="utf-8"),
                solver_output.read_text(encoding="utf-8"),
            )

            events = read_events(debug_log)
            event_names = [event["event"] for event in events]
            for event in events:
                for key in ("seq", "elapsed_ms", "event", "phase", "level", "data"):
                    self.assertIn(key, event)

            for required_event in (
                "run.started",
                "input.loaded",
                "candidate_generation.started",
                "decision.candidate_accepted",
                "decision.greedy_hint_candidate_selected",
                "decision.ortools_stage_result_accepted",
                "decision.output_sorted",
                "decision.first_output_spec_identified",
                "first_spec.ancestry",
                "first_spec.coverage_check",
                "output.first_spec",
                "run.completed",
            ):
                self.assertIn(required_event, event_names)

            decisions = [
                event for event in events if event["event"].startswith("decision.")
            ]
            self.assertTrue(decisions)
            self.assertTrue(all("state" in event for event in decisions))

            first_spec = next(
                event for event in events if event["event"] == "output.first_spec"
            )
            self.assertEqual(first_spec["data"]["spec_id"], "spec_1")
            self.assertEqual(first_spec["data"]["row"]["ru"], "rf-1")
            self.assertIn("first_spec_candidate_index", first_spec["state"])

            ancestry = next(
                event for event in events if event["event"] == "first_spec.ancestry"
            )
            self.assertEqual(
                ancestry["data"]["generations"][0]["source"]["seed_tc_ids"],
                ["A"],
            )

    def test_debug_cli_logs_exception_with_traceback_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "bad_input.csv"
            support_path = root / "support.csv"
            debug_output = root / "debug_output.csv"
            debug_log = root / "debug.jsonl"

            input_path.write_text("ru\nrf-1\n", encoding="utf-8")
            support_path.write_text(
                "ru,lte_band,nr_band\nrf-1,b1,n1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SystemExit, "must contain a tc_id"):
                debug_solve_test_lines.main(
                    [
                        "--input",
                        str(input_path),
                        "--output",
                        str(debug_output),
                        "--ru-band-support",
                        str(support_path),
                        "--debug-log",
                        str(debug_log),
                    ]
                )

            events = read_events(debug_log)
            exception_event = next(
                event for event in events if event["event"] == "run.exception"
            )
            self.assertEqual(exception_event["level"], "error")
            self.assertEqual(exception_event["data"]["exception_type"], "SystemExit")
            self.assertIn("must contain a tc_id", exception_event["data"]["message"])
            self.assertIn("traceback", exception_event["data"])
            self.assertEqual(exception_event["state"]["phase"], "input")


if __name__ == "__main__":
    unittest.main()
