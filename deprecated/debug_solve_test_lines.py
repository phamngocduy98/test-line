#!/usr/bin/env python3
"""Debug event tracer for solve_test_lines.py.

This companion CLI preserves the solver's normal CSV output while writing a
JSONL event trace for solver phases, decisions, state snapshots, and the first
emitted output spec.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from solve_test_lines import (
    DEFAULT_MAX_COMPATIBILITY_VARIANTS,
    SINGLE_SELECT_COLUMNS,
    Candidate,
    RuBandSupport,
    TestCase,
    assign_cases_equally,
    coarse_signature,
    coverage_delta,
    covers_column,
    equipment_count,
    is_temporarily_ignored_column,
    load_cases,
    load_ru_band_support,
    merge_cases,
    render_cell,
    resolve_compatibility_variants,
    single_select_key,
    sliding_window_starts,
    spec_has_compatible_ru_bands,
    spec_signature,
    validate_solution,
    validate_support_references,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run solve_test_lines.py with structured debug event tracing."
    )
    parser.add_argument("--input", default="input.csv", help="Input testcase CSV path.")
    parser.add_argument(
        "--output", default="output_specs.csv", help="Output specs CSV path."
    )
    parser.add_argument(
        "--ru-band-support",
        required=True,
        help="Required CSV mapping RUs to supported LTE and NR bands.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="OR-Tools solve timeout in seconds. Default: 600.",
    )
    parser.add_argument(
        "--max-candidates-per-bucket",
        type=int,
        default=250,
        help="Candidate cap per compatible bucket. Default: 250.",
    )
    parser.add_argument(
        "--max-cover-checks-per-candidate",
        type=int,
        default=0,
        help=(
            "Limit coverage checks per candidate for very large files. "
            "0 means check all rows. Exact-row candidates always cover themselves."
        ),
    )
    parser.add_argument(
        "--ignore-tech-and-ue-capa",
        action="store_true",
        help=(
            "Ignore all tech and ue capa columns during optimization. "
            "Ignored columns are blank in the output."
        ),
    )
    parser.add_argument(
        "--auto-assign",
        action="store_true",
        help="Assign testcases as evenly as possible across selected specs.",
    )
    parser.add_argument(
        "--debug-log",
        default="debug_solve_test_lines.jsonl",
        help="Structured JSONL debug event log path.",
    )
    parser.add_argument(
        "--trace-output-spec",
        default="spec_1",
        help="Output spec_id to trace deeply. Default: spec_1.",
    )
    return parser.parse_args(argv)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(_jsonable(key)): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=repr)]
    return str(value)


def spec_to_json(spec: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    return {column: list(tokens) for column, tokens in spec.items()}


def signature_to_json(
    signature: tuple[tuple[str, tuple[str, ...]], ...],
) -> list[dict[str, Any]]:
    return [
        {"column": column, "tokens": list(tokens)}
        for column, tokens in signature
    ]


def bucket_key_to_json(bucket_key: tuple[Any, ...] | None) -> list[dict[str, Any]]:
    if bucket_key is None:
        return []
    return [
        {"column": column, "tokens": list(tokens)}
        for column, tokens in bucket_key
    ]


def case_ids(cases: list[TestCase], indices: Iterable[int]) -> list[str]:
    return [cases[index].tc_id for index in indices]


class EventLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started_at = time.monotonic()
        self.sequence = 0
        self._handle = path.open("w", encoding="utf-8")

    def emit(
        self,
        event: str,
        phase: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.sequence += 1
        record: dict[str, Any] = {
            "seq": self.sequence,
            "elapsed_ms": round((time.monotonic() - self.started_at) * 1000, 3),
            "event": event,
            "phase": phase,
            "level": level,
            "data": _jsonable(data or {}),
        }
        if state is not None:
            record["state"] = _jsonable(state)
        self._handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


class DebugContext:
    def __init__(self, logger: EventLogger) -> None:
        self.logger = logger
        self.phase = "init"
        self.state: dict[str, Any] = {}
        self.current_bucket_key: tuple[Any, ...] | None = None
        self.current_bucket_signatures: set[
            tuple[tuple[str, tuple[str, ...]], ...]
        ] = set()
        self.latest_signature: tuple[tuple[str, tuple[str, ...]], ...] | None = None
        self.generation_counter = 0
        self.candidate_debug: dict[
            tuple[tuple[str, tuple[str, ...]], ...],
            dict[str, Any],
        ] = {}
        self.candidate_index_by_signature: dict[
            tuple[tuple[str, tuple[str, ...]], ...],
            int,
        ] = {}

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def update_state(self, **items: Any) -> None:
        self.state.update(items)

    def next_generation_id(self) -> int:
        self.generation_counter += 1
        return self.generation_counter

    def snapshot(self, **extra: Any) -> dict[str, Any]:
        state = dict(self.state)
        state["phase"] = self.phase
        if self.current_bucket_key is not None:
            state["current_bucket_key"] = bucket_key_to_json(self.current_bucket_key)
        if self.current_bucket_signatures:
            signatures = sorted(self.current_bucket_signatures, key=repr)
            state["generated_signature_count"] = len(signatures)
            state["generated_signatures"] = [
                signature_to_json(signature) for signature in signatures[:10]
            ]
            state["generated_signatures_truncated"] = len(signatures) > 10
        if self.latest_signature is not None:
            state["latest_signature"] = signature_to_json(self.latest_signature)
        state.update(extra)
        return state

    def emit(
        self,
        event: str,
        *,
        phase: str | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.logger.emit(
            event,
            phase or self.phase,
            level=level,
            data=data,
            state=state,
        )

    def decision(
        self,
        event: str,
        *,
        data: dict[str, Any] | None = None,
        **state: Any,
    ) -> None:
        self.emit(event, data=data, state=self.snapshot(**state))


def remember_candidate_generation(
    ctx: DebugContext,
    candidate: Candidate,
    generation: dict[str, Any],
) -> None:
    entry = ctx.candidate_debug.setdefault(
        candidate.signature,
        {
            "signature": signature_to_json(candidate.signature),
            "generations": [],
        },
    )
    entry["generations"].append(generation)
    entry["generation_count"] = len(entry["generations"])


def debug_build_candidate_variants(
    requirement_columns: list[str],
    cases: list[TestCase],
    indices: Iterable[int],
    all_cases: list[TestCase],
    max_cover_checks: int,
    ctx: DebugContext,
    source: dict[str, Any],
    enforce_delta: bool = True,
    support: RuBandSupport | None = None,
    max_compatibility_variants: int = DEFAULT_MAX_COMPATIBILITY_VARIANTS,
) -> list[Candidate]:
    index_tuple = tuple(sorted(set(indices)))
    if not index_tuple:
        ctx.decision(
            "decision.candidate_rejected",
            data={"reason": "empty_seed", "source": source},
        )
        return []

    source_with_seed = {
        **source,
        "seed_indices": list(index_tuple),
        "seed_tc_ids": case_ids(cases, index_tuple),
    }
    merged_spec = merge_cases(requirement_columns, (cases[index] for index in index_tuple))
    if merged_spec is None:
        ctx.decision(
            "decision.candidate_rejected",
            data={"reason": "merge_failed", "source": source_with_seed},
        )
        return []

    ctx.emit(
        "candidate.merged",
        data={
            "source": source_with_seed,
            "merged_spec": spec_to_json(merged_spec),
        },
        state=ctx.snapshot(),
    )

    specs = (
        resolve_compatibility_variants(
            merged_spec, support, max_variants=max_compatibility_variants
        )
        if support is not None
        else [merged_spec]
    )
    if not specs:
        ctx.decision(
            "decision.compatibility_variant_rejected",
            data={
                "reason": "no_compatible_variants",
                "source": source_with_seed,
                "merged_spec": spec_to_json(merged_spec),
            },
        )
        return []

    check_cases = all_cases
    if max_cover_checks > 0 and len(all_cases) > max_cover_checks:
        seed = list(index_tuple)
        seed_set = set(seed)
        others = [case.index for case in all_cases if case.index not in seed_set]
        selected = seed + others[: max(0, max_cover_checks - len(seed))]
        check_cases = [all_cases[index] for index in selected]

    candidates: list[Candidate] = []
    for variant_index, spec in enumerate(specs, start=1):
        generation_id = ctx.next_generation_id()
        variant_data = {
            "generation_id": generation_id,
            "source": source_with_seed,
            "variant_index": variant_index,
            "variant_count": len(specs),
            "merged_spec": spec_to_json(merged_spec),
            "resolved_spec": spec_to_json(spec),
        }

        if support is not None and not spec_has_compatible_ru_bands(spec, support):
            ctx.decision(
                "decision.compatibility_variant_rejected",
                data={**variant_data, "reason": "ru_band_incompatible"},
            )
            continue

        if support is not None:
            ctx.decision(
                "decision.compatibility_variant_chosen",
                data=variant_data,
            )

        covered: list[int] = []
        for case in check_cases:
            ok, _ = coverage_delta(
                requirement_columns,
                spec,
                case,
                enforce_delta=enforce_delta,
            )
            if ok:
                covered.append(case.index)

        seed_is_covered = True
        failed_seed_index: int | None = None
        for index in index_tuple:
            if index in covered:
                continue
            ok, _ = coverage_delta(
                requirement_columns,
                spec,
                all_cases[index],
                enforce_delta=enforce_delta,
            )
            if not ok:
                seed_is_covered = False
                failed_seed_index = index
                break
            covered.append(index)
        if not seed_is_covered:
            ctx.decision(
                "decision.candidate_rejected",
                data={
                    **variant_data,
                    "reason": "seed_not_covered",
                    "failed_seed_index": failed_seed_index,
                    "failed_seed_tc_id": (
                        all_cases[failed_seed_index].tc_id
                        if failed_seed_index is not None
                        else None
                    ),
                    "covered_count": len(covered),
                },
            )
            continue

        signature = spec_signature(spec)
        candidate = Candidate(
            spec=spec,
            covered=tuple(covered),
            equipment_count=equipment_count(requirement_columns, spec),
            signature=signature,
        )
        generation = {
            **variant_data,
            "signature": signature_to_json(signature),
            "covered_indices": list(candidate.covered),
            "covered_tc_ids": case_ids(all_cases, candidate.covered),
            "covered_count": len(candidate.covered),
            "equipment_count": candidate.equipment_count,
        }
        remember_candidate_generation(ctx, candidate, generation)
        ctx.emit(
            "candidate.built",
            data=generation,
            state=ctx.snapshot(),
        )
        candidates.append(candidate)
    return candidates


def debug_add_candidate(
    candidates: dict[tuple[tuple[str, tuple[str, ...]], ...], Candidate],
    candidate: Candidate | None,
    ctx: DebugContext,
) -> None:
    if candidate is None:
        return

    current = candidates.get(candidate.signature)
    previous_count = len(candidates)
    ctx.latest_signature = candidate.signature
    if current is None:
        candidates[candidate.signature] = candidate
        ctx.update_state(candidate_count=len(candidates))
        ctx.decision(
            "decision.candidate_accepted",
            data={
                "signature": signature_to_json(candidate.signature),
                "covered_count": len(candidate.covered),
                "equipment_count": candidate.equipment_count,
                "candidate_count_before": previous_count,
                "candidate_count_after": len(candidates),
            },
            candidate_count=len(candidates),
        )
        return

    covered = tuple(sorted(set(current.covered) | set(candidate.covered)))
    candidates[candidate.signature] = Candidate(
        spec=current.spec,
        covered=covered,
        equipment_count=current.equipment_count,
        signature=current.signature,
    )
    ctx.update_state(candidate_count=len(candidates))
    ctx.decision(
        "decision.duplicate_candidate_merged",
        data={
            "signature": signature_to_json(candidate.signature),
            "previous_covered_count": len(current.covered),
            "incoming_covered_count": len(candidate.covered),
            "merged_covered_count": len(covered),
            "candidate_count": len(candidates),
        },
        candidate_count=len(candidates),
    )


def debug_generate_candidates(
    requirement_columns: list[str],
    cases: list[TestCase],
    max_candidates_per_bucket: int,
    max_cover_checks: int,
    ctx: DebugContext,
    support: RuBandSupport | None = None,
) -> list[Candidate]:
    ctx.set_phase("candidate_generation")
    ctx.emit(
        "candidate_generation.started",
        data={
            "testcase_count": len(cases),
            "requirement_columns": requirement_columns,
            "max_candidates_per_bucket": max_candidates_per_bucket,
            "max_cover_checks": max_cover_checks,
        },
        state=ctx.snapshot(candidate_count=0),
    )
    candidates: dict[tuple[tuple[str, tuple[str, ...]], ...], Candidate] = {}
    ctx.update_state(candidate_count=0)

    for case in cases:
        ctx.update_state(current_source="exact", current_case_index=case.index)
        exact_variants = debug_build_candidate_variants(
            requirement_columns,
            cases,
            [case.index],
            cases,
            max_cover_checks,
            ctx,
            source={"kind": "exact", "case_index": case.index, "tc_id": case.tc_id},
            support=support,
            max_compatibility_variants=1,
        )
        if support is not None and not exact_variants:
            raise SystemExit(
                f"testcase {case.tc_id} has no compatible RU-band realization"
            )
        for candidate in exact_variants:
            debug_add_candidate(candidates, candidate, ctx)

    bucket_map: dict[tuple[Any, ...], list[TestCase]] = defaultdict(list)
    for case in cases:
        bucket_map[single_select_key(case)].append(case)
    ctx.update_state(bucket_count=len(bucket_map))
    ctx.emit(
        "candidate_generation.buckets_created",
        data={"bucket_count": len(bucket_map)},
        state=ctx.snapshot(candidate_count=len(candidates)),
    )

    for bucket_position, bucket_cases in enumerate(bucket_map.values(), start=1):
        bucket_key = single_select_key(bucket_cases[0])
        bucket_signatures: set[
            tuple[tuple[str, tuple[str, ...]], ...]
        ] = set()
        ctx.current_bucket_key = bucket_key
        ctx.current_bucket_signatures = bucket_signatures
        ctx.update_state(
            current_bucket_position=bucket_position,
            current_bucket_size=len(bucket_cases),
            current_bucket_candidate_count=0,
        )

        def add_bucket_variants(
            indices: Iterable[int],
            source_kind: str,
            extra_source: dict[str, Any] | None = None,
        ) -> None:
            remaining = max_candidates_per_bucket - len(bucket_signatures)
            source = {
                "kind": source_kind,
                "bucket_position": bucket_position,
                "bucket_key": bucket_key_to_json(bucket_key),
            }
            if extra_source:
                source.update(extra_source)
            if remaining <= 0:
                ctx.decision(
                    "decision.bucket_cap_reached",
                    data={
                        "max_candidates_per_bucket": max_candidates_per_bucket,
                        "source": source,
                    },
                    current_bucket_candidate_count=len(bucket_signatures),
                )
                return
            variants = debug_build_candidate_variants(
                requirement_columns,
                cases,
                indices,
                cases,
                max_cover_checks,
                ctx,
                source=source,
                support=support,
                max_compatibility_variants=min(4, remaining),
            )
            for candidate in variants:
                bucket_signatures.add(candidate.signature)
                ctx.update_state(
                    current_bucket_candidate_count=len(bucket_signatures)
                )
                debug_add_candidate(candidates, candidate, ctx)

        sorted_bucket = sorted(
            bucket_cases,
            key=lambda case: (
                sum(len(case.tokens[column]) for column in requirement_columns),
                case.index,
            ),
        )
        ctx.emit(
            "candidate_generation.bucket_started",
            data={
                "bucket_position": bucket_position,
                "bucket_size": len(sorted_bucket),
                "bucket_key": bucket_key_to_json(bucket_key),
            },
            state=ctx.snapshot(candidate_count=len(candidates)),
        )
        if len(sorted_bucket) > 1:
            add_bucket_variants(
                (case.index for case in sorted_bucket),
                "bucket_all",
            )

        for window_size in (2, 3, 5, 8, 13, 21, 34, 55):
            if (
                window_size > len(sorted_bucket)
                or len(bucket_signatures) >= max_candidates_per_bucket
            ):
                continue
            made = 0
            for start in sliding_window_starts(len(sorted_bucket), window_size):
                add_bucket_variants(
                    (case.index for case in sorted_bucket[start : start + window_size]),
                    "sliding_window",
                    {"window_size": window_size, "window_start": start},
                )
                made += 1
                if (
                    made >= max_candidates_per_bucket
                    or len(bucket_signatures) >= max_candidates_per_bucket
                ):
                    ctx.decision(
                        "decision.bucket_cap_reached",
                        data={
                            "max_candidates_per_bucket": max_candidates_per_bucket,
                            "window_size": window_size,
                            "made": made,
                        },
                        current_bucket_candidate_count=len(bucket_signatures),
                    )
                    break

        signature_groups: dict[tuple[Any, ...], list[TestCase]] = defaultdict(list)
        for case in sorted_bucket:
            signature_groups[
                coarse_signature(requirement_columns, case, include_equipment=False)
            ].append(case)
            signature_groups[
                coarse_signature(requirement_columns, case, include_equipment=True)
            ].append(case)

        ranked_groups = sorted(
            signature_groups.values(),
            key=lambda group: (-len(group), min(case.index for case in group)),
        )
        for group in ranked_groups[:max_candidates_per_bucket]:
            if len(bucket_signatures) >= max_candidates_per_bucket:
                ctx.decision(
                    "decision.bucket_cap_reached",
                    data={"max_candidates_per_bucket": max_candidates_per_bucket},
                    current_bucket_candidate_count=len(bucket_signatures),
                )
                break
            if len(group) > 1:
                add_bucket_variants(
                    (case.index for case in group),
                    "coarse_signature_group",
                    {
                        "group_size": len(group),
                        "group_tc_ids": [case.tc_id for case in group],
                    },
                )

        ctx.emit(
            "candidate_generation.bucket_completed",
            data={
                "bucket_position": bucket_position,
                "bucket_generated_signatures": len(bucket_signatures),
                "candidate_count": len(candidates),
            },
            state=ctx.snapshot(candidate_count=len(candidates)),
        )

    sorted_candidates = sorted(
        candidates.values(),
        key=lambda candidate: (
            candidate.equipment_count,
            -len(candidate.covered),
            candidate.signature,
        ),
    )
    ctx.candidate_index_by_signature = {
        candidate.signature: index for index, candidate in enumerate(sorted_candidates)
    }
    ctx.current_bucket_key = None
    ctx.current_bucket_signatures = set()
    ctx.update_state(candidate_count=len(sorted_candidates))
    ctx.emit(
        "candidate_generation.completed",
        data={"candidate_count": len(sorted_candidates)},
        state=ctx.snapshot(candidate_count=len(sorted_candidates)),
    )
    return sorted_candidates


def debug_greedy_set_cover_hint(
    candidates: list[Candidate],
    cases: list[TestCase],
    ctx: DebugContext,
) -> set[int]:
    uncovered = {case.index for case in cases}
    hinted_selected: set[int] = set()

    while uncovered:
        best_index = min(
            range(len(candidates)),
            key=lambda index: (
                -len(uncovered.intersection(candidates[index].covered)),
                candidates[index].equipment_count,
                index,
            ),
        )
        newly_covered = uncovered.intersection(candidates[best_index].covered)
        if not newly_covered:
            missing_ids = [
                case.tc_id for case in cases if case.index in uncovered
            ]
            raise SystemExit(
                "no candidate covers testcases: " + ", ".join(missing_ids)
            )
        hinted_selected.add(best_index)
        uncovered.difference_update(newly_covered)
        ctx.decision(
            "decision.greedy_hint_candidate_selected",
            data={
                "candidate_index": best_index,
                "newly_covered_indices": sorted(newly_covered),
                "newly_covered_tc_ids": case_ids(cases, sorted(newly_covered)),
                "remaining_uncovered_count": len(uncovered),
                "equipment_count": candidates[best_index].equipment_count,
            },
            selected_candidate_indices=sorted(hinted_selected),
            uncovered_testcase_count=len(uncovered),
        )

    return hinted_selected


def debug_solve_with_ortools(
    candidates: list[Candidate],
    cases: list[TestCase],
    timeout_seconds: float,
    ctx: DebugContext,
) -> tuple[str, list[int]]:
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        raise SystemExit("Missing dependency: pip install ortools") from None

    ctx.set_phase("solve")
    model = cp_model.CpModel()
    num_candidates = len(candidates)

    selected = [model.NewBoolVar(f"selected_{j}") for j in range(num_candidates)]
    coverers: dict[int, list[int]] = defaultdict(list)

    for j, candidate in enumerate(candidates):
        for case_index in candidate.covered:
            coverers[case_index].append(j)

    for case in cases:
        if not coverers[case.index]:
            raise SystemExit(f"no candidate covers testcase {case.tc_id}")
        model.Add(sum(selected[j] for j in coverers[case.index]) >= 1)

    ctx.emit(
        "solve.model_built",
        data={
            "candidate_count": num_candidates,
            "testcase_count": len(cases),
            "coverage_constraints": len(cases),
        },
        state=ctx.snapshot(candidate_count=num_candidates),
    )

    hinted_selected = debug_greedy_set_cover_hint(candidates, cases, ctx)
    for j, var in enumerate(selected):
        model.AddHint(var, int(j in hinted_selected))

    max_equipment = model.NewIntVar(
        0, max(c.equipment_count for c in candidates), "max_equipment"
    )
    for j, candidate in enumerate(candidates):
        model.Add(max_equipment >= candidate.equipment_count * selected[j])

    total_equipment = sum(
        candidates[j].equipment_count * selected[j] for j in range(num_candidates)
    )
    selected_count = sum(selected)

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = max(1, min(8, os.cpu_count() or 8))
    started_at = time.monotonic()

    objectives = (
        ("selected_count", selected_count),
        ("max_equipment", max_equipment),
        ("total_equipment", total_equipment),
    )
    solve_status = "OPTIMAL"
    status = None
    best_selected_indices: list[int] | None = None
    for objective_name, objective in objectives:
        remaining = max(0.1, timeout_seconds - (time.monotonic() - started_at))
        solver.parameters.max_time_in_seconds = remaining
        model.Minimize(objective)
        ctx.emit(
            "solve.stage_started",
            data={"objective": objective_name, "remaining_timeout_seconds": remaining},
            state=ctx.snapshot(selected_candidate_indices=best_selected_indices or []),
        )
        status = solver.Solve(model)
        status_name = solver.StatusName(status)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            best_selected_indices = [
                j for j, var in enumerate(selected) if solver.BooleanValue(var)
            ]
        ctx.emit(
            "solve.stage_completed",
            data={
                "objective": objective_name,
                "status": status_name,
                "objective_value": (
                    int(solver.ObjectiveValue())
                    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
                    else None
                ),
                "selected_candidate_indices": best_selected_indices or [],
            },
            state=ctx.snapshot(selected_candidate_indices=best_selected_indices or []),
        )
        if status == cp_model.OPTIMAL:
            ctx.decision(
                "decision.ortools_stage_result_accepted",
                data={
                    "objective": objective_name,
                    "status": status_name,
                    "objective_value": int(solver.ObjectiveValue()),
                    "selected_candidate_indices": best_selected_indices or [],
                },
                selected_candidate_indices=best_selected_indices or [],
                solve_status=solve_status,
            )
            model.Add(objective == int(solver.ObjectiveValue()))
            continue
        if status == cp_model.FEASIBLE:
            solve_status = "FEASIBLE_TIMEOUT"
            ctx.decision(
                "decision.ortools_stage_result_accepted",
                data={
                    "objective": objective_name,
                    "status": status_name,
                    "solve_status": solve_status,
                    "selected_candidate_indices": best_selected_indices or [],
                },
                selected_candidate_indices=best_selected_indices or [],
                solve_status=solve_status,
            )
            break
        if status == cp_model.UNKNOWN and best_selected_indices is not None:
            solve_status = "FEASIBLE_TIMEOUT"
            ctx.decision(
                "decision.ortools_stage_result_accepted",
                data={
                    "objective": objective_name,
                    "status": status_name,
                    "solve_status": solve_status,
                    "selected_candidate_indices": best_selected_indices,
                },
                selected_candidate_indices=best_selected_indices,
                solve_status=solve_status,
            )
            break
        raise SystemExit("no feasible solution found")

    if best_selected_indices is None:
        raise SystemExit("no feasible solution found")

    ctx.update_state(
        selected_candidate_indices=best_selected_indices,
        solve_status=solve_status,
    )
    ctx.emit(
        "solve.completed",
        data={
            "solve_status": solve_status,
            "selected_candidate_indices": best_selected_indices,
        },
        state=ctx.snapshot(
            selected_candidate_indices=best_selected_indices,
            solve_status=solve_status,
        ),
    )
    return solve_status, best_selected_indices


def explain_coverage(
    requirement_columns: list[str],
    candidate_spec: dict[str, tuple[str, ...]],
    case: TestCase,
    *,
    enforce_delta: bool = True,
    support: RuBandSupport | None = None,
) -> dict[str, Any]:
    support_compatible = (
        spec_has_compatible_ru_bands(candidate_spec, support)
        if support is not None
        else True
    )
    columns: list[dict[str, Any]] = []
    total_delta = 0
    all_ok = support_compatible
    for column in requirement_columns:
        ok, delta = covers_column(
            column,
            candidate_spec[column],
            case.tokens[column],
            enforce_delta=enforce_delta,
        )
        if not ok:
            all_ok = False
        total_delta += delta
        columns.append(
            {
                "column": column,
                "ok": ok,
                "delta": delta,
                "spec_tokens": list(candidate_spec[column]),
                "case_tokens": list(case.tokens[column]),
            }
        )
    return {
        "ok": all_ok,
        "support_compatible": support_compatible,
        "total_delta": total_delta if all_ok else 0,
        "columns": columns,
    }


def spec_number_from_id(spec_id: str) -> int | None:
    prefix = "spec_"
    if not spec_id.startswith(prefix):
        return None
    try:
        value = int(spec_id[len(prefix) :])
    except ValueError:
        return None
    return value if value >= 1 else None


def emit_traced_spec_events(
    ctx: DebugContext,
    *,
    spec_id: str,
    candidate_index: int,
    candidate: Candidate,
    row: dict[str, Any],
    requirement_columns: list[str],
    cases: list[TestCase],
    support: RuBandSupport | None,
) -> None:
    metadata = ctx.candidate_debug.get(candidate.signature, {})
    ctx.emit(
        "first_spec.ancestry",
        data={
            "spec_id": spec_id,
            "candidate_index": candidate_index,
            "signature": signature_to_json(candidate.signature),
            "candidate_spec": spec_to_json(candidate.spec),
            "covered_indices": list(candidate.covered),
            "covered_tc_ids": case_ids(cases, candidate.covered),
            "generation_count": metadata.get("generation_count", 0),
            "generations": metadata.get("generations", []),
        },
        state=ctx.snapshot(first_spec_candidate_index=candidate_index),
    )
    for case in cases:
        explanation = explain_coverage(
            requirement_columns,
            candidate.spec,
            case,
            support=support,
        )
        ctx.emit(
            "first_spec.coverage_check",
            data={
                "spec_id": spec_id,
                "candidate_index": candidate_index,
                "case_index": case.index,
                "tc_id": case.tc_id,
                **explanation,
            },
            state=ctx.snapshot(first_spec_candidate_index=candidate_index),
        )
    ctx.emit(
        "output.first_spec",
        data={
            "spec_id": spec_id,
            "candidate_index": candidate_index,
            "row": row,
        },
        state=ctx.snapshot(first_spec_candidate_index=candidate_index),
    )


def debug_write_output(
    path: Path,
    input_columns: list[str],
    requirement_columns: list[str],
    cases: list[TestCase],
    candidates: list[Candidate],
    selected_indices: list[int],
    solve_status: str,
    ctx: DebugContext,
    *,
    auto_assign: bool = False,
    enforce_delta: bool = True,
    support: RuBandSupport | None = None,
    trace_output_spec: str = "spec_1",
) -> tuple[int, int, int]:
    del enforce_delta

    ctx.set_phase("output")
    output_columns = ["spec_id"]
    if auto_assign:
        output_columns.extend(["assigned_tc_ids", "assigned_count"])
    output_columns.extend(
        [
            "covered_tc_ids",
            "covered_count",
            "equipment_count",
            "solve_status",
        ]
    )
    output_columns.extend(column for column in input_columns if column != "tc_id")

    selected_sorted = sorted(
        selected_indices,
        key=lambda index: (
            candidates[index].equipment_count,
            -len(candidates[index].covered),
            min(candidates[index].covered),
        ),
    )
    first_spec_candidate_index = selected_sorted[0] if selected_sorted else None
    ctx.update_state(
        selected_candidate_indices=selected_indices,
        selected_spec_count=len(selected_indices),
        first_spec_candidate_index=first_spec_candidate_index,
    )
    ctx.decision(
        "decision.output_sorted",
        data={
            "selected_candidate_indices": selected_indices,
            "output_candidate_order": selected_sorted,
            "first_spec_candidate_index": first_spec_candidate_index,
        },
        selected_candidate_indices=selected_indices,
        selected_spec_count=len(selected_indices),
        first_spec_candidate_index=first_spec_candidate_index,
    )

    assigned_by_spec: dict[int, list[int]] = {}
    if auto_assign:
        assigned_by_spec = assign_cases_equally(
            [candidates[index].covered for index in selected_sorted],
            cases,
        )

    target_spec_number = spec_number_from_id(trace_output_spec) or 1
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_columns)
        writer.writeheader()
        for spec_number, candidate_index in enumerate(selected_sorted, start=1):
            candidate = candidates[candidate_index]
            covered_ids = [cases[index].tc_id for index in candidate.covered]

            row: dict[str, Any] = {
                "spec_id": f"spec_{spec_number}",
                "covered_tc_ids": " + ".join(covered_ids),
                "covered_count": len(covered_ids),
                "equipment_count": candidate.equipment_count,
                "solve_status": solve_status,
            }
            if auto_assign:
                assigned = assigned_by_spec[spec_number - 1]
                row["assigned_tc_ids"] = " + ".join(
                    cases[index].tc_id for index in assigned
                )
                row["assigned_count"] = len(assigned)
            for column in requirement_columns:
                row[column] = render_cell(candidate.spec[column])

            if spec_number == target_spec_number:
                ctx.decision(
                    "decision.first_output_spec_identified",
                    data={
                        "spec_id": row["spec_id"],
                        "candidate_index": candidate_index,
                        "signature": signature_to_json(candidate.signature),
                    },
                    first_spec_candidate_index=candidate_index,
                    selected_candidate_indices=selected_indices,
                )
                emit_traced_spec_events(
                    ctx,
                    spec_id=row["spec_id"],
                    candidate_index=candidate_index,
                    candidate=candidate,
                    row=row,
                    requirement_columns=requirement_columns,
                    cases=cases,
                    support=support,
                )

            writer.writerow(row)

    selected_candidates = [candidates[index] for index in selected_indices]
    max_equipment = max(candidate.equipment_count for candidate in selected_candidates)
    total_equipment = sum(candidate.equipment_count for candidate in selected_candidates)
    ctx.update_state(
        selected_spec_count=len(selected_indices),
        max_equipment=max_equipment,
        total_equipment=total_equipment,
    )
    ctx.emit(
        "output.completed",
        data={
            "output": str(path),
            "selected_specs": len(selected_indices),
            "max_equipment": max_equipment,
            "total_equipment": total_equipment,
        },
        state=ctx.snapshot(
            selected_spec_count=len(selected_indices),
            max_equipment=max_equipment,
            total_equipment=total_equipment,
        ),
    )
    return len(selected_indices), max_equipment, total_equipment


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = EventLogger(Path(args.debug_log))
    ctx = DebugContext(logger)
    started_at = time.monotonic()

    try:
        ctx.emit(
            "run.started",
            data={"args": vars(args)},
            state=ctx.snapshot(),
        )
        input_path = Path(args.input)
        output_path = Path(args.output)
        support_path = Path(args.ru_band_support)

        ctx.set_phase("input")
        input_columns, cases = load_cases(input_path)
        ctx.update_state(
            input=str(input_path),
            input_testcases=len(cases),
            input_columns=input_columns,
        )
        ctx.emit(
            "input.loaded",
            data={
                "path": str(input_path),
                "columns": input_columns,
                "testcase_count": len(cases),
            },
            state=ctx.snapshot(),
        )

        ctx.set_phase("support")
        support = load_ru_band_support(support_path)
        ctx.emit(
            "support.loaded",
            data={
                "path": str(support_path),
                "ru_count": len(support.ru_names),
                "lte_band_count": len(support.lte_band_names),
                "nr_band_count": len(support.nr_band_names),
            },
            state=ctx.snapshot(),
        )

        ctx.set_phase("requirements")
        requirement_columns = [column for column in input_columns if column != "tc_id"]
        ignored_columns: list[str] = []
        if args.ignore_tech_and_ue_capa:
            ignored_columns = [
                column
                for column in requirement_columns
                if is_temporarily_ignored_column(column)
            ]
            requirement_columns = [
                column
                for column in requirement_columns
                if not is_temporarily_ignored_column(column)
            ]
        ctx.update_state(
            requirement_columns=requirement_columns,
            requirement_column_count=len(requirement_columns),
            ignored_columns=ignored_columns,
        )
        ctx.emit(
            "requirements.selected",
            data={
                "requirement_columns": requirement_columns,
                "ignored_columns": ignored_columns,
            },
            state=ctx.snapshot(),
        )

        ctx.set_phase("validation")
        validate_support_references(requirement_columns, cases, support)
        ctx.emit(
            "validation.support_references_completed",
            data={"status": "ok"},
            state=ctx.snapshot(),
        )

        candidates = debug_generate_candidates(
            requirement_columns=requirement_columns,
            cases=cases,
            max_candidates_per_bucket=max(1, args.max_candidates_per_bucket),
            max_cover_checks=max(0, args.max_cover_checks_per_candidate),
            ctx=ctx,
            support=support,
        )
        if not candidates:
            raise SystemExit("no candidate specs generated")

        solve_status, selected_indices = debug_solve_with_ortools(
            candidates,
            cases,
            args.timeout,
            ctx,
        )
        ctx.set_phase("validation")
        ctx.emit(
            "validation.solution_started",
            data={
                "selected_candidate_indices": selected_indices,
                "solve_status": solve_status,
            },
            state=ctx.snapshot(selected_candidate_indices=selected_indices),
        )
        validate_solution(
            requirement_columns,
            cases,
            candidates,
            selected_indices,
            support=support,
        )
        ctx.emit(
            "validation.solution_completed",
            data={"status": "ok"},
            state=ctx.snapshot(selected_candidate_indices=selected_indices),
        )

        spec_count, max_equipment, total_equipment = debug_write_output(
            output_path,
            input_columns,
            requirement_columns,
            cases,
            candidates,
            selected_indices,
            solve_status,
            ctx,
            auto_assign=args.auto_assign,
            support=support,
            trace_output_spec=args.trace_output_spec,
        )

        elapsed = time.monotonic() - started_at
        print(f"status={solve_status}")
        print(f"runtime_seconds={elapsed:.2f}")
        print(f"input_testcases={len(cases)}")
        print(f"candidate_specs={len(candidates)}")
        print(f"selected_specs={spec_count}")
        print(f"max_equipment={max_equipment}")
        print(f"total_equipment={total_equipment}")
        print(f"output={output_path}")

        ctx.emit(
            "run.completed",
            data={
                "status": solve_status,
                "runtime_seconds": round(elapsed, 6),
                "input_testcases": len(cases),
                "candidate_specs": len(candidates),
                "selected_specs": spec_count,
                "max_equipment": max_equipment,
                "total_equipment": total_equipment,
                "output": str(output_path),
            },
            state=ctx.snapshot(
                solve_status=solve_status,
                selected_spec_count=spec_count,
                max_equipment=max_equipment,
                total_equipment=total_equipment,
            ),
        )
        return 0
    except BaseException as exc:
        ctx.emit(
            "run.exception",
            level="error",
            data={
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
            state=ctx.snapshot(),
        )
        raise
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
