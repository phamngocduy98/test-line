import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from merge_output_specs import (
    SpecGroup,
    du_count,
    load_groups,
    main,
    merge_small_groups,
    parse_args,
    ru_count,
)
from solve_test_lines import RuBandSupport, TestCase, parse_cell


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
        nr_band_names={},
        lte_by_ru={
            "rf-1": frozenset({"b1", "b2"}),
            "rf-2": frozenset({"b2"}),
        },
        nr_by_ru={"rf-1": frozenset(), "rf-2": frozenset()},
    )


class MergeHelpersTests(unittest.TestCase):
    def test_ru_and_du_counts(self) -> None:
        spec = {
            "ru": ("rf-1", "rf-2"),
            "enb": ("1",),
            "vdu": ("2",),
            "au": (),
            "cu": ("any",),
        }
        self.assertEqual(ru_count(spec), 2)
        self.assertEqual(du_count(spec), 4)

    def test_broader_target_absorbs_equal_size_source(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1", **{"lte band": "b1"}),
            make_case(
                1,
                "B",
                ru="rf-1 + rf-1",
                enb="1",
                **{"lte band": "b1 + b2"},
            ),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",), "lte band": ("b1",)}),
            SpecGroup(
                1,
                [1],
                {
                    "ru": ("rf-1", "rf-1"),
                    "enb": ("1",),
                    "lte band": ("b1", "b2"),
                },
            ),
        ]
        merged, count = merge_small_groups(
            groups,
            ["ru", "enb", "lte band"],
            cases,
            make_support(),
            max_ru=3,
            max_du=3,
            max_tc_per_spec=10,
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].assigned_indices, [0, 1])
        self.assertEqual(merged[0].spec["ru"], ("rf-1", "rf-1"))
        self.assertEqual(merged[0].spec["lte band"], ("b1", "b2"))

    def test_ru_limit_rejects_merge(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-2", enb="1"),
            make_case(2, "C", ru="rf-2", enb="1"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",)}),
            SpecGroup(1, [1, 2], {"ru": ("rf-2",), "enb": ("1",)}),
        ]
        merged, count = merge_small_groups(
            groups,
            ["ru", "enb"],
            cases,
            make_support(),
            max_ru=1,
            max_du=3,
            max_tc_per_spec=10,
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(merged), 2)

    def test_du_limit_rejects_merge(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="3"),
            make_case(1, "B", ru="rf-1", enb="4"),
            make_case(2, "C", ru="rf-1", enb="4"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("3",)}),
            SpecGroup(1, [1, 2], {"ru": ("rf-1",), "enb": ("4",)}),
        ]
        merged, count = merge_small_groups(
            groups,
            ["ru", "enb"],
            cases,
            make_support(),
            max_ru=3,
            max_du=3,
            max_tc_per_spec=10,
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(merged), 2)

    def test_original_testcase_check_can_reject_source_spec_match(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1", **{"lte band": "b2"}),
            make_case(1, "B", ru="rf-1", enb="1", **{"lte band": "b1"}),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",), "lte band": ("b1",)}),
            SpecGroup(1, [1], {"ru": ("rf-1",), "enb": ("1",), "lte band": ("b1",)}),
        ]
        merged, count = merge_small_groups(
            groups,
            ["ru", "enb", "lte band"],
            cases,
            make_support(),
            max_ru=3,
            max_du=3,
            max_tc_per_spec=10,
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(merged), 2)


class MergeIoTests(unittest.TestCase):
    def test_load_groups_requires_complete_unique_assignments(self) -> None:
        cases = [make_case(0, "A", ru="rf-1"), make_case(1, "B", ru="rf-1")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=[
                        "spec_id", "assigned_tc_ids", "assigned_count",
                        "covered_tc_ids", "covered_count", "equipment_count",
                        "total_delta", "solve_status", "ru",
                    ]
                )
                writer.writeheader()
                writer.writerow({
                    "spec_id": "spec_1",
                    "assigned_tc_ids": "A",
                    "assigned_count": "1",
                    "covered_tc_ids": "A",
                    "covered_count": "1",
                    "equipment_count": "1",
                    "total_delta": "0",
                    "solve_status": "OPTIMAL",
                    "ru": "rf-1",
                })
            with self.assertRaisesRegex(SystemExit, "missing: B"):
                load_groups(path, cases)

    def test_parse_args_defaults(self) -> None:
        with patch(
            "sys.argv",
            ["merge_output_specs.py", "--ru-band-support", "support.csv"],
        ):
            args = parse_args()
        self.assertEqual(args.max_ru, 3)
        self.assertEqual(args.max_du, 3)
        self.assertEqual(args.max_tc_per_spec, 338)

    def test_main_writes_compacted_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            testcases = root / "input.csv"
            first_pass = root / "output.csv"
            support = root / "support.csv"
            output = root / "merged.csv"

            testcases.write_text(
                "tc_id,enb,lte band,ru\n"
                "A,1,b1,rf-1\n"
                "B,1,b1,rf-1\n"
                "C,1,b1,rf-1\n",
                encoding="utf-8",
            )
            support.write_text(
                "ru,lte_band,nr_band\nrf-1,b1,\n",
                encoding="utf-8",
            )
            first_pass.write_text(
                "spec_id,assigned_tc_ids,assigned_count,covered_tc_ids,"
                "covered_count,equipment_count,total_delta,solve_status,"
                "enb,lte band,ru\n"
                "spec_1,A,1,A,1,2,0,OPTIMAL,1,b1,rf-1\n"
                "spec_2,B + C,2,B + C,2,2,0,OPTIMAL,1,b1,rf-1\n",
                encoding="utf-8",
            )

            with patch(
                "sys.argv",
                [
                    "merge_output_specs.py",
                    "--input", str(first_pass),
                    "--testcases", str(testcases),
                    "--ru-band-support", str(support),
                    "--output", str(output),
                ],
            ):
                self.assertEqual(main(), 0)

            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["assigned_tc_ids"], "A + B + C")
            self.assertEqual(rows[0]["solve_status"], "SECOND_PASS")


if __name__ == "__main__":
    unittest.main()
