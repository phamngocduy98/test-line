"""Grouped testcase indexes for solver coverage and optimization."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import BAND_COLUMNS, NUMERIC_COLUMNS
from .coverage import active_requirement_columns, coverage_excess, numeric_value, spec_ru_band_compatible
from .models import ParsedCsv, SolveOptions, SupportTable, Token


NormalizedTokens = tuple[tuple[tuple[str, ...], ...], ...]


@dataclass(frozen=True)
class ColumnFeatures:
    normal_tokens: tuple[tuple[str, ...], ...]
    relation_count: int
    normal_count: int


@dataclass(frozen=True)
class CaseGroup:
    signature: NormalizedTokens
    tokens: dict[str, tuple[Token, ...]]
    row_indexes: tuple[int, ...]
    numeric_values: dict[str, int]
    column_features: dict[str, ColumnFeatures]

    @property
    def weight(self) -> int:
        return len(self.row_indexes)


@dataclass(frozen=True)
class IndexedCoverage:
    group_mask: int
    row_indexes: tuple[int, ...]
    excess_by_group: dict[int, int]

    def weighted_excess(self, groups: tuple[CaseGroup, ...]) -> int:
        return sum(groups[index].weight * excess for index, excess in self.excess_by_group.items())


@dataclass(frozen=True)
class CoverageIndex:
    columns: tuple[str, ...]
    groups: tuple[CaseGroup, ...]
    row_to_group: tuple[int, ...]
    support: SupportTable
    options: SolveOptions
    spec_compatibility_cache: dict[NormalizedTokens, bool]

    @classmethod
    def build(cls, parsed: ParsedCsv, support: SupportTable, options: SolveOptions) -> "CoverageIndex":
        columns = active_requirement_columns(parsed.columns, options)
        groups_by_signature: dict[NormalizedTokens, int] = {}
        group_tokens: list[dict[str, tuple[Token, ...]]] = []
        group_rows: list[list[int]] = []
        row_to_group: list[int] = []

        for row_index, row in enumerate(parsed.rows):
            signature = _row_signature(row.tokens, columns)
            group_index = groups_by_signature.get(signature)
            if group_index is None:
                group_index = len(group_tokens)
                groups_by_signature[signature] = group_index
                group_tokens.append({column: row.tokens.get(column, ()) for column in columns})
                group_rows.append([])
            group_rows[group_index].append(row_index)
            row_to_group.append(group_index)

        groups = tuple(
            CaseGroup(
                signature=signature,
                tokens=group_tokens[index],
                row_indexes=tuple(group_rows[index]),
                numeric_values=_numeric_values(group_tokens[index], columns),
                column_features=_column_features(group_tokens[index], columns),
            )
            for signature, index in sorted(groups_by_signature.items(), key=lambda item: item[1])
        )
        return cls(
            columns=columns,
            groups=groups,
            row_to_group=tuple(row_to_group),
            support=support,
            options=options,
            spec_compatibility_cache={},
        )

    def coverage_for_spec(self, spec: dict[str, tuple[Token, ...]]) -> IndexedCoverage:
        group_mask = 0
        row_indexes: list[int] = []
        excess_by_group: dict[int, int] = {}

        spec_signature = _row_signature(spec, self.columns)
        compatible = self.spec_compatibility_cache.get(spec_signature)
        if compatible is None:
            compatible = spec_ru_band_compatible(spec, self.support)
            self.spec_compatibility_cache[spec_signature] = compatible
        if not compatible:
            return IndexedCoverage(group_mask=0, row_indexes=(), excess_by_group={})

        spec_numeric_values = _numeric_values(spec, self.columns)
        spec_features = _column_features(spec, self.columns)
        for group_index, group in enumerate(self.groups):
            if not _might_cover_group(group, spec_numeric_values, spec_features, self.columns, self.options):
                continue
            result = coverage_excess(group.tokens, spec, self.columns, self.support, self.options)
            if result is None:
                continue
            group_mask |= 1 << group_index
            row_indexes.extend(group.row_indexes)
            if result.excess:
                excess_by_group[group_index] = result.excess

        return IndexedCoverage(
            group_mask=group_mask,
            row_indexes=tuple(row_indexes),
            excess_by_group=excess_by_group,
        )

    def expand_group_mask(self, group_mask: int) -> tuple[int, ...]:
        row_indexes: list[int] = []
        for group_index, group in enumerate(self.groups):
            if group_mask & (1 << group_index):
                row_indexes.extend(group.row_indexes)
        return tuple(row_indexes)


def _row_signature(tokens_by_column: dict[str, tuple[Token, ...]], columns: tuple[str, ...]) -> NormalizedTokens:
    return tuple(
        tuple(token.normalized() for token in tokens_by_column.get(column, ()))
        for column in columns
    )


def _numeric_values(tokens_by_column: dict[str, tuple[Token, ...]], columns: tuple[str, ...]) -> dict[str, int]:
    return {
        column: numeric_value(tokens_by_column.get(column, ()))
        for column in columns
        if column in NUMERIC_COLUMNS
    }


def _column_features(tokens_by_column: dict[str, tuple[Token, ...]], columns: tuple[str, ...]) -> dict[str, ColumnFeatures]:
    features: dict[str, ColumnFeatures] = {}
    for column in columns:
        if column in NUMERIC_COLUMNS:
            continue
        relation_count = 0
        normal_tokens: list[tuple[str, ...]] = []
        for token in tokens_by_column.get(column, ()):
            normalized = token.normalized()
            if column in BAND_COLUMNS and _is_relation_values(normalized):
                relation_count += 1
            else:
                normal_tokens.append(normalized)
        features[column] = ColumnFeatures(
            normal_tokens=tuple(normal_tokens),
            relation_count=relation_count,
            normal_count=len(normal_tokens),
        )
    return features


def _might_cover_group(
    group: CaseGroup,
    spec_numeric_values: dict[str, int],
    spec_features: dict[str, ColumnFeatures],
    columns: tuple[str, ...],
    options: SolveOptions,
) -> bool:
    for column in columns:
        if column in NUMERIC_COLUMNS:
            testcase_value = group.numeric_values.get(column, 0)
            spec_value = spec_numeric_values.get(column, 0)
            if spec_value < testcase_value:
                return False
            overage = spec_value - testcase_value
            if (
                testcase_value > 0
                and spec_value > testcase_value * options.max_numeric_overage_ratio
                and overage > options.max_numeric_overage_units
            ):
                return False
            continue

        testcase_features = group.column_features[column]
        spec_column_features = spec_features[column]
        relation_consumed_slots = 2 * testcase_features.relation_count
        if spec_column_features.normal_count + relation_consumed_slots < testcase_features.normal_count:
            return False
        extra_slots = max(
            0,
            spec_column_features.normal_count - testcase_features.normal_count - relation_consumed_slots,
        )
        if extra_slots > options.max_extra_slots:
            return False
        for testcase_token in testcase_features.normal_tokens:
            if not any(_slot_might_match(column, testcase_token, spec_token, options) for spec_token in spec_column_features.normal_tokens):
                return False
    return True


def _slot_might_match(column: str, testcase: tuple[str, ...], spec: tuple[str, ...], options: SolveOptions) -> bool:
    testcase_values = set(testcase)
    spec_values = set(spec)
    if "any" in testcase_values:
        return True
    if "any" in spec_values:
        return column not in options.reject_spec_side_wildcard
    if not (testcase_values & spec_values):
        return False
    return len(spec_values - testcase_values) <= options.max_extra_alternatives


def _is_relation_values(values: tuple[str, ...]) -> bool:
    return bool(set(values) & {"intra", "inter"})
