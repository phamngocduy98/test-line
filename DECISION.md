# Implementation Decisions

This document records the main implementation decisions for the test line spec
optimizer and random benchmark generator.

## Solver File

The main program is `solve_test_lines.py`.

It reads testcase requirements from `input.csv` by default and writes selected
test line specs to `output_specs.csv`. The script is intentionally standalone:
all project-specific rules live in this file so it can be run directly from the
project folder.

Every run must also provide an RU-band support table:

```text
python solve_test_lines.py --ru-band-support ru_band_support.csv
```

## CSV Parsing

Cells are parsed as AND slots separated by plus signs, regardless of surrounding whitespace:

```text
a + b
```

The solver accepts inconsistent separator spacing such as `a +b`, `a+ b`, and
`a+b`. A plus sign is always a separator; capability names containing `+` are
not supported.

Alternatives within one slot are separated by `/`:

```text
rf1234/rf1235 + any
```

This means `(rf1234 or rf1235) and any`. Whitespace around `/` is optional.

Blank cells mean no requirement. The literal value `null` is treated as a real
requirement value.

## `any` Handling

`any` is treated as a cardinality placeholder, not a literal concrete value.

Examples:

- `any` requires one matching slot.
- `any + any` requires two matching slots.
- `any + volte` requires one arbitrary slot and one concrete `volte` slot.

During column merging, the solver keeps `any` only when there are not enough
concrete values to satisfy the required number of slots. The RU-band
compatibility phase then resolves RU/LTE/NR wildcard slots before optimization.

## RU-Band Support

`--ru-band-support` is required. The CSV must contain:

```text
ru,lte_band,nr_band
rf-1001,b1 + b3,n41/n78
```

Each row identifies one concrete RU. LTE and NR cells list every band supported
by that RU; both `+` and `/` are treated as union separators in this table.
Repeated RU rows are unioned. Matching is case-insensitive, while emitted values
use the spelling first seen in the table.

Blank band cells are allowed. Blank or `any` RU names, `any` bands, and
`intra`/`inter` relationship tokens are rejected in the support table.

Before candidate generation, every concrete RU and LTE/NR band referenced by
the testcase input must exist in the support table. Missing values stop the run
with a list of unknown RUs or bands.

For non-empty `ru`, `lte band`, and `nr band` requirements, wildcard slots are
resolved to concrete support-table values:

- `lte band=b1, ru=any` selects an RU that supports `b1`.
- `lte band=any, ru=rf-1001` selects an LTE band supported by `rf-1001`.
- when both are `any`, the solver generates concrete compatible pairs.
- NR uses the same rules.

Blank requirement cells remain blank. Existing concrete `/` alternatives and
slot counts are preserved. For specs with multiple RUs and bands, every
concrete band slot must have at least one alternative supported by at least one
selected RU alternative. One RU may support multiple bands.

Wildcard expansion is deterministic and bounded per merged candidate. Exact
testcase rows are expanded independently, so a testcase with no compatible
concrete realization is reported before optimization.

## Delta Rule

For a spec to cover a testcase, each non-empty column may add at most one extra
token beyond that testcase requirement.

This keeps specs from becoming broad catch-all lines while still allowing useful
merges. The delta is calculated per column, then summed for optimization and
reporting.

## Single-Select Columns

`cc location` is a single-select column.

A generated spec may contain at most one concrete value in this column. If two
testcases require conflicting concrete values, they cannot be merged into the same
candidate spec. `any` can be satisfied by the selected concrete value.

## Band Rules

`lte band` and `nr band` support relationship tokens:

- `intra`: selected bands must support a same-band relationship.
- `inter`: selected bands must support a different-band relationship.

Repeated concrete band values are preserved. For example, `b1 + b1` is not
collapsed to `b1 + any`. This matters because repeated bands represent an
intra-band requirement.

Relationship tokens may remain in output specs when no concrete resolution is
available, for example `any + intra`.

## Temporary Requirement Exclusion

Passing `--ignore-tech-and-ue-capa` excludes every column whose normalized name
starts with `tech ` or `ue capa ` from candidate generation, coverage, deltas,
validation, and equipment count. The columns remain in the output CSV but their
values are blank. Without the flag, these requirements retain their normal
behavior.

## Equipment Count

Equipment count is intentionally narrower than total requirement count.

The solver counts only:

- DU group: numeric sum of `enb`, `vdu`, `au`, `cu`
- RU group: number of tokens in `ru`
- UE group: numeric value in `ue`

Other fields are requirements but are not equipment.

Numeric equipment values are summed. Non-numeric equipment tokens and `any`
count as one slot.

UE capability columns remain coverage requirements unless
`--ignore-tech-and-ue-capa` is used, but they do not determine UE equipment
count.

Numeric DU and UE requirements use capacity semantics. A spec with `ue=2`
covers requirements for `ue=1` or `ue=2`, and merging those requirements keeps
`ue=2`. For these numeric columns, delta is spare numeric capacity rather than
extra token count, and the one-extra-token rule does not apply.

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
runtime and memory practical for large files. The cap applies across all merge
strategies in a bucket. Exact rows contribute one deterministic compatible
realization each; merged seeds may contribute a small number of compatibility
variants until the bucket cap is reached.

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

Each selected spec may be assigned at most `--max-tc-per-spec` testcases. The
default limit is 338. When a spec covers more testcases than the limit, the
candidate pool includes enough identical physical spec instances to preserve
feasibility.

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
the best feasible assignment found inside the generated candidate pool. The
solver starts from a deterministic feasible assignment hint and retains the
last completed stage if a later optimization stage reaches the timeout.

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

## Second-Pass Compaction

`merge_output_specs.py` performs a separate greedy pass over solver output:

```text
python merge_output_specs.py \
  --input output_specs.csv \
  --testcases input.csv \
  --ru-band-support ru_band_support.csv \
  --output merged_output_specs.csv
```

The pass rebuilds merged specs from the original assigned testcase rows, then
rechecks coverage and RU-band compatibility. By default, a spec with at most
three assigned testcases may merge into a strictly larger spec. A resulting
spec may contain at most three RU slots and total DU capacity of three across
`enb`, `vdu`, `au`, and `cu`.

Use `--max-small-tc`, `--max-ru`, `--max-du`, and `--max-tc-per-spec` to change
these limits. The output uses the same columns as the first pass and marks
`solve_status` as `SECOND_PASS`.

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

Concrete RU requirements give each slot a 30% chance of containing two
alternatives separated by `/`. The number of RU slots is still determined by
the existing variation-dependent width distribution.

UE capability generation uses:

- LTE: `emtc`, `volte`
- NR: `nr`
- Special: `6cc`, `s23`, `s21`
- NSA: `nsa` appears in both `ue capa lte` and `ue capa nr`, matching the sample
  `input.csv` pattern.

## Known Limits

The solver is scalable by limiting candidate generation, but it is not a proof
of global optimality over all possible merged specs for large inputs.

For very large files, `--max-cover-checks-per-candidate` can reduce coverage
checking cost, but using it may miss some coverable testcase/spec relationships.
The default value is `0`, which checks all rows. Coverage discovered by repeated
generation of the same spec is combined, so exact-row candidates still
guarantee that every testcase has at least one covering candidate.

The random generator is for performance evaluation, not for simulating every
real telecom rule. It focuses on the columns and constraints currently handled
by the solver.
