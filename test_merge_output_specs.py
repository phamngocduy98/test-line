import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from merge_output_specs import (
    SpecGroup,
    du_count,
    load_groups,
    main,
    merge_attempt,
    merge_small_groups,
    parse_args,
    ru_count,
    ue_count,
    validate_merged_groups,
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
        nr_band_names={"n1": "n1", "n2": "n2"},
        lte_by_ru={
            "rf-1": frozenset({"b1", "b2"}),
            "rf-2": frozenset({"b2"}),
        },
        nr_by_ru={
            "rf-1": frozenset({"n1", "n2"}),
            "rf-2": frozenset({"n2"}),
        },
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
        self.assertEqual(ue_count({"ue": ("3",)}), 3)
        self.assertEqual(ue_count({}), 0)

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
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].covered_indices, [0, 1])
        self.assertEqual(merged[0].spec["ru"], ("rf-1", "rf-1"))
        self.assertEqual(merged[0].spec["lte band"], ("b1", "b2"))

    def test_later_broader_spec_absorbs_earlier_smaller_spec(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-1 + rf-1", enb="1"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",)}),
            SpecGroup(
                1,
                [1],
                {"ru": ("rf-1", "rf-1"), "enb": ("1",)},
            ),
        ]

        merged, count = merge_small_groups(
            groups,
            ["ru", "enb"],
            cases,
            make_support(),
            max_ru=3,
            max_du=3,
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].original_order, 0)
        self.assertEqual(merged[0].covered_indices, [0, 1])
        self.assertEqual(merged[0].spec["ru"], ("rf-1", "rf-1"))

    def test_equivalent_specs_keep_earlier_input_spec(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-1", enb="1"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",)}),
            SpecGroup(1, [1], {"ru": ("rf-1",), "enb": ("1",)}),
        ]

        merged, count = merge_small_groups(
            groups,
            ["ru", "enb"],
            cases,
            make_support(),
            max_ru=3,
            max_du=3,
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].original_order, 0)
        self.assertEqual(merged[0].covered_indices, [0, 1])

    def test_bubble_merge_restarts_after_each_merge(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-1 + rf-1", enb="1"),
            make_case(2, "C", ru="rf-1 + rf-1 + rf-1", enb="1"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",)}),
            SpecGroup(
                1,
                [1],
                {"ru": ("rf-1", "rf-1"), "enb": ("1",)},
            ),
            SpecGroup(
                2,
                [2],
                {"ru": ("rf-1", "rf-1", "rf-1"), "enb": ("1",)},
            ),
        ]

        merged, count = merge_small_groups(
            groups,
            ["ru", "enb"],
            cases,
            make_support(),
            max_ru=3,
            max_du=3,
        )

        self.assertEqual(count, 2)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].original_order, 0)
        self.assertEqual(merged[0].covered_indices, [0, 1, 2])
        self.assertEqual(merged[0].spec["ru"], ("rf-1", "rf-1", "rf-1"))

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
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(merged), 2)

    def test_merge_ignores_delta_limit(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-1 + rf-1 + rf-1", enb="1"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",)}),
            SpecGroup(
                1,
                [1],
                {"ru": ("rf-1", "rf-1", "rf-1"), "enb": ("1",)},
            ),
        ]
        merged, count = merge_small_groups(
            groups,
            ["ru", "enb"],
            cases,
            make_support(),
            max_ru=3,
            max_du=3,
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)

    def test_merge_attempt_extends_supported_bands(self) -> None:
        left = SpecGroup(
            0,
            [0],
            {"ru": ("rf-1",), "enb": ("1",), "lte band": ("b1",)},
        )
        right = SpecGroup(
            1,
            [1],
            {"ru": ("rf-1",), "enb": ("1",), "lte band": ("b2",)},
        )

        candidate, reason = merge_attempt(
            left,
            right,
            ["ru", "enb", "lte band"],
            make_support(),
            max_ru=3,
            max_du=3,
            max_ue=10,
        )

        self.assertEqual(reason, "compatible")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["lte band"], ("b1", "b2"))

    def test_merge_attempt_extends_supported_nr_bands(self) -> None:
        left = SpecGroup(
            0,
            [0],
            {"ru": ("rf-1",), "nr band": ("n1",)},
        )
        right = SpecGroup(
            1,
            [1],
            {"ru": ("rf-1",), "nr band": ("n2",)},
        )

        candidate, reason = merge_attempt(
            left,
            right,
            ["ru", "nr band"],
            make_support(),
            max_ru=3,
            max_du=3,
            max_ue=10,
        )

        self.assertEqual(reason, "compatible")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["nr band"], ("n1", "n2"))

    def test_merge_rejects_unsupported_combined_band(self) -> None:
        support = make_support()
        support.lte_by_ru["rf-1"] = frozenset({"b1"})
        left = SpecGroup(
            0,
            [0],
            {"ru": ("rf-1",), "lte band": ("b1",)},
        )
        right = SpecGroup(
            1,
            [1],
            {"ru": ("rf-1",), "lte band": ("b2",)},
        )

        candidate, reason = merge_attempt(
            left,
            right,
            ["ru", "lte band"],
            support,
            max_ru=3,
            max_du=3,
            max_ue=10,
        )

        self.assertIsNone(candidate)
        self.assertEqual(reason, "ru_band_compatibility")

    def test_merge_uses_max_ue_with_limit(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", ue="2"),
            make_case(1, "B", ru="rf-1", ue="4"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "ue": ("2",)}),
            SpecGroup(1, [1], {"ru": ("rf-1",), "ue": ("4",)}),
        ]

        merged, count = merge_small_groups(
            groups,
            ["ru", "ue"],
            cases,
            make_support(),
            max_ru=3,
            max_du=3,
            max_ue=4,
        )

        self.assertEqual(count, 1)
        self.assertEqual(merged[0].spec["ue"], ("4",))

    def test_ue_limit_rejects_combined_candidate(self) -> None:
        left = SpecGroup(0, [0], {"ru": ("rf-1",), "ue": ("2",)})
        right = SpecGroup(1, [1], {"ru": ("rf-1",), "ue": ("4",)})

        candidate, reason = merge_attempt(
            left,
            right,
            ["ru", "ue"],
            make_support(),
            max_ru=3,
            max_du=3,
            max_ue=3,
        )

        self.assertIsNone(candidate)
        self.assertEqual(reason, "max_ue actual=4 limit=3")

    def test_inter_relation_requires_two_supported_bands(self) -> None:
        support = make_support()
        support.lte_by_ru["rf-2"] = frozenset({"b2"})
        left = SpecGroup(
            0,
            [0],
            {"ru": ("rf-2",), "lte band": ("inter",)},
        )
        right = SpecGroup(
            1,
            [1],
            {"ru": ("rf-2",), "lte band": ("b2",)},
        )

        candidate, reason = merge_attempt(
            left,
            right,
            ["ru", "lte band"],
            support,
            max_ru=3,
            max_du=3,
            max_ue=10,
        )

        self.assertIsNone(candidate)
        self.assertEqual(
            reason,
            "relation column='lte band' relation='inter'",
        )

    def test_intra_relation_requires_a_supported_band(self) -> None:
        support = make_support()
        support.nr_by_ru["rf-2"] = frozenset()
        left = SpecGroup(
            0,
            [0],
            {"ru": ("rf-2",), "nr band": ("intra",)},
        )
        right = SpecGroup(
            1,
            [1],
            {"ru": ("rf-2",), "nr band": ()},
        )

        candidate, reason = merge_attempt(
            left,
            right,
            ["ru", "nr band"],
            support,
            max_ru=3,
            max_du=3,
            max_ue=10,
        )

        self.assertIsNone(candidate)
        self.assertEqual(
            reason,
            "relation column='nr band' relation='intra'",
        )

    def test_relation_token_is_preserved_in_candidate(self) -> None:
        left = SpecGroup(
            0,
            [0],
            {"ru": ("rf-1",), "lte band": ("inter",)},
        )
        right = SpecGroup(
            1,
            [1],
            {"ru": ("rf-1",), "lte band": ("b1",)},
        )

        candidate, reason = merge_attempt(
            left,
            right,
            ["ru", "lte band"],
            make_support(),
            max_ru=3,
            max_du=3,
            max_ue=10,
        )

        self.assertEqual(reason, "compatible")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["lte band"], ("inter", "b1"))

    def test_single_select_conflict_rejects_candidate(self) -> None:
        left = SpecGroup(
            0,
            [0],
            {"ru": ("rf-1",), "cc location": ("local",)},
        )
        right = SpecGroup(
            1,
            [1],
            {"ru": ("rf-1",), "cc location": ("remote",)},
        )

        candidate, reason = merge_attempt(
            left,
            right,
            ["ru", "cc location"],
            make_support(),
            max_ru=3,
            max_du=3,
            max_ue=10,
        )

        self.assertIsNone(candidate)
        self.assertEqual(reason, "merge_conflict column='cc location'")

    def test_verbose_merge_logs_pair_and_failure(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-2", enb="1"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",)}),
            SpecGroup(1, [1], {"ru": ("rf-2",), "enb": ("1",)}),
        ]
        output = io.StringIO()

        with patch("sys.stdout", output):
            merged, count = merge_small_groups(
                groups,
                ["ru", "enb"],
                cases,
                make_support(),
                max_ru=1,
                max_du=3,
                verbose=True,
            )

        self.assertEqual(count, 0)
        self.assertEqual(len(merged), 2)
        log = output.getvalue()
        self.assertIn("TRY left=spec_1 right=spec_2", log)
        self.assertIn(
            "FAIL left=spec_1 right=spec_2 "
            "condition=max_ru actual=2 limit=1",
            log,
        )

    def test_merge_rejects_candidate_that_loses_covered_any_coverage(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-2"),
            make_case(1, "B", ru="rf-1"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("any",)}),
            SpecGroup(1, [1], {"ru": ("rf-1",)}),
        ]
        original_specs = [dict(group.spec) for group in groups]
        original_coverages = [list(group.covered_indices) for group in groups]
        output = io.StringIO()

        with patch("sys.stdout", output):
            merged, count = merge_small_groups(
                groups,
                ["ru"],
                cases,
                make_support(),
                max_ru=3,
                max_du=3,
                verbose=True,
            )

        self.assertEqual(count, 0)
        self.assertEqual(len(merged), 2)
        self.assertEqual([group.spec for group in groups], original_specs)
        self.assertEqual(
            [group.covered_indices for group in groups],
            original_coverages,
        )
        self.assertIn(
            "FAIL left=spec_1 right=spec_2 "
            "condition=covered_testcase_coverage tc_id=A column='ru' "
            "candidate='rf-1' requirement='rf-2'",
            output.getvalue(),
        )

    def test_verbose_merge_logs_complete_new_spec(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1"),
            make_case(1, "B", ru="rf-1", enb="2"),
        ]
        groups = [
            SpecGroup(0, [0], {"ru": ("rf-1",), "enb": ("1",)}),
            SpecGroup(1, [1], {"ru": ("rf-1",), "enb": ("2",)}),
        ]
        output = io.StringIO()

        with patch("sys.stdout", output):
            merged, count = merge_small_groups(
                groups,
                ["ru", "enb"],
                cases,
                make_support(),
                max_ru=3,
                max_du=3,
                verbose=True,
            )

        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)
        self.assertIn(
            "NEW_SPEC target=spec_1 covered_tc_ids=A + B "
            "ru='rf-1' enb='2'",
            output.getvalue(),
        )

    def test_final_validation_reports_unsatisfied_covered_case(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-1", enb="1", **{"lte band": "b2"}),
            make_case(1, "B", ru="rf-1", enb="1", **{"lte band": "b1"}),
        ]
        group = SpecGroup(
            0,
            [0, 1],
            {"ru": ("rf-1",), "enb": ("1",), "lte band": ("b1",)},
        )

        failures = validate_merged_groups(
            [group],
            ["ru", "enb", "lte band"],
            cases,
            make_support(),
        )

        self.assertEqual([(item.tc_id) for _, item in failures], ["A"])


