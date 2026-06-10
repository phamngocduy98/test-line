import csv
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark_random_inputs import sample_ru, write_ru_band_support
from solve_test_lines import (
    Candidate,
    RuBandSupport,
    TestCase as SolverTestCase,
    add_candidate,
    all_integer_tokens,
    alternatives,
    any_count,
    build_candidate,
    coarse_signature,
    concrete_tokens,
    coverage_delta,
    covers_column,
    equipment_count,
    expand_candidates_for_capacity,
    generate_candidates,
    is_any,
    is_temporarily_ignored_column,
    load_cases,
    load_ru_band_support,
    merge_cases,
    merge_column,
    numeric_equipment,
    parse_cell,
    parse_args,
    relation_satisfied,
    resolve_compatibility_variants,
    render_cell,
    single_select_key,
    slots_cover,
    solve_with_ortools,
    spec_signature,
    spec_has_compatible_ru_bands,
    split_band_tokens,
    main,
    validate_solution,
    validate_support_references,
    write_output,
)


def make_case(index: int, tc_id: str, **values: str) -> SolverTestCase:
    tokens = {column: parse_cell(value) for column, value in values.items()}
    return SolverTestCase(index=index, tc_id=tc_id, raw=values, tokens=tokens)


def make_candidate(
    spec: dict[str, tuple[str, ...]],
    covered: tuple[int, ...],
    deltas: tuple[int, ...],
    count: int = 0,
) -> Candidate:
    return Candidate(
        spec=spec,
        covered=covered,
        deltas=deltas,
        equipment_count=count,
        signature=spec_signature(spec),
    )


def make_support() -> RuBandSupport:
    return RuBandSupport(
        ru_names={"rf-1": "rf-1", "rf-2": "rf-2"},
        lte_band_names={"b1": "b1", "b3": "b3"},
        nr_band_names={"n41": "n41", "n78": "n78"},
        lte_by_ru={"rf-1": frozenset({"b1"}), "rf-2": frozenset({"b3"})},
        nr_by_ru={"rf-1": frozenset({"n41"}), "rf-2": frozenset({"n78"})},
    )


class ParsingTests(unittest.TestCase):
    def test_blank_and_none_cells_are_empty(self) -> None:
        self.assertEqual(parse_cell(""), ())
        self.assertEqual(parse_cell(None), ())

    def test_plus_creates_slots_and_whitespace_is_optional(self) -> None:
        self.assertEqual(parse_cell(" a+b + c "), ("a", "b", "c"))

    def test_slash_creates_normalized_alternatives(self) -> None:
        self.assertEqual(parse_cell("a / b+a"), ("a/b", "a"))

    def test_empty_segments_and_duplicate_alternatives_are_ignored(self) -> None:
        self.assertEqual(parse_cell("a/a++b/"), ("a", "b"))

    def test_render_cell_joins_slots(self) -> None:
        self.assertEqual(render_cell(("a/b", "any")), "a/b + any")

    def test_any_is_case_insensitive_and_works_inside_or(self) -> None:
        self.assertTrue(is_any("ANY"))
        self.assertTrue(is_any("x/Any"))
        self.assertFalse(is_any("anything"))

    def test_alternatives_are_case_insensitive(self) -> None:
        self.assertEqual(alternatives("RF-1/rf-2"), frozenset({"rf-1", "rf-2"}))

    def test_any_and_concrete_helpers(self) -> None:
        tokens = parse_cell("a + any + b/ANY")
        self.assertEqual(any_count(tokens), 2)
        self.assertEqual(concrete_tokens(tokens), ("a",))

    def test_integer_token_detection(self) -> None:
        self.assertTrue(all_integer_tokens(("0", "-1", "2")))
        self.assertFalse(all_integer_tokens(()))
        self.assertFalse(all_integer_tokens(("1", "any")))

    def test_temporary_ignore_column_matching(self) -> None:
        self.assertTrue(is_temporarily_ignored_column(" Tech LTE "))
        self.assertTrue(is_temporarily_ignored_column("ue capa nr"))
        self.assertFalse(is_temporarily_ignored_column("ue"))


