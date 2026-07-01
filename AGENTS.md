# AGENTS.md

## Project Overview

This repo is a standard-library Python CLI for solving telecom test-line specs.
The behavior contract lives in `docs/REQUIREMENT.md`; the implementation plan
lives in `docs/SOLVER_PLAN.md`. The CLI entrypoint is `solve_test_lines.py`,
with implementation modules under `test_line_solver/` and tests under `tests/`.

## Hard Constraints

- Treat `docs/REQUIREMENT.md` as the behavior source of truth for parser,
  validation, solver, compatibility, output, and tests. Source: extracted solver
  contract. Applies: behavior changes. Expiry: when superseded by a newer
  requirements doc.
- Keep this file short and routing-oriented; put detailed guidance in focused
  docs instead. Source: progressive-disclosure maintenance. Applies: every
  `AGENTS.md` update. Expiry: never.
- Do not edit generated caches or sample/generated CSV outputs unless the task
  explicitly requires it. Source: repo contains ignored `__pycache__/`, sample
  inputs, and generated output artifacts. Applies: all tasks. Expiry: when those
  artifacts are removed.

## Quick Start

- Run parser mode: `python3 solve_test_lines.py --parse-only`
- Run solver mode: `python3 solve_test_lines.py --output /tmp/test-line-output.csv`
- Run tests: `python3 -m unittest discover -s tests`
- Compile check: `python3 -m py_compile solve_test_lines.py test_line_solver/*.py tests/*.py`

## Task Routing

- Requirements and expected behavior: read `docs/REQUIREMENT.md`.
- Implementation strategy and public CLI options: read `docs/SOLVER_PLAN.md`.
- Parser and CSV shape: inspect `test_line_solver/parsing.py` and
  `tests/test_parser_validation.py`.
- RU-band validation/compatibility: inspect `test_line_solver/support.py`,
  `test_line_solver/validation.py`, `test_line_solver/coverage.py`, and
  `tests/test_domain_solver.py`.
- Candidate generation, optimization, and output: inspect
  `test_line_solver/candidates.py`, `test_line_solver/optimizer.py`,
  `test_line_solver/output.py`, and solver/domain tests.
- CLI behavior: inspect `test_line_solver/cli.py` and `tests/test_cli.py`.

## Working Guidelines

- Prefer small, verifiable slices and keep behavior aligned with the requirement
  doc before optimizing or refactoring.
- Use standard-library Python unless a requirement explicitly introduces another
  dependency.
- When changing behavior, add or update focused `unittest` coverage close to the
  affected module.
- For docs-only changes, a read-through is enough verification unless the docs
  include runnable commands.

## Done Criteria

- Requested files are updated and unrelated user changes are preserved.
- Relevant tests/checks have run, or the reason they were not run is recorded.
- The final response names changed files and verification performed.
