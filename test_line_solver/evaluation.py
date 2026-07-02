"""Final expanded-solution assignment and low-use metrics."""

from __future__ import annotations

from dataclasses import dataclass

from .coverage import equipment_count
from .expansion import expanded_spec
from .indexing import CoverageIndex, IndexedCoverage
from .models import Candidate, ParsedCsv, SolveOptions, SupportTable, Token
from .parsing import render_tokens


@dataclass(frozen=True)
class EvaluatedCandidate:
    candidate: Candidate
    spec: dict[str, tuple[Token, ...]]
    coverage: IndexedCoverage
    active_signature: str
    output_signature: str


@dataclass(frozen=True)
class AssignedCandidate:
    evaluated: EvaluatedCandidate
    assigned_indexes: tuple[int, ...]
    assigned_excess: int

    @property
    def assigned_count(self) -> int:
        return len(self.assigned_indexes)


@dataclass(frozen=True)
class SolutionEvaluation:
    rows: tuple[AssignedCandidate, ...]
    total_equipment: int
    total_assignment_excess: int
    low_use_spec_count: int
    low_use_deficit: int

    @property
    def selected_spec_count(self) -> int:
        return len(self.rows)

    def objective(self) -> tuple[int, int, int, int, int, tuple[str, ...]]:
        return (
            self.total_equipment,
            self.total_assignment_excess,
            self.low_use_spec_count,
            self.low_use_deficit,
            self.selected_spec_count,
            tuple(sorted(row.evaluated.output_signature for row in self.rows)),
        )


class SolutionEvaluator:
    def __init__(self, parsed: ParsedCsv, support: SupportTable, options: SolveOptions):
        self.parsed = parsed
        self.support = support
        self.options = options
        self.output_columns = tuple(column for column in parsed.columns if column != "tc_id")
        self.coverage_index = CoverageIndex.build(parsed, support, options)
        self._candidate_cache: dict[str, EvaluatedCandidate] = {}

    def evaluate(self, candidates: tuple[Candidate, ...]) -> SolutionEvaluation:
        evaluated_rows = tuple(self._evaluate_candidate(candidate) for candidate in candidates)
        self._validate_coverage(evaluated_rows)
        assigned_indexes, assigned_excess = self._assign_rows(evaluated_rows)

        rows = tuple(
            AssignedCandidate(
                evaluated=evaluated,
                assigned_indexes=tuple(assigned_indexes[index]),
                assigned_excess=assigned_excess[index],
            )
            for index, evaluated in enumerate(evaluated_rows)
        )
        threshold = self.options.min_assigned_cases_per_spec
        if threshold > 0:
            low_use_deficits = [max(0, threshold - row.assigned_count) for row in rows]
        else:
            low_use_deficits = [0 for _row in rows]
        return SolutionEvaluation(
            rows=rows,
            total_equipment=sum(equipment_count(row.evaluated.spec) for row in rows),
            total_assignment_excess=sum(row.assigned_excess for row in rows),
            low_use_spec_count=sum(1 for deficit in low_use_deficits if deficit),
            low_use_deficit=sum(low_use_deficits),
        )

    def _evaluate_candidate(self, candidate: Candidate) -> EvaluatedCandidate:
        cached = self._candidate_cache.get(candidate.signature)
        if cached is not None:
            return cached
        spec = expanded_spec(candidate.spec, self.support)
        evaluated = EvaluatedCandidate(
            candidate=candidate,
            spec=spec,
            coverage=self.coverage_index.coverage_for_spec(spec),
            active_signature=_rendered_signature(spec, self.coverage_index.columns),
            output_signature=_rendered_signature(spec, self.output_columns),
        )
        self._candidate_cache[candidate.signature] = evaluated
        return evaluated

    def _validate_coverage(self, rows: tuple[EvaluatedCandidate, ...]) -> None:
        covered = set()
        for row in rows:
            covered.update(row.coverage.row_indexes)
        expected = set(range(len(self.parsed.rows)))
        if covered != expected:
            missing = sorted(expected - covered)
            raise ValueError(f"expanded solution does not cover testcase indexes: {missing}")

    def _assign_rows(self, rows: tuple[EvaluatedCandidate, ...]) -> tuple[list[list[int]], list[int]]:
        assigned_indexes: list[list[int]] = [[] for _row in rows]
        assigned_excess = [0 for _row in rows]
        assignment_counts = [0 for _row in self.parsed.rows]

        for testcase_index, _row in enumerate(self.parsed.rows):
            group_index = self.coverage_index.row_to_group[testcase_index]
            choices = []
            for selected_index, evaluated in enumerate(rows):
                if not evaluated.coverage.group_mask & (1 << group_index):
                    continue
                choices.append(
                    (
                        evaluated.coverage.excess_by_group.get(group_index, 0),
                        equipment_count(evaluated.spec),
                        evaluated.active_signature,
                        selected_index,
                    )
                )
            if not choices:
                continue
            excess, _equipment, _signature, selected_index = min(choices)
            assigned_indexes[selected_index].append(testcase_index)
            assigned_excess[selected_index] += excess
            assignment_counts[testcase_index] += 1

        bad_indexes = [index for index, count in enumerate(assignment_counts) if count != 1]
        if bad_indexes:
            raise ValueError(f"expanded solution does not assign testcase indexes exactly once: {bad_indexes}")
        return assigned_indexes, assigned_excess


def _rendered_signature(spec: dict[str, tuple[Token, ...]], columns: tuple[str, ...]) -> str:
    return "|".join(f"{column}={render_tokens(spec.get(column, ())) }" for column in columns)