class LoadCasesTests(unittest.TestCase):
    def write_csv(self, directory: str, name: str, text: str) -> Path:
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_load_cases_parses_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "input.csv", "tc_id,ru\nA,a/b + any\n")
            columns, cases = load_cases(path)
        self.assertEqual(columns, ["tc_id", "ru"])
        self.assertEqual(cases[0].tokens["ru"], ("a/b", "any"))
        self.assertEqual(cases[0].raw["ru"], "a/b + any")

    def test_load_cases_requires_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "input.csv", "")
            with self.assertRaisesRegex(SystemExit, "no header row"):
                load_cases(path)

    def test_load_cases_requires_tc_id_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "input.csv", "ru\na\n")
            with self.assertRaisesRegex(SystemExit, "must contain a tc_id"):
                load_cases(path)

    def test_load_cases_rejects_empty_tc_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "input.csv", "tc_id,ru\n,a\n")
            with self.assertRaisesRegex(SystemExit, "empty tc_id"):
                load_cases(path)

    def test_load_cases_rejects_header_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "input.csv", "tc_id,ru\n")
            with self.assertRaisesRegex(SystemExit, "no testcase rows"):
                load_cases(path)


class RuBandSupportTests(unittest.TestCase):
    def write_csv(self, directory: str, text: str) -> Path:
        path = Path(directory) / "support.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_load_support_unions_duplicate_rows_and_matches_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(
                directory,
                "ru,lte_band,nr_band\nRF-1,b1/b3,n41\nrf-1,b7,n78\n",
            )
            support = load_ru_band_support(path)
        self.assertEqual(support.ru_names["rf-1"], "RF-1")
        self.assertEqual(support.lte_by_ru["rf-1"], frozenset({"b1", "b3", "b7"}))
        self.assertEqual(support.nr_by_ru["rf-1"], frozenset({"n41", "n78"}))

    def test_load_support_rejects_bad_schema_and_invalid_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad_schema = self.write_csv(directory, "ru,lte_band\nrf-1,b1\n")
            with self.assertRaisesRegex(SystemExit, "must contain columns"):
                load_ru_band_support(bad_schema)
            bad_value = self.write_csv(
                directory, "ru,lte_band,nr_band\nrf-1,any,\n"
            )
            with self.assertRaisesRegex(SystemExit, "invalid lte_band"):
                load_ru_band_support(bad_value)
            bad_ru = self.write_csv(
                directory, "ru,lte_band,nr_band\nintra,b1,\n"
            )
            with self.assertRaisesRegex(SystemExit, "one concrete ru"):
                load_ru_band_support(bad_ru)

    def test_validate_references_reports_unknown_values(self) -> None:
        cases = [
            make_case(
                0,
                "A",
                ru="rf-9",
                **{"lte band": "b9", "nr band": "n99"},
            )
        ]
        with self.assertRaisesRegex(SystemExit, "unknown RUs: rf-9"):
            validate_support_references(
                ["ru", "lte band", "nr band"], cases, make_support()
            )

    def test_validate_references_checks_concrete_alternatives_beside_any(self) -> None:
        cases = [
            make_case(0, "A", ru="rf-9/any", **{"lte band": "b9/any"})
        ]
        with self.assertRaisesRegex(SystemExit, "unknown RUs: rf-9"):
            validate_support_references(
                ["ru", "lte band"], cases, make_support()
            )

    def test_resolve_any_values_to_compatible_concrete_specs(self) -> None:
        support = make_support()
        band_fixed = resolve_compatibility_variants(
            {"ru": ("any",), "lte band": ("b1",)}, support
        )
        self.assertEqual(band_fixed[0]["ru"], ("rf-1",))
        ru_fixed = resolve_compatibility_variants(
            {"ru": ("rf-2",), "lte band": ("any",)}, support
        )
        self.assertEqual(ru_fixed[0]["lte band"], ("b3",))
        both_any = resolve_compatibility_variants(
            {"ru": ("any",), "nr band": ("any",)}, support
        )
        self.assertEqual(
            (both_any[0]["ru"], both_any[0]["nr band"]),
            (("rf-1",), ("n41",)),
        )

    def test_compatibility_is_aggregate_and_blank_columns_stay_blank(self) -> None:
        support = make_support()
        spec = {
            "ru": ("rf-1", "rf-2"),
            "lte band": ("b1", "b3"),
            "nr band": (),
        }
        self.assertTrue(spec_has_compatible_ru_bands(spec, support))
        variants = resolve_compatibility_variants(spec, support)
        self.assertEqual(variants[0]["nr band"], ())
        self.assertFalse(
            spec_has_compatible_ru_bands(
                {"ru": ("rf-1",), "lte band": ("b3",)}, support
            )
        )

    def test_resolution_preserves_slots_alternatives_and_relations(self) -> None:
        variants = resolve_compatibility_variants(
            {
                "ru": ("rf-1/rf-2", "any"),
                "lte band": ("any", "inter"),
            },
            make_support(),
        )
        self.assertTrue(variants)
        self.assertEqual(len(variants[0]["ru"]), 2)
        self.assertEqual(variants[0]["ru"][0], "rf-1/rf-2")
        self.assertEqual(variants[0]["lte band"][1], "inter")
        alternatives_with_any = resolve_compatibility_variants(
            {"ru": ("rf-1/any",), "lte band": ("b1/any",)},
            make_support(),
        )
        self.assertIn("rf-1", alternatives_with_any[0]["ru"][0].split("/"))
        self.assertIn("b1", alternatives_with_any[0]["lte band"][0].split("/"))


