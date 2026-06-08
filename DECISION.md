# Implementation Decisions

This document records the main implementation decisions for the test line spec
optimizer and random benchmark generator.

## Solver File

The main program is `solve_test_lines.py`.

It reads testcase requirements from `input.csv` by default and writes selected
test line specs to `output_specs.csv`. The script is intentionally standalone:
all project-specific rules live in this file so it can be run directly from the
project folder.

## CSV Parsing

Cells are parsed as token lists separated by spaced plus separators:

```text
a + b
```

The solver uses a regex separator equivalent to `space + plus + space`, so
values such as `s23+` remain a single token. This was chosen because telecom
capability names can contain `+`, while requirement combinations in the input
use spaces around `+`.

Blank cells mean no requirement. The literal value `null` is treated as a real
requirement value.

## `any` Handling

`any` is treated as a cardinality placeholder, not a literal concrete value.

Examples:

- `any` requires one matching slot.
- `any + any` requires two matching slots.
- `any + volte` requires one arbitrary slot and one concrete `volte` slot.

When generating a merged spec, the solver keeps `any` only when there are not
enough concrete values to satisfy the required number of slots.

## Delta Rule

For a spec to cover a testcase, each non-empty column may add at most one extra
token beyond that testcase requirement.

This keeps specs from becoming broad catch-all lines while still allowing useful
merges. The delta is calculated per column, then summed for optimization and
reporting.

## Single-Select Columns

`cc location` and `ca type` are single-select columns.

A generated spec may contain at most one concrete value in each of these
columns. If two testcases require conflicting concrete values in one of these
columns, they cannot be merged into the same candidate spec. `any` can be
satisfied by the selected concrete value.

## Band Rules

`lte band` and `nr band` support relationship tokens:

- `intra`: selected bands must support a same-band relationship.
- `inter`: selected bands must support a different-band relationship.

Repeated concrete band values are preserved. For example, `b1 + b1` is not
collapsed to `b1 + any`. This matters because repeated bands represent an
intra-band requirement.

Relationship tokens may remain in output specs when no concrete resolution is
available, for example `any + intra`.

## Equipment Count

Equipment count is intentionally narrower than total requirement count.

The solver counts only:

- DU group: numeric sum of `enb`, `vdu`, `au`, `cu`
- RU group: number of tokens in `ru`
- UE group: number of tokens across `ue capa lte`, `ue capa nr`,
  `ue capa special`

Other fields are requirements but are not equipment.

Numeric equipment values are summed. Non-numeric equipment tokens and `any`
count as one slot.

## Candidate Generation

The solver does not enumerate every possible testcase subset. That would be
infeasible for 1000 to 3000 rows.

Instead, it creates a bounded deterministic candidate pool:

- exact row candidates, guaranteeing feasibility;
- compatible bucket merges based on single-select columns;
- sliding window merges inside compatible buckets;
- coarse signature merges for similar requirement shapes.

The optimization result is exact over this generated candidate pool, not over
every theoretically possible merged spec.

Candidate growth is controlled by `--max-candidates-per-bucket`. This keeps
runtime and memory practical for large files.

## Optimization Engine

The solver requires Google OR-Tools CP-SAT.

If OR-Tools is not installed, the program exits with:

```text
Missing dependency: pip install ortools
```

OR-Tools was chosen because the assignment problem is naturally modeled with
binary decision variables:

- whether a candidate spec is selected;
- whether a testcase is assigned to a selected spec.

Every testcase must be assigned exactly once.

## Optimization Priority

The objective is lexicographic and solved in stages:

1. Minimize maximum equipment count in any selected spec.
2. Minimize total equipment count across selected specs.
3. Minimize selected spec count.
4. Minimize assignment imbalance.
5. Minimize total delta.

The implementation uses staged solves instead of one huge weighted objective to
avoid integer overflow and weight-tuning problems on large candidate pools.

## Timeout Behavior

The default timeout is 600 seconds.

If CP-SAT proves the final staged objective, the output status is `OPTIMAL`.
If it finds a feasible solution but cannot finish within the timeout, the output
status is `FEASIBLE_TIMEOUT`.

The output remains valid in both cases. With `FEASIBLE_TIMEOUT`, the result is
the best feasible assignment found inside the generated candidate pool.

## Output Format

`output_specs.csv` includes:

- `spec_id`
- `assigned_tc_ids`
- `assigned_count`
- `covered_tc_ids`
- `covered_count`
- `equipment_count`
- `total_delta`
- `solve_status`
- all original requirement columns except `tc_id`

Assigned testcase IDs are the final one-to-one assignment used for balancing.
Covered testcase IDs show all testcases that the spec could cover.

## Verification

Before writing output, the solver validates:

- every testcase is assigned exactly once;
- each assigned testcase is covered by its assigned spec;
- per-column delta does not exceed one;
- single-select columns do not emit multiple concrete values;
- equipment count matches the implemented counting rule.

The program exits with a clear error if no feasible solution exists.

## Benchmark Generator

The benchmark program is `benchmark_random_inputs.py`.

It generates random input files and runs `solve_test_lines.py` on them. It is
used to evaluate performance and solution behavior across different random
sizes and variation levels.

By default, generated data is written to `random_input.csv` and solver output to
`random_output_specs.csv`, so the real `input.csv` is not overwritten.

## Random Data Decisions

The random generator intentionally limits value universes:

- 8 LTE bands
- 8 NR bands
- 8 RU types

This creates repeated combinations that are useful for performance testing.
Completely unbounded random values would produce mostly unmergeable rows and
would not exercise the optimizer meaningfully.

Approximately 50% of generated values are `any`, as requested.

UE capability generation uses:

- LTE: `emtc`, `volte`
- NR: `nr`
- Special: `6cc`, `s23+`, `s21`
- NSA: `nsa` appears in both `ue capa lte` and `ue capa nr`, matching the sample
  `input.csv` pattern.

## Known Limits

The solver is scalable by limiting candidate generation, but it is not a proof
of global optimality over all possible merged specs for large inputs.

For very large files, `--max-cover-checks-per-candidate` can reduce coverage
checking cost, but using it may miss some coverable testcase/spec relationships.
The default value is `0`, which checks all rows.

The random generator is for performance evaluation, not for simulating every
real telecom rule. It focuses on the columns and constraints currently handled
by the solver.
