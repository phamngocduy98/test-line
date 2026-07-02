"""Core data models for parsing, solving, and output."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Token:
    """One required slot with one or more alternatives."""

    alternatives: tuple[str, ...]

    def as_text(self) -> str:
        return "/".join(self.alternatives)

    def normalized(self) -> tuple[str, ...]:
        return tuple(alternative.casefold() for alternative in self.alternatives)

    def has_any(self) -> bool:
        return any(alternative.casefold() == "any" for alternative in self.alternatives)


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    raw: dict[str, str]
    tokens: dict[str, tuple[Token, ...]]


@dataclass(frozen=True)
class ParsedCsv:
    path: Path
    columns: tuple[str, ...]
    rows: tuple[ParsedRow, ...]


@dataclass(frozen=True)
class SolveOptions:
    ignore_optional_columns: bool = False
    auto_assign: bool = False
    timeout_seconds: float = 600.0
    solver: str = "auto"
    solver_threads: int | None = None
    max_candidates: int = 20000
    max_candidates_per_bucket: int = 250
    max_merge_width: int = 55
    max_extra_slots: int = 1
    max_extra_alternatives: int = 1
    max_numeric_overage_ratio: float = 2.0
    max_numeric_overage_units: int = 1
    reject_spec_side_wildcard: tuple[str, ...] = ()
    min_assigned_cases_per_spec: int = 10


@dataclass(frozen=True)
class SupportTable:
    ru_order: tuple[str, ...]
    lte_by_ru: dict[str, tuple[str, ...]]
    nr_by_ru: dict[str, tuple[str, ...]]
    ru_display: dict[str, str]
    lte_display: dict[str, str]
    nr_display: dict[str, str]


@dataclass(frozen=True)
class Candidate:
    signature: str
    spec: dict[str, tuple[Token, ...]]
    source_indexes: tuple[int, ...]
    equipment_count: int
    coverage: frozenset[int]
    assignment_excess: dict[int, int]
    group_coverage_mask: int = 0
    group_assignment_excess: dict[int, int] = field(default_factory=dict)
    group_weights: tuple[int, ...] = ()


@dataclass(frozen=True)
class Solution:
    candidates: tuple[Candidate, ...]
    assignments: dict[int, Candidate]
    status: str