class SlotCoverageTests(unittest.TestCase):
    def test_spec_needs_at_least_as_many_slots(self) -> None:
        self.assertFalse(slots_cover(("a",), ("a", "any")))

    def test_each_requirement_needs_a_compatible_slot(self) -> None:
        self.assertFalse(slots_cover(("a", "b"), ("a", "c")))

    def test_matching_uses_distinct_slots(self) -> None:
        self.assertTrue(slots_cover(("a", "b"), ("a/b", "a")))
        self.assertFalse(slots_cover(("a", "c"), ("a/b", "a")))

    def test_spec_any_and_requirement_any_are_wildcards(self) -> None:
        self.assertTrue(slots_cover(("any",), ("a",)))
        self.assertTrue(slots_cover(("a",), ("any",)))

    def test_or_matches_case_insensitively(self) -> None:
        self.assertTrue(slots_cover(("RF-1",), ("rf-1/rf-2",)))


class BandRuleTests(unittest.TestCase):
    def test_split_band_tokens_separates_bands_relations_and_any(self) -> None:
        bands, relations, anys = split_band_tokens(parse_cell("b3/b1 + intra + any"))
        self.assertEqual(bands, ["b1/b3"])
        self.assertEqual(relations, {"intra"})
        self.assertEqual(anys, 1)

    def test_explicit_relation_token_satisfies_relation(self) -> None:
        self.assertTrue(relation_satisfied(parse_cell("any + intra"), "intra"))

    def test_relation_needs_two_slots_without_explicit_token(self) -> None:
        self.assertFalse(relation_satisfied(parse_cell("b1"), "intra"))
        self.assertFalse(relation_satisfied(parse_cell("b1"), "inter"))

    def test_intra_accepts_duplicate_band_or_any(self) -> None:
        self.assertTrue(relation_satisfied(parse_cell("b1 + b1"), "intra"))
        self.assertTrue(relation_satisfied(parse_cell("b1 + any"), "intra"))
        self.assertFalse(relation_satisfied(parse_cell("b1 + b3"), "intra"))

    def test_inter_accepts_distinct_bands_or_any(self) -> None:
        self.assertTrue(relation_satisfied(parse_cell("b1 + b3"), "inter"))
        self.assertTrue(relation_satisfied(parse_cell("b1 + any"), "inter"))
        self.assertFalse(relation_satisfied(parse_cell("b1 + b1"), "inter"))

    def test_unknown_relation_is_not_satisfied(self) -> None:
        self.assertFalse(relation_satisfied(parse_cell("b1 + b3"), "unknown"))

    def test_band_or_matches_either_alternative(self) -> None:
        requirement = parse_cell("b1/b3 + any")
        self.assertTrue(covers_column("lte band", parse_cell("b3 + b7"), requirement)[0])

    def test_band_relation_failure_rejects_coverage(self) -> None:
        self.assertFalse(
            covers_column("lte band", parse_cell("b1 + b1"), parse_cell("inter"))[0]
        )

    def test_missing_required_band_rejects_coverage(self) -> None:
        self.assertFalse(
            covers_column("nr band", parse_cell("n41"), parse_cell("n41 + n78"))[0]
        )