class MergeIoTests(unittest.TestCase):
    def test_load_groups_requires_complete_coverage(self) -> None:
        cases = [make_case(0, "A", ru="rf-1"), make_case(1, "B", ru="rf-1")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=[
                        "spec_id", "covered_tc_ids", "covered_count",
                        "equipment_count", "solve_status", "ru",
                    ]
                )
                writer.writeheader()
                writer.writerow({
                    "spec_id": "spec_1",
                    "covered_tc_ids": "A",
                    "covered_count": "1",
                    "equipment_count": "1",
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
        self.assertEqual(args.max_ue, 10)
        self.assertFalse(args.verbose)

    def test_parse_args_accepts_verbose_short_option(self) -> None:
        with patch(
            "sys.argv",
            [
                "merge_output_specs.py",
                "--ru-band-support",
                "support.csv",
                "-v",
            ],
        ):
            args = parse_args()
        self.assertTrue(args.verbose)

    def test_main_rejects_negative_max_ue(self) -> None:
        with patch(
            "sys.argv",
            [
                "merge_output_specs.py",
                "--ru-band-support",
                "support.csv",
                "--max-ue",
                "-1",
            ],
        ):
            with self.assertRaisesRegex(SystemExit, "--max-ue must be non-negative"):
                main()

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
                "spec_id,covered_tc_ids,covered_count,equipment_count,solve_status,"
                "enb,lte band,ru\n"
                "spec_1,A,1,2,OPTIMAL,1,b1,rf-1\n"
                "spec_2,B + C,2,2,OPTIMAL,1,b1,rf-1\n",
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
            self.assertEqual(rows[0]["covered_tc_ids"], "A + B + C")
            self.assertEqual(rows[0]["solve_status"], "SECOND_PASS")


if __name__ == "__main__":
    unittest.main()
