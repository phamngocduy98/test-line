# AGENTS.md

## Project Overview

This repo is a Python CLI workspace for rebuilding a telecom test-line solver
from legacy behavior. The old implementation and tests live under
`deprecated/`; the extracted behavior contract lives in
`docs/REQUIREMENT.md`.

## Hard Constraints

- Preserve user work already in the tree; inspect `git status --short` before broad edits.
  Source: repo is mid-rewrite. Applies: every code or docs task. Expiry: never.
- Treat `docs/REQUIREMENT.md` as the behavior source of truth for solver work.
  Source: legacy solver requirements were extracted there. Applies: parser, solver,
  validation, output, and test changes. Expiry: when a newer requirements doc replaces it.
- Keep `AGENTS.md` short and routing-oriented.
  Source: Lecture 04 progressive-disclosure guidance. Applies: every update to this file.
  Expiry: never.
- Put detailed rules in focused docs, not in this file.
  Source: avoid instruction bloat and lost-in-the-middle failures. Applies: new
  architecture, testing, solver, and runbook guidance. Expiry: never.
- Do not edit generated caches or sample outputs unless the task explicitly requires it.
  Source: repo contains `__pycache__/` and generated CSV artifacts. Applies: all tasks.
  Expiry: when those artifacts are removed from the repo.

## Quick Start

- List files: `find . -maxdepth 3 -type f | sort`
- Check working tree: `git status --short`
- Run the scratch parser: `python3 solve_test_lines.py`
- Run legacy tests from the legacy directory: `cd deprecated && python3 -m unittest`
- Compile a Python file: `python3 -m py_compile path/to/file.py`

## Task Routing

- Solver behavior or compatibility: read `docs/REQUIREMENT.md`.
- Legacy implementation details: inspect `deprecated/solve_test_lines.py`.
- Legacy test expectations: inspect `deprecated/test_solve_test_lines.py`.
- Merge/output behavior from old tooling: inspect `deprecated/MERGE_DECISION.md` and
  `deprecated/merge_output_specs.py` only when that task touches merge tooling.
- Benchmark/random input behavior: inspect `deprecated/benchmark_random_inputs.py`
  only when generating or validating random fixtures.

## Working Guidelines

- Prefer small, verifiable slices. The current rewrite is expected to grow from
  input parsing toward full solver behavior.
- Keep parser semantics aligned with the requirements: `+` separates required slots,
  `/` separates alternatives, blank requirement cells mean `any`, and `any` is a
  wildcard token.
- Use standard-library Python unless a requirement explicitly depends on another package.
- When changing behavior, add or update tests close to the affected legacy behavior.
- For docs-only changes, a read-through is enough verification unless the docs include
  runnable commands.

## Done Criteria

- The requested files are updated.
- Relevant commands have run, or the reason they were not run is recorded.
- The final response names the changed files and the verification performed.

## Instruction Maintenance

- Before adding an instruction here, ask whether it belongs in a topic doc instead.
- Each new instruction should have a source, applicability condition, and expiry condition.
- Remove outdated instructions during nearby edits; stale guidance is worse than missing guidance.