class ColumnCoverageTests(unittest.TestCase):
    def test_empty_requirement_is_always_covered_with_zero_delta(self) -> None:
        self.assertEqual(covers_column("ru", parse_cell("a + b"), ()), (True, 0))

    def test_single_select_rejects_multiple_concrete_spec_values(self) -> None:
        self.assertEqual(
            covers_column("cc location", parse_cell("intra cc + inter cc"), parse_cell("any")),
            (False, 0),
        )

    def test_single_select_allows_concrete_value_for_any(self) -> None:
        self.assertTrue(
            covers_column("cc location", parse_cell("intra cc"), parse_cell("any"))[0]
        )

    def test_numeric_capacity_sums_tokens(self) -> None:
        self.assertEqual(
            covers_column("enb", parse_cell("1 + 2"), parse_cell("2")),
            (True, 1),
        )

    def test_numeric_capacity_rejects_insufficient_value(self) -> None:
        self.assertEqual(covers_column("ue", parse_cell("2"), parse_cell("3")), (False, 0))

    def test_non_numeric_equipment_falls_back_to_slot_matching(self) -> None:
        self.assertEqual(covers_column("ue", parse_cell("any"), parse_cell("nsa")), (True, 0))

    def test_ru_or_matches_first_alternative_with_extra_slot(self) -> None:
        matched, delta = covers_column(
            "ru", parse_cell("rf-1000 + rf-1003"), parse_cell("rf-1000/rf-1001")
        )
        self.assertTrue(matched)
        self.assertEqual(delta, 1)

    def test_ru_or_matches_second_alternative_in_second_spec_slot(self) -> None:
        matched, delta = covers_column(
            "ru", parse_cell("rf-1003 + rf-1001"), parse_cell("rf-1000/rf-1001")
        )
        self.assertTrue(matched)
        self.assertEqual(delta, 1)

    def test_unlisted_or_alternative_does_not_match(self) -> None:
        self.assertFalse(
            covers_column("ru", parse_cell("rf-9"), parse_cell("rf-1/rf-2"))[0]
        )

    def test_delta_over_one_is_rejected_by_default(self) -> None:
        self.assertEqual(
            covers_column("ru", parse_cell("a + b + c"), parse_cell("a")),
            (False, 0),
        )

    def test_delta_over_one_can_be_allowed(self) -> None:
        self.assertEqual(
            covers_column(
                "ru", parse_cell("a + b + c"), parse_cell("a"), enforce_delta=False
            ),
            (True, 2),
        )

    def test_coverage_delta_sums_columns(self) -> None:
        case = make_case(0, "A", ru="a", ue="1")
        result = coverage_delta(
            ["ru", "ue"],
            {"ru": parse_cell("a + b"), "ue": parse_cell("3")},
            case,
        )
        self.assertEqual(result, (True, 3))

    def test_coverage_delta_stops_on_failed_column(self) -> None:
        case = make_case(0, "A", ru="missing", ue="1")
        self.assertEqual(
            coverage_delta(
                ["ru", "ue"],
                {"ru": parse_cell("a"), "ue": parse_cell("3")},
                case,
            ),
            (False, 0),
        )


class MergeTests(unittest.TestCase):
    def test_numeric_merge_keeps_max_total_capacity(self) -> None:
        self.assertEqual(
            merge_column("enb", [parse_cell("1 + 2"), parse_cell("2")]),
            ("3",),
        )

    def test_non_numeric_merge_keeps_max_duplicate_count(self) -> None:
        self.assertEqual(
            merge_column("ru", [parse_cell("a + a"), parse_cell("a + b")]),
            ("a", "a", "b"),
        )

    def test_merge_uses_any_to_preserve_max_slot_count(self) -> None:
        self.assertEqual(
            merge_column("ru", [parse_cell("a"), parse_cell("any + any")]),
            ("a", "any"),
        )

    def test_band_relation_tokens_are_canonicalized(self) -> None:
        self.assertEqual(
            merge_column("lte band", [parse_cell("INTRA"), parse_cell("intra")]),
            ("intra",),
        )

    def test_single_select_conflict_cannot_merge(self) -> None:
        self.assertIsNone(
            merge_column(
                "cc location", [parse_cell("intra cc"), parse_cell("inter cc")]
            )
        )

    def test_merge_cases_returns_none_on_column_conflict(self) -> None:
        cases = [
            make_case(0, "A", **{"cc location": "intra cc"}),
            make_case(1, "B", **{"cc location": "inter cc"}),
        ]
        self.assertIsNone(merge_cases(["cc location"], cases))

    def test_merge_cases_builds_all_columns(self) -> None:
        cases = [
            make_case(0, "A", ru="a", ue="1"),
            make_case(1, "B", ru="any + any", ue="2"),
        ]
        self.assertEqual(
            merge_cases(["ru", "ue"], cases),
            {"ru": ("a", "any"), "ue": ("2",)},
        )


