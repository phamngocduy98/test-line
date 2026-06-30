# Merge Feature Decisions

This document is the source of truth for the second-pass smart merge feature
implemented by `merge_output_specs.py`.

## Purpose

The merge pass compacts the first-pass output from `solve_test_lines.py` by
transferring compatible testcase coverages into fewer test line specs.

It reads:

- first-pass specs from `output_specs.csv` by default;
- original testcase requirements from `input.csv` by default;
- the required RU-band support CSV.

It writes `merged_output_specs.csv` by default, using the same columns as the
first-pass output and setting `solve_status` to `SECOND_PASS`.

Example:

```text
python merge_output_specs.py \
  --input output_specs.csv \
  --testcases input.csv \
  --ru-band-support ru_band_support.csv \
  --output merged_output_specs.csv
```

## Current Implemented Behavior

### Candidate Construction

Each active pair produces a combined candidate across all requirement fields.

- Numeric `enb`, `vdu`, `au`, `cu`, and `ue` capacity uses the maximum required
  value from the two specs.
- Multi-slot requirements combine the minimum tokens needed to cover both
  specs.
- LTE and NR cells combine only bands and relation tokens required by either
  spec.
- Single-select conflicts reject the candidate.

The earlier group identity is retained, its spec is replaced by the combined
candidate, and the later group's coverages are transferred into it.

### Candidate Limits

The resulting candidate must satisfy:

- RU slot count at most `--max-ru`, default `3`;
- each DU column is limited independently by `--max-enb`, `--max-vdu`,
  `--max-au`, and `--max-cu`, each defaulting to `2`;
- UE capacity at most `--max-ue`, default `10`.

An absent `ue` column counts as zero.

### RU-Band and Relation Validation

The RU support table is used to increase merge opportunities while keeping the
candidate physically valid:

- every concrete LTE and NR band must be supported by at least one selected RU;
- `inter` requires at least two distinct bands available from the selected RUs;
- `intra` requires at least one band available from the selected RUs;
- literal `inter` and `intra` tokens remain in the output;
- unrelated supported bands are not added to the candidate.

The candidate must cover both original specs and every testcase covered by
either group with the solver's delta restriction disabled. Validating the
covered testcases directly is required because coverage is not transitive when
a prior spec contains `any` and the combined candidate selects a concrete value.
An incompatible candidate is rejected before either group is mutated.

### Fixed-Point Scan

After every successful merge, pair scanning restarts from the current active
set. Processing stops only after a complete scan finds no compatible pair.
This allows a newly expanded candidate to merge with additional specs.

### Final Validation

After merging finishes, every covered testcase is checked against its final
spec with the delta restriction disabled.

The program prints:

```text
merged_requirement_check=PASS
unsatisfied_testcases=0
```

If any testcase is not covered, it prints each testcase and target spec, exits
with an error, and does not write the merged output.

### Verbose Diagnostics

Use `-v` or `--verbose` to log every pair merge attempt.

Verbose output includes:

- `TRY` for an attempted target/source direction;
- `FAIL` with the first candidate-construction, capacity, RU-band, relation, or
  coverage failure;
- `MERGE` when the combined candidate is accepted;
- `NEW_SPEC` with the combined covered testcase IDs and every rendered requirement
  field after an accepted merge.

Example:

```text
FAIL left=spec_1 right=spec_2 condition=max_ru actual=4 limit=3
FAIL left=spec_1 right=spec_2 condition=max_enb actual=3 limit=2
FAIL left=spec_1 right=spec_2 condition=max_ue actual=12 limit=10
FAIL left=spec_1 right=spec_2 condition=covered_testcase_coverage tc_id=A column='ru' candidate='rf-1' requirement='rf-2'
NEW_SPEC target=spec_1 covered_tc_ids=A + B ru='rf-1' enb='1'
```

## Failure Handling

The pass stops with an error when:

- required input columns are missing;
- a testcase ID is unknown or uncovered;
- solver output contains requirement columns absent from testcase input;
- any RU, ENB, VDU, AU, CU, or UE maximum is negative;
- final merged coverages do not satisfy their testcase requirements.

Merge incompatibility is not an execution error. The two specs remain separate.

## Determinism

Pair selection uses original input order. Successful merges retain the earlier
input group identity. Assignment indices and final output groups are sorted
using stable deterministic keys.

## Output Metadata

The output preserves first-pass requirement columns and core metadata:

- `spec_id`
- `covered_tc_ids`
- `covered_count`
- `equipment_count`
- `solve_status`
- all requirement columns

With `--auto-assign`, the merge pass recalculates a balanced one-to-one
testcase assignment after all merges and adds `assigned_tc_ids` and
`assigned_count`. Assignment is limited to specs that cover each testcase.
Without the flag, assignment columns are omitted.

`covered_tc_ids` is recalculated with the delta restriction
disabled. Output spec IDs are reassigned after final sorting.

## Required Test Coverage

The merge feature must retain tests for:

- combined candidate construction across all fields;
- deterministic earlier-group retention;
- fixed-point chained merging;
- RU, DU, and UE limits;
- inputs without an active UE column;
- supported LTE and NR band extension;
- rejection of unsupported combined bands;
- feasible and infeasible `inter` and `intra` combinations;
- preservation of relation tokens;
- single-select conflicts;
- merges with delta greater than one;
- final testcase validation;
- verbose success and failure diagnostics;
- input assignment validation and compacted CSV output.
