import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solve_test_lines import RuBandSupport, TestCase, parse_cell
from solve_lines import (
    Assignment,
    EquipmentWeights,
    Line,
    _try_merge_lines,
    greedy_initial,
    main,
    parse_args,
    validate_assignment,
)


def make_case(index: int, tc_id: str, **values: str) -> TestCase:
    return TestCase(
        index=index,
        tc_id=tc_id,
        raw=values,
        tokens={column: parse_cell(value) for column, value in values.items()},
    )


def make_support() -> RuBandSupport:
    return RuBandSupport(
        ru_names={"rf-1": "rf-1", "rf-2": "rf-2"},
        lte_band_names={"b1": "b1", "b2": "b2"},
        nr_band_names={"n1": "n1", "n2": "n2"},
        lte_by_ru={
            "rf-1": frozenset({"b1"}),
            "rf-2": frozenset({"b2"}),
        },
        nr_by_ru={
            "rf-1": frozenset({"n1"}),
            "rf-2": frozenset({"n2"}),
        },
    )


class CliTests(unittest.TestCase):
    def test_parse_args_matches_plan_defaults(self) -> None:
        with patch("sys.argv", ["solve_lines.py", "--ru-band-support", "support.csv"]):
            args = parse_args()
        self.assertEqual(args.initial_strategy, "greedy")
        self.assertEqual(args.temperature_start, 2.0)
        self.assertEqual(args.cooling_rate, 0.995)
        self.assertEqual(args.restart_interval, 10000)
        self.assertEqual(args.time_limit, 300.0)


class CompatibilityTests(unittest.TestCase):
    def test_line_keeps_raw_wildcards_for_future_moves_but_costs_concrete_spec(self) -> None:
        support = make_support()
        weights = EquipmentWeights()
        cases = [
            make_case(0, "A", ru="any", **{"lte band": "any"}),
            make_case(1, "B", ru="rf-2", **{"lte band": "b2"}),
        ]
        line = Line()
        assignment = Assignment.empty()
        assignment.add_line(line)
        assignment.assign(0, line)

        concrete = line.get_spec(cases, ["ru", "lte band"], weights, support)
        self.assertNotIn("any", concrete["ru"])
        self.assertNotIn("any", concrete["lte band"])

        # Adding B should be evaluated against the raw A requirements, not the
        # arbitrary concrete realization chosen for A alone.
        self.assertEqual(
            line.cost_if_add(cases[1], ["ru", "lte band"], weights, cases, support),
            1.0,
        )

    def test_merge_move_rejects_candidate_without_ru_band_realization(self) -> None:
        support = make_support()
        weights = EquipmentWeights()
        cases = [
            make_case(0, "A", ru="any", **{"lte band": "b1"}),
            make_case(1, "B", ru="rf-2", **{"lte band": "b2"}),
        ]
        left = Line(case_indices={0})
        right = Line(case_indices={1})

        # The two single lines are feasible, but merging collapses the RU slot
        # to rf-2 while retaining b1 + b2, which rf-2 cannot support.
        self.assertIsNone(
            _try_merge_lines(
                left,
                right,
                cases,
                ["ru", "lte band"],
                weights,
                support,
                max_cases_per_line=10,
            )
        )

    def test_validate_assignment_recomputes_dirty_lines(self) -> None:
        support = make_support()
        weights = EquipmentWeights()
        cases = [make_case(0, "A", ru="any", **{"lte band": "b1"})]
        line = Line(case_indices={0})
        assignment = Assignment(lines=[line], case_to_line={0: line.line_id}, line_by_id={line.line_id: line})

        validate_assignment(
            assignment,
            cases,
            ["ru", "lte band"],
            weights,
            support,
            max_cases_per_line=10,
        )
        self.assertFalse(line._dirty)
        self.assertEqual(line._spec["ru"], ("rf-1",))


class EndToEndTests(unittest.TestCase):
    def test_main_resolves_any_values_and_writes_valid_line_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.csv"
            output_path = root / "lines.csv"
            support_path = root / "support.csv"
            input_path.write_text(
                "tc_id,ru,lte band,enb\n"
                "A,any,b1,1\n"
                "B,rf-2,any,1\n",
                encoding="utf-8",
            )
            support_path.write_text(
                "ru,lte_band,nr_band\n"
                "rf-1,b1,n1\n"
                "rf-2,b2,n2\n",
                encoding="utf-8",
            )
            argv = [
                "solve_lines.py",
                "--input", str(input_path),
                "--output", str(output_path),
                "--ru-band-support", str(support_path),
                "--time-limit", "0",
                "--seed", "1",
            ]
            with patch("sys.argv", argv):
                self.assertEqual(main(), 0)

            with output_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertTrue(all(row["ru"] != "any" for row in rows))
        self.assertTrue(all(row["lte band"] != "any" for row in rows))
        self.assertEqual(sum(int(row["covered_count"]) for row in rows), 2)


if __name__ == "__main__":
    unittest.main()