class EquipmentTests(unittest.TestCase):
    def test_numeric_equipment_counts_blank_any_integer_and_text(self) -> None:
        self.assertEqual(numeric_equipment(()), 0)
        self.assertEqual(numeric_equipment(parse_cell("any + 2 + nsa")), 4)

    def test_equipment_count_only_counts_du_ru_and_ue(self) -> None:
        columns = ["enb", "vdu", "au", "cu", "ru", "ue", "lte band"]
        spec = {
            "enb": ("2",),
            "vdu": ("1",),
            "au": ("0",),
            "cu": ("1",),
            "ru": ("a/b", "c"),
            "ue": ("3",),
            "lte band": ("b1", "b3"),
        }
        self.assertEqual(equipment_count(columns, spec), 9)

    def test_equipment_count_skips_absent_requirement_columns(self) -> None:
        self.assertEqual(equipment_count(["ru"], {"ru": ("a", "b")}), 2)


class SignatureTests(unittest.TestCase):
    def test_spec_signature_sorts_columns(self) -> None:
        self.assertEqual(
            spec_signature({"ru": ("a",), "enb": ("1",)}),
            (("enb", ("1",)), ("ru", ("a",))),
        )

    def test_single_select_key_ignores_any_and_missing_columns(self) -> None:
        any_case = make_case(0, "A", **{"cc location": "any"})
        missing_case = make_case(1, "B", ru="a")
        self.assertEqual(single_select_key(any_case), (("cc location", ()),))
        self.assertEqual(single_select_key(missing_case), ())

    def test_coarse_signature_modes(self) -> None:
        case = make_case(
            0,
            "A",
            **{"cc location": "intra cc", "lte band": "b1 + inter", "ru": "a", "ue": "2"},
        )
        without_equipment = coarse_signature(
            ["cc location", "lte band", "ru", "ue"], case, include_equipment=False
        )
        with_equipment = coarse_signature(
            ["cc location", "lte band", "ru", "ue"], case, include_equipment=True
        )
        self.assertIn(("lte band", ("inter",)), without_equipment)
        self.assertNotIn(("ru", ("a",)), without_equipment)
        self.assertIn(("ru", ("a",)), with_equipment)
        self.assertIn(("ue", ("2",)), with_equipment)


