from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_line_solver.errors import InputError
from test_line_solver.parsing import parsed_payload_to_json, read_ru_band_csv, read_testcase_csv
from test_line_solver.support import build_support_table
from test_line_solver.validation import validate_testcases


class ParserValidationTests(unittest.TestCase):
    def write(self, directory: Path, name: str, text: str) -> Path:
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def load_valid(self, directory: Path, input_text: str):
        input_path = self.write(directory, "input.csv", input_text)
        support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRF-1,b1 + b2,n1\n")
        testcase_csv = read_testcase_csv(input_path, require_ru=True)
        support_csv = read_ru_band_csv(support_path)
        support = build_support_table(support_csv)
        validate_testcases(testcase_csv, support, final_solver=True)
        return testcase_csv, support_csv

    def test_parse_context_preserves_shape_and_excludes_tc_id_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            testcase_csv, support_csv = self.load_valid(
                directory,
                "tc_id,enb,lte band,ru,unknown\n"
                " T1 ,1,b1 + b2,any,\n"
                "T2,,b1,RF-1,a/a++b/\n",
            )

            self.assertEqual(("tc_id", "enb", "lte band", "ru", "unknown"), testcase_csv.columns)
            self.assertEqual([2, 3], [row.row_number for row in testcase_csv.rows])
            self.assertNotIn("tc_id", testcase_csv.rows[0].tokens)
            self.assertEqual([["b1"], ["b2"]], [list(token.alternatives) for token in testcase_csv.rows[0].tokens["lte band"]])
            self.assertEqual([], [list(token.alternatives) for token in testcase_csv.rows[0].tokens["unknown"]])
            self.assertEqual([], [list(token.alternatives) for token in testcase_csv.rows[1].tokens["enb"]])
            self.assertEqual([["b1"]], [list(token.alternatives) for token in testcase_csv.rows[1].tokens["lte band"]])
            self.assertEqual([["a"], ["b"]], [list(token.alternatives) for token in testcase_csv.rows[1].tokens["unknown"]])

            payload = json.loads(parsed_payload_to_json(testcase_csv, support_csv))
            self.assertEqual("input.csv", Path(payload["input"]["path"]).name)
            self.assertNotIn("tc_id", payload["input"]["rows"][0]["tokens"])

    def test_parse_only_does_not_require_testcase_ru(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band\nT1,\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRF-1,,\n")
            testcase_csv = read_testcase_csv(input_path, require_ru=False)
            support_csv = read_ru_band_csv(support_path)
            support = build_support_table(support_csv)
            validate_testcases(testcase_csv, support, final_solver=False)
            self.assertEqual([], [list(token.alternatives) for token in testcase_csv.rows[0].tokens["lte band"]])

    def test_blank_cells_are_empty_but_explicit_any_and_zero_remain_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(
                directory,
                "input.csv",
                "tc_id,tech nsa,lte band,nr band,ru,unknown\n"
                "T1,0,,,any,\n"
                "T2,any,any,any,any,0\n",
            )
            testcase_csv = read_testcase_csv(input_path, require_ru=True)
            first = testcase_csv.rows[0].tokens
            second = testcase_csv.rows[1].tokens
            self.assertEqual([["0"]], [list(token.alternatives) for token in first["tech nsa"]])
            self.assertEqual([], [list(token.alternatives) for token in first["lte band"]])
            self.assertEqual([], [list(token.alternatives) for token in first["nr band"]])
            self.assertEqual([["any"]], [list(token.alternatives) for token in first["ru"]])
            self.assertEqual([], [list(token.alternatives) for token in first["unknown"]])
            self.assertEqual([["any"]], [list(token.alternatives) for token in second["tech nsa"]])
            self.assertEqual([["any"]], [list(token.alternatives) for token in second["lte band"]])
            self.assertEqual([["any"]], [list(token.alternatives) for token in second["nr band"]])
            self.assertEqual([["0"]], [list(token.alternatives) for token in second["unknown"]])

    def test_final_solver_requires_ru_and_testcase_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            input_path = self.write(directory, "input.csv", "tc_id,lte band\nT1,b1\n")
            with self.assertRaisesRegex(InputError, "missing required column"):
                read_testcase_csv(input_path, require_ru=True)

            empty_path = self.write(directory, "empty.csv", "tc_id,ru\n")
            support_path = self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRF-1,b1,\n")
            testcase_csv = read_testcase_csv(empty_path, require_ru=True)
            support = build_support_table(read_ru_band_csv(support_path))
            with self.assertRaisesRegex(InputError, "no testcase rows"):
                validate_testcases(testcase_csv, support, final_solver=True)

    def test_rejects_duplicate_blank_tc_id_and_invalid_numeric(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            support = build_support_table(read_ru_band_csv(self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRF-1,b1,\n")))
            duplicate = read_testcase_csv(self.write(directory, "dup.csv", "tc_id,enb,lte band,ru\nA,1,b1,RF-1\n A ,1,b1,RF-1\n"), require_ru=True)
            with self.assertRaisesRegex(InputError, "duplicate tc_id"):
                validate_testcases(duplicate, support, final_solver=True)

            blank = read_testcase_csv(self.write(directory, "blank.csv", "tc_id,enb,lte band,ru\n ,1,b1,RF-1\n"), require_ru=True)
            with self.assertRaisesRegex(InputError, "empty tc_id"):
                validate_testcases(blank, support, final_solver=True)

            invalid = read_testcase_csv(self.write(directory, "badnum.csv", "tc_id,enb,lte band,ru\nA,1 + 2,b1,RF-1\n"), require_ru=True)
            with self.assertRaisesRegex(InputError, "enb must be one non-negative integer"):
                validate_testcases(invalid, support, final_solver=True)

            for value in ("1/2", "any", "-1", "1.5"):
                with self.subTest(value=value):
                    invalid = read_testcase_csv(
                        self.write(directory, f"badnum-{value.replace('/', '-')}.csv", f"tc_id,enb,lte band,ru\nA,{value},b1,RF-1\n"),
                        require_ru=True,
                    )
                    with self.assertRaisesRegex(InputError, "enb must be one non-negative integer"):
                        validate_testcases(invalid, support, final_solver=True)

    def test_rejects_invalid_support_table_and_unknown_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaisesRegex(InputError, "support-table ru"):
                build_support_table(read_ru_band_csv(self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nany,b1,\n")))

            with self.assertRaisesRegex(InputError, "must not contain special value"):
                build_support_table(read_ru_band_csv(self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRF-1,any,\n")))

            support = build_support_table(read_ru_band_csv(self.write(directory, "valid-support.csv", "ru,lte_band,nr_band\nRF-1,b1,\n")))
            unknown = read_testcase_csv(self.write(directory, "unknown.csv", "tc_id,lte band,ru\nA,b2,RF-1\n"), require_ru=True)
            with self.assertRaisesRegex(InputError, "unknown concrete lte band"):
                validate_testcases(unknown, support, final_solver=True)

            unknown_ru = read_testcase_csv(self.write(directory, "unknown-ru.csv", "tc_id,lte band,ru\nA,b1,RF-2\n"), require_ru=True)
            with self.assertRaisesRegex(InputError, "unknown concrete RU"):
                validate_testcases(unknown_ru, support, final_solver=True)

    def test_support_rows_with_same_ru_are_union_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            support = build_support_table(
                read_ru_band_csv(self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRF-1,b1,n1\nrf-1,b2,n2\n"))
            )
            self.assertEqual(("rf-1",), support.ru_order)
            self.assertEqual(("b1", "b2"), support.lte_by_ru["rf-1"])
            self.assertEqual(("n1", "n2"), support.nr_by_ru["rf-1"])
            self.assertEqual("RF-1", support.ru_display["rf-1"])

    def test_rejects_no_compatible_ru_band_realization(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            support = build_support_table(
                read_ru_band_csv(self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nRF-1,b1,\nRF-2,b2,\n"))
            )
            testcase_csv = read_testcase_csv(
                self.write(directory, "input.csv", "tc_id,lte band,ru\nA,b2,RF-1\n"),
                require_ru=True,
            )
            with self.assertRaisesRegex(InputError, "no compatible RU-band realization"):
                validate_testcases(testcase_csv, support, final_solver=True)

    def test_blank_nr_band_means_no_requirement_for_line_627_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            support = build_support_table(read_ru_band_csv(self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nrf1,b46,\n")))
            testcase_csv = read_testcase_csv(
                self.write(
                    directory,
                    "input.csv",
                    "tc_id,tech lte,tech nsa,tech nr sa,enb,vdu,au,cu,lte band,nr band,ru,cc location,ca type,rf condition,ue,ue capa lte,ue capa nr,ue capa special\n"
                    "626,1,0,0,1,0,0,0,b46,,any,any,any,any,0,any,,\n",
                ),
                require_ru=True,
            )
            validate_testcases(testcase_csv, support, final_solver=True)

    def test_explicit_nr_any_still_requires_nr_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            support = build_support_table(read_ru_band_csv(self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nrf1,b46,\n")))
            testcase_csv = read_testcase_csv(
                self.write(directory, "input.csv", "tc_id,lte band,nr band,ru\n626,b46,any,any\n"),
                require_ru=True,
            )
            with self.assertRaisesRegex(InputError, "no compatible RU-band realization"):
                validate_testcases(testcase_csv, support, final_solver=True)

    def test_blank_ru_requires_blank_bands(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            support = build_support_table(read_ru_band_csv(self.write(directory, "ru-band.csv", "ru,lte_band,nr_band\nrf1,b46,\n")))
            no_bands = read_testcase_csv(self.write(directory, "no-bands.csv", "tc_id,lte band,nr band,ru\nA,,,\n"), require_ru=True)
            validate_testcases(no_bands, support, final_solver=True)

            with_band = read_testcase_csv(self.write(directory, "with-band.csv", "tc_id,lte band,nr band,ru\nA,b46,,\n"), require_ru=True)
            with self.assertRaisesRegex(InputError, "no compatible RU-band realization"):
                validate_testcases(with_band, support, final_solver=True)


if __name__ == "__main__":
    unittest.main()