class CandidateTests(unittest.TestCase):
    def test_build_candidate_requires_indices(self) -> None:
        case = make_case(0, "A", ru="a")
        self.assertIsNone(build_candidate(["ru"], [case], [], [case], 0))

    def test_build_candidate_rejects_unmergeable_cases(self) -> None:
        cases = [
            make_case(0, "A", **{"cc location": "intra cc"}),
            make_case(1, "B", **{"cc location": "inter cc"}),
        ]
        self.assertIsNone(build_candidate(["cc location"], cases, [0, 1], cases, 0))

    def test_build_candidate_covers_compatible_cases(self) -> None:
        cases = [make_case(0, "A", ru="a"), make_case(1, "B", ru="any")]
        candidate = build_candidate(["ru"], cases, [0], cases, 0)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.covered, (0, 1))

    def test_build_candidate_keeps_seed_coverage_when_checks_are_capped(self) -> None:
        cases = [
            make_case(0, "A", ru="a"),
            make_case(1, "B", ru="b"),
            make_case(2, "C", ru="c"),
        ]
        candidate = build_candidate(["ru"], cases, [2], cases, 1)
        self.assertIsNotNone(candidate)
        self.assertIn(2, candidate.covered)

    def test_add_candidate_ignores_none(self) -> None:
        candidates = {}
        add_candidate(candidates, None)
        self.assertEqual(candidates, {})

    def test_add_candidate_unions_coverage_and_keeps_lowest_delta(self) -> None:
        spec = {"ru": ("a",)}
        candidates = {}
        add_candidate(candidates, make_candidate(spec, (0,), (2,)))
        add_candidate(candidates, make_candidate(spec, (0, 1), (1, 0)))
        merged = candidates[spec_signature(spec)]
        self.assertEqual(merged.covered, (0, 1))
        self.assertEqual(merged.deltas, (1, 0))

    def test_generate_candidates_includes_exact_rows(self) -> None:
        cases = [make_case(0, "A", ru="a"), make_case(1, "B", ru="b")]
        candidates = generate_candidates(["ru"], cases, 10, 0)
        covered = {index for candidate in candidates for index in candidate.covered}
        self.assertEqual(covered, {0, 1})

    def test_generate_candidates_resolves_ru_band_any_values(self) -> None:
        cases = [
            make_case(0, "A", ru="any", **{"lte band": "b1"}),
            make_case(1, "B", ru="rf-2", **{"lte band": "any"}),
        ]
        candidates = generate_candidates(
            ["lte band", "ru"], cases, 10, 0, support=make_support()
        )
        self.assertTrue(candidates)
        self.assertTrue(
            all(
                "any" not in candidate.spec["ru"]
                and "any" not in candidate.spec["lte band"]
                and spec_has_compatible_ru_bands(candidate.spec, make_support())
                for candidate in candidates
            )
        )

    def test_generate_candidates_rejects_incompatible_exact_case(self) -> None:
        cases = [make_case(0, "A", ru="rf-1", **{"lte band": "b3"})]
        with self.assertRaisesRegex(SystemExit, "testcase A"):
            generate_candidates(
                ["lte band", "ru"], cases, 10, 0, support=make_support()
            )

    def test_exact_wildcard_row_adds_one_compatibility_variant(self) -> None:
        cases = [make_case(0, "A", ru="any", **{"lte band": "any"})]
        candidates = generate_candidates(
            ["lte band", "ru"], cases, 10, 0, support=make_support()
        )
        self.assertEqual(len(candidates), 1)

    def test_merge_candidates_respect_bucket_cap(self) -> None:
        cases = [
            make_case(index, str(index), ru=value)
            for index, value in enumerate(("a", "b", "c", "d", "e", "f"))
        ]
        candidates = generate_candidates(["ru"], cases, 2, 0)
        exact_signatures = {
            spec_signature({"ru": (value,)})
            for value in ("a", "b", "c", "d", "e", "f")
        }
        merged = [
            candidate
            for candidate in candidates
            if candidate.signature not in exact_signatures
        ]
        self.assertLessEqual(len(merged), 2)

    def test_expand_candidates_for_capacity(self) -> None:
        candidate = make_candidate({"ru": ("a",)}, (0, 1, 2, 3, 4), (0, 0, 0, 0, 0))
        self.assertEqual(len(expand_candidates_for_capacity([candidate], 2)), 3)


class SolverAndValidationTests(unittest.TestCase):
    def test_solver_selects_and_assigns_all_cases(self) -> None:
        cases = [make_case(0, "A", ru="a"), make_case(1, "B", ru="b")]
        candidates = expand_candidates_for_capacity(
            generate_candidates(["ru"], cases, 10, 0), 10
        )
        status, selected, assignments = solve_with_ortools(candidates, cases, 10, 10)
        self.assertEqual(status, "OPTIMAL")
        self.assertEqual(set(assignments), {0, 1})
        validate_solution(["ru"], cases, candidates, selected, assignments, 10)

    def test_solver_rejects_uncovered_case(self) -> None:
        cases = [make_case(0, "A", ru="a")]
        candidates = [make_candidate({"ru": ("b",)}, (), ())]
        with self.assertRaisesRegex(SystemExit, "no candidate covers"):
            solve_with_ortools(candidates, cases, 1, 1)

    def test_validate_requires_every_case_assignment(self) -> None:
        cases = [make_case(0, "A", ru="a")]
        with self.assertRaisesRegex(SystemExit, "every testcase"):
            validate_solution(["ru"], cases, [], [], {}, 1)

    def test_validate_requires_selected_and_assigned_sets_to_match(self) -> None:
        cases = [make_case(0, "A", ru="a")]
        candidate = make_candidate({"ru": ("a",)}, (0,), (0,))
        with self.assertRaisesRegex(SystemExit, "selected specs"):
            validate_solution(["ru"], cases, [candidate], [], {0: 0}, 1)

    def test_validate_enforces_testcase_limit(self) -> None:
        cases = [make_case(0, "A", ru="a"), make_case(1, "B", ru="a")]
        candidate = make_candidate({"ru": ("a",)}, (0, 1), (0, 0))
        with self.assertRaisesRegex(SystemExit, "testcase limit"):
            validate_solution(["ru"], cases, [candidate], [0], {0: 0, 1: 0}, 1)

    def test_validate_rejects_uncovered_assignment(self) -> None:
        cases = [make_case(0, "A", ru="a")]
        candidate = make_candidate({"ru": ("b",)}, (0,), (0,))
        with self.assertRaisesRegex(SystemExit, "not covered"):
            validate_solution(["ru"], cases, [candidate], [0], {0: 0}, 1)

    def test_validate_rejects_multiple_single_select_values(self) -> None:
        cases = [make_case(0, "A", **{"cc location": "any"})]
        spec = {"cc location": parse_cell("intra cc + inter cc")}
        candidate = make_candidate(spec, (0,), (0,))
        with patch("solve_test_lines.coverage_delta", return_value=(True, 0)):
            with self.assertRaisesRegex(SystemExit, "multiple concrete values"):
                validate_solution(
                    ["cc location"], cases, [candidate], [0], {0: 0}, 1
                )

    def test_validate_rejects_wrong_equipment_count(self) -> None:
        cases = [make_case(0, "A", ru="a")]
        candidate = make_candidate({"ru": ("a",)}, (0,), (0,), count=99)
        with self.assertRaisesRegex(SystemExit, "equipment count mismatch"):
            validate_solution(["ru"], cases, [candidate], [0], {0: 0}, 1)

    def test_validate_can_disable_delta_limit(self) -> None:
        cases = [make_case(0, "A", ru="a")]
        spec = {"ru": parse_cell("a + b + c")}
        candidate = make_candidate(spec, (0,), (2,), count=3)
        validate_solution(
            ["ru"], cases, [candidate], [0], {0: 0}, 1, enforce_delta=False
        )

    def test_validate_rejects_incompatible_ru_band_spec(self) -> None:
        cases = [make_case(0, "A", ru="rf-1", **{"lte band": "b3"})]
        spec = {"ru": ("rf-1",), "lte band": ("b3",)}
        candidate = make_candidate(spec, (0,), (0,), count=1)
        with self.assertRaisesRegex(SystemExit, "not covered"):
            validate_solution(
                ["lte band", "ru"],
                cases,
                [candidate],
                [0],
                {0: 0},
                1,
                support=make_support(),
            )


class OutputTests(unittest.TestCase):
    def test_write_output_writes_metrics_and_ignored_columns_blank(self) -> None:
        cases = [
            SolverTestCase(
                index=0,
                tc_id="A",
                raw={"ru": "a", "tech lte": "1"},
                tokens={"ru": ("a",), "tech lte": ("1",)},
            )
        ]
        candidate = make_candidate({"ru": ("a",)}, (0,), (0,), count=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.csv"
            metrics = write_output(
                path,
                ["tc_id", "ru", "tech lte"],
                ["ru"],
                cases,
                [candidate],
                [0],
                {0: 0},
                "OPTIMAL",
            )
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(metrics, (1, 1, 1, 0, [1]))
        self.assertEqual(rows[0]["assigned_tc_ids"], "A")
        self.assertEqual(rows[0]["ru"], "a")
        self.assertEqual(rows[0]["tech lte"], "")


class CliTests(unittest.TestCase):
    def test_parse_args_defaults(self) -> None:
        with patch(
            "sys.argv",
            ["solve_test_lines.py", "--ru-band-support", "support.csv"],
        ):
            args = parse_args()
        self.assertEqual(args.input, "input.csv")
        self.assertEqual(args.output, "output_specs.csv")
        self.assertEqual(args.ru_band_support, "support.csv")
        self.assertEqual(args.timeout, 600.0)
        self.assertEqual(args.max_tc_per_spec, 338)
        self.assertFalse(args.ignore_tech_and_ue_capa)

    def test_parse_args_accepts_all_options(self) -> None:
        argv = [
            "solve_test_lines.py",
            "--input",
            "in.csv",
            "--output",
            "out.csv",
            "--ru-band-support",
            "support.csv",
            "--timeout",
            "12",
            "--max-candidates-per-bucket",
            "5",
            "--max-cover-checks-per-candidate",
            "7",
            "--ignore-tech-and-ue-capa",
            "--max-tc-per-spec",
            "9",
        ]
        with patch("sys.argv", argv):
            args = parse_args()
        self.assertEqual(args.input, "in.csv")
        self.assertEqual(args.output, "out.csv")
        self.assertEqual(args.ru_band_support, "support.csv")
        self.assertEqual(args.timeout, 12)
        self.assertEqual(args.max_candidates_per_bucket, 5)
        self.assertEqual(args.max_cover_checks_per_candidate, 7)
        self.assertTrue(args.ignore_tech_and_ue_capa)
        self.assertEqual(args.max_tc_per_spec, 9)

    def test_main_rejects_non_positive_testcase_limit(self) -> None:
        with patch(
            "sys.argv",
            [
                "solve_test_lines.py",
                "--ru-band-support",
                "support.csv",
                "--max-tc-per-spec",
                "0",
            ],
        ):
            with self.assertRaisesRegex(SystemExit, "must be positive"):
                main()

    def test_main_rejects_empty_candidate_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.csv"
            output_path = Path(directory) / "output.csv"
            support_path = Path(directory) / "support.csv"
            input_path.write_text("tc_id,ru\nA,a\n", encoding="utf-8")
            support_path.write_text(
                "ru,lte_band,nr_band\na,b1,n1\n", encoding="utf-8"
            )
            argv = [
                "solve_test_lines.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--ru-band-support",
                str(support_path),
            ]
            with patch("sys.argv", argv), patch(
                "solve_test_lines.generate_candidates", return_value=[]
            ):
                with self.assertRaisesRegex(SystemExit, "no candidate specs"):
                    main()

    def test_main_ignored_columns_are_blank_in_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.csv"
            output_path = Path(directory) / "output.csv"
            support_path = Path(directory) / "support.csv"
            input_path.write_text(
                "tc_id,tech lte,ru,ue capa lte\nA,1,rf-1,volte\n",
                encoding="utf-8",
            )
            support_path.write_text(
                "ru,lte_band,nr_band\nrf-1,b1,n1\n", encoding="utf-8"
            )
            argv = [
                "solve_test_lines.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--ru-band-support",
                str(support_path),
                "--ignore-tech-and-ue-capa",
                "--timeout",
                "10",
            ]
            with patch("sys.argv", argv):
                self.assertEqual(main(), 0)
            with output_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["ru"], "rf-1")
        self.assertEqual(row["tech lte"], "")
        self.assertEqual(row["ue capa lte"], "")

    def test_main_resolves_compatible_any_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.csv"
            output_path = Path(directory) / "output.csv"
            support_path = Path(directory) / "support.csv"
            input_path.write_text(
                "tc_id,lte band,nr band,ru\n"
                "A,b1,,any\n"
                "B,any,,rf-2\n",
                encoding="utf-8",
            )
            support_path.write_text(
                "ru,lte_band,nr_band\nrf-1,b1,n41\nrf-2,b3,n78\n",
                encoding="utf-8",
            )
            argv = [
                "solve_test_lines.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--ru-band-support",
                str(support_path),
                "--timeout",
                "10",
            ]
            with patch("sys.argv", argv):
                self.assertEqual(main(), 0)
            with output_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertTrue(all(row["ru"] != "any" for row in rows))
        self.assertTrue(all(row["lte band"] != "any" for row in rows))


class BenchmarkRuTests(unittest.TestCase):
    def test_random_ru_generation_includes_or_slots(self) -> None:
        rng = random.Random(1)
        generated = [sample_ru(rng, "high") for _ in range(100)]
        self.assertTrue(any("/" in value for value in generated))
        for value in generated:
            for slot in value.split(" + "):
                choices = slot.split("/")
                self.assertLessEqual(len(choices), 2)
                self.assertEqual(len(choices), len(set(choices)))

    def test_random_ru_generation_preserves_slot_width(self) -> None:
        rng = random.Random(2)
        for _ in range(100):
            value = sample_ru(rng, "high")
            self.assertGreaterEqual(len(value.split(" + ")), 1)
            self.assertLessEqual(len(value.split(" + ")), 4)

    def test_benchmark_support_table_contains_all_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "support.csv"
            write_ru_band_support(path)
            support = load_ru_band_support(path)
        self.assertEqual(len(support.ru_names), 8)
        self.assertTrue(all(len(bands) == 8 for bands in support.lte_by_ru.values()))
        self.assertTrue(all(len(bands) == 8 for bands in support.nr_by_ru.values()))


if __name__ == "__main__":
    unittest.main()
