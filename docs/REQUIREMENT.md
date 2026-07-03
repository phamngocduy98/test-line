# Test Line Solver Requirements

## Problem

The program receives a list of telecom test case requirements and an RU-band
support table. It must produce a smaller set of reusable test line
specifications that can cover all input test cases while respecting equipment,
band, wildcard, and compatibility constraints.

The core question is:

> Given many test cases with overlapping requirements, what set of test line
> specs covers every test case with the least practical amount of equipment?

The solver must prefer practical selected specs over simply minimizing the
number of rows. A solution with fewer specs must not win when it does so by
creating one very broad spec with substantially more equipment, capacity,
wildcards, alternatives, or extra slots than the covered testcases require.

The solver should prefer, in order:

1. Specs that satisfy the default practicality guardrails.
2. Lower total equipment count across all selected test line specs, because
   each additional spec adds equipment.
3. Fewer testcase assignments to specs that are larger than the testcase's own
   requirements.
4. Fewer selected specs that receive very few assigned testcases, because such
   specs are usually not worth building when a practical alternative exists.
5. Fewer selected test line specs.

A testcase is assigned to a larger spec when the selected spec contains extra
slots, capacity, spec-side wildcard breadth, concrete values, or alternatives
beyond what that testcase requires. These assignments are allowed only within
the practicality guardrails by default, and should be minimized before spec
count is optimized.

A selected spec assigned fewer than 10 testcases is considered low-use by
default. Low-use specs are undesirable, not invalid. Low-use refinement should
try to merge low-use testcase assignments into existing non-low receiver specs
until none remain when feasible. The refinement pass is a fast low-use merge
pass: it broadens receiver specs directly from exact testcase requirements,
uses exact receiver assignment only for small search spaces, and uses greedy
iteration for larger spaces. It does not prove the best possible refinement,
does not scan generated candidates, and ignores assignment excess.

The first rewrite milestone only needs to read the input CSV files and parse
their cells into normalized requirement tokens.

## Input Files

### Testcase Requirements CSV

Default path: `input.csv`

This file describes the requirements for each testcase. It must be a CSV file
encoded as UTF-8, with optional BOM support.

Header names are matched exactly after UTF-8 BOM removal. Header names are not
trimmed, case-folded, or normalized.

Required columns:

| Column | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tc_id` | string | yes | Unique testcase identifier. Must be non-empty after trimming whitespace. |
| `ru` | token list | final solver only | RU requirement. Required for final solving and RU-band compatibility. `--parse-only` may parse files that do not have `ru`. |

All other columns are requirement columns. Known requirement columns include:

| Column | Type | Meaning |
| --- | --- | --- |
| `tech lte` | token list | LTE technology requirement. |
| `tech nsa` | token list | NSA technology requirement. |
| `tech nr sa` | token list | NR SA technology requirement. |
| `enb` | integer capacity | eNB capacity requirement. |
| `vdu` | integer capacity | vDU capacity requirement. |
| `au` | integer capacity | AU capacity requirement. |
| `cu` | integer capacity | CU capacity requirement. |
| `lte band` | band token list | LTE band requirements. |
| `nr band` | band token list | NR band requirements. |
| `ru` | token list | RU requirements. |
| `cc location` | token list | Carrier aggregation location requirement. |
| `ca type` | token list | Carrier aggregation type requirement. |
| `rf condition` | token list | RF condition requirement. |
| `ue` | integer capacity | UE capacity requirement. |
| `ue capa lte` | token list | LTE UE capability requirement. |
| `ue capa nr` | token list | NR UE capability requirement. |
| `ue capa special` | token list | Special UE capability requirement. |

The program must preserve unknown requirement columns and parse them with the
same generic token rules unless a later requirement gives them special meaning.

Optional requirement columns are active by default. When optional columns are
ignored, they must not affect coverage, merging, assignment excess, equipment
count, or solver selection, but they must remain in the output as blank columns.
The default optional columns are:

- `tech lte`
- `tech nsa`
- `tech nr sa`
- `ue capa lte`
- `ue capa nr`
- `ue capa special`

### RU-Band Support CSV

Default path for the rewrite: `ru-band.csv`

Legacy CLI option: `--ru-band-support`

This file describes which LTE and NR bands each RU can support. It must be a
CSV file encoded as UTF-8, with optional BOM support.

Header names are matched exactly after UTF-8 BOM removal. Header names are not
trimmed, case-folded, or normalized.

Required columns:

| Column | Type | Required | Meaning |
| --- | --- | --- | --- |
| `ru` | concrete string | yes | RU name. Must be one concrete RU value. |
| `lte_band` | token list | yes | LTE bands supported by the RU. Blank means no LTE bands listed. |
| `nr_band` | token list | yes | NR bands supported by the RU. Blank means no NR bands listed. |

RU support rows with the same RU name must be merged by unioning their LTE and
NR bands. RU and band matching must be case-insensitive, while output should use
the first spelling seen in the support CSV.

## Cell Data Type

Every non-`tc_id` cell in the testcase requirements CSV is parsed as a
requirement cell.

### Requirement Cell Grammar

```text
cell         := empty | slot ("+" slot)*
slot         := alternative ("/" alternative)*
alternative  := non-empty text after trimming whitespace
```

Generic requirement cell rules:

- In the testcase requirements CSV, a blank requirement cell means no
  requirement and parses to no tokens.
- `+` separates required slots.
- `/` separates alternatives within one slot.
- Whitespace around `+` and `/` is ignored.
- Empty slots and empty alternatives are ignored.
- Duplicate alternatives in the same slot are removed while preserving order.
- The literal text `null` is a normal value, not a blank.
- There is no escaping for `+` or `/`; they are always separators.

Column-specific blank rules:

- In numeric equipment columns (`enb`, `vdu`, `au`, `cu`, and `ue`), a blank
  testcase cell means no capacity or equipment requirement and parses to no
  tokens, matching the generic testcase blank rule.
- In numeric equipment columns, a non-blank testcase cell must be one
  non-negative base-10 integer token such as `0`, `1`, or `12`.
- In RU-band support columns (`lte_band` and `nr_band`), a blank cell means no
  listed support bands and parses to no tokens.

Examples:

| Raw cell | Parsed tokens |
| --- | --- |
| empty testcase requirement cell | `[]` |
| empty numeric equipment testcase cell | `[]` |
| empty RU-band support band cell | `[]` |
| `b1 + b2` | `[["b1"], ["b2"]]` |
| `n1+n2` | `[["n1"], ["n2"]]` |
| `rf-1/rf-2 + any` | `[["rf-1", "rf-2"], ["any"]]` |
| `a/a++b/` | `[["a"], ["b"]]` |

### Token Meaning

A parsed token is one required slot. A token may contain one or more
alternatives.

```json
["rf-1", "rf-2"]
```

means one slot that may be satisfied by `rf-1` or `rf-2`.

Multiple tokens mean multiple required slots:

```json
[["b1"], ["b2"]]
```

means both `b1` and `b2` are required.

### Special Values

| Value | Meaning |
| --- | --- |
| `any` | Wildcard slot, meaning an open choice for one required slot. Case-insensitive. |
| `intra` | Band relationship token for same-band aggregation. Case-insensitive. |
| `inter` | Band relationship token for different-band aggregation. Case-insensitive. |

A token containing `any` as one alternative, such as `rf-1/any`, must be treated
as a wildcard-capable token.

Plain-language `any` examples:

| Requirement | Meaning |
| --- | --- |
| `ru=any` | The testcase needs one RU slot, but does not care which RU. |
| `ru=any + any` | The testcase needs two RU slots, but does not care which RUs. |
| `lte band=b1 + any` | The testcase needs one `b1` band slot and one additional open band slot. |

## Parsed Data Model

After reading the input files, the program must represent each CSV as:

```text
ParsedCsv
  path: source file path
  columns: ordered list of CSV headers
  rows: ordered list of ParsedRow

ParsedRow
  row_number: original 1-based CSV row number
  raw: map of column name to original cell text
  tokens: map of non-tc_id column name to parsed token list

Token
  alternatives: ordered list of strings
```

Column order and row order must be preserved.

## Final Expected Output

The final solver must write an output CSV containing selected test line specs.

Default path: `output_specs.csv`

Each output row represents one selected test line spec.

Required output columns:

| Column | Type | Meaning |
| --- | --- | --- |
| `spec_id` | string | Stable generated ID such as `spec_1`. |
| `covered_tc_ids` | string list | Testcase IDs covered by this spec, joined with ` + `. |
| `covered_count` | integer | Number of covered testcases. |
| `equipment_count` | integer | Equipment count for this spec. |
| `solve_status` | string | Solver status, such as `OPTIMAL`, `FEASIBLE_TIMEOUT`, `FEASIBLE_LOW_USE_CHECKED`, or `FEASIBLE_LOW_USE_REFINED`. |
| `main_solve_status` | string | Status returned by the primary optimizer, or `IMPORTED` in `--refine-output` mode. |
| `low_use_refinement_status` | string | Low-use refinement pass status: `DISABLED`, `COMPLETED_UNCHANGED`, `COMPLETED_REFINED`, or `FEASIBLE_TIMEOUT`. |
| input requirement columns | rendered requirement cell | Spec values for each input column except `tc_id`. |

When auto-assignment is enabled, the output must also include:

| Column | Type | Meaning |
| --- | --- | --- |
| `assigned_tc_ids` | string list | Testcase IDs assigned to this spec, joined with ` + `. |
| `assigned_count` | integer | Number of assigned testcases. |

The final solver must always choose an internal assignment from testcases to
selected specs for optimization. `--auto-assign` controls only whether those
assignments are written to the output CSV. This prevents a selected spec from
looking good only because it covers many cases broadly while no testcase would
prefer to use it in practice.

The low-use threshold is based on assigned testcase count, not technical
coverage count. A threshold of `0` disables low-use analysis and refinement.

`covered_tc_ids` and `assigned_tc_ids` must be computed from the same final spec
representation that is written to output, after `ru`, `lte band`, and `nr band`
wildcards have been expanded to compatible domains. Computing coverage before
expansion can list impossible coverage. For example, if RU `a` supports only
`b1`, then a pre-expansion spec `ru=a, lte band=any` might appear to cover a
testcase that needs `b2`; after expansion the output spec is effectively
`ru=a, lte band=b1`, so the `b2` testcase must not appear in `covered_tc_ids`.

Output formatting rules:

- Output must be written as UTF-8 CSV.
- `spec_id` values must be `spec_1`, `spec_2`, and so on in output order.
- Selected specs must be sorted by equipment count ascending, covered count
  descending, earliest covered testcase index ascending, then rendered spec
  signature ascending.
- For `ru`, `lte band`, and `nr band`, wildcard slots must expand to the full
  compatible domain for that slot before final output. Preserve slot count and
  render each slot as alternatives. For example, two RU wildcard slots may
  render as `RU1/RU2/RU3 + RU2/RU3/RU4` after applying band constraints.
- For `ru`, `lte band`, and `nr band`, do not emit an arbitrary single concrete
  realization such as `RU1 + RU2` when the slot has multiple compatible values,
  and do not emit `any` when doing so would lose band compatibility
  information.
- For other columns, if `any` can correctly represent an output slot without
  losing required coverage or compatibility information, keep `any` instead of
  enumerating concrete values.
- Requirement slots must render alternatives with `/`.
- Requirement cells must render slots with ` + `.
- Ignored input requirement columns must remain in the output and be blank.

### CLI Modes

- Normal solving reads the testcase CSV and RU-band support CSV, validates final
  solver requirements, solves, and writes `output_specs.csv`.
- `--parse-only` reads both CSV files, validates basic CSV structure, parses
  cells into the parsed data model, prints the parsed JSON, and exits without
  solving or writing `output_specs.csv`.
- `--parse-only` must still require `tc_id` in the testcase CSV and `ru`,
  `lte_band`, and `nr_band` in the RU-band support CSV, but it must not require
  final-solver-only fields such as testcase `ru`.
- `--limit-rows N` limits processing to the first `N` testcase input rows after
  parsing. It applies to both normal solving and `--parse-only`; `N` must be a
  positive integer.
- `--min-assigned-cases-per-spec N` treats selected specs with fewer than `N`
  assigned testcases as low-use. The default is `10`; `0` disables low-use
  analysis and refinement.
- `--low-use-refinement-timeout SECONDS` gives the low-use refinement pass a
  dedicated positive timeout after the primary solver returns. When omitted,
  refinement uses the remaining `--timeout` budget.
- `--refine-output PATH` skips the primary optimizer, imports selected specs
  from an existing output CSV, rebuilds coverage and assignment from the current
  input and RU-band support, runs only low-use refinement, and writes a new
  output CSV. It cannot be used with `--parse-only` or with low-use refinement
  disabled.
- `--max-low-use-merge-combinations N` caps when the fast low-use merge pass
  enumerates all receiver assignments exactly. The default is `1000000`; larger
  spaces use greedy iteration. `N` must be a positive integer.
- `--low-use-affordable-equipment-delta N` caps cumulative equipment increase
  allowed during the fast low-use merge pass. The default is `0`; `N` must be zero
  or a positive integer.
- The CLI should print progress messages and final elapsed time to stderr so
  stdout remains usable for `--parse-only` JSON output.

In `--refine-output` mode, imported output metadata such as `spec_id`,
`covered_tc_ids`, `covered_count`, `equipment_count`, `solve_status`,
`main_solve_status`, `low_use_refinement_status`, `assigned_tc_ids`, and
`assigned_count` must not be trusted. Coverage, assignment, equipment, and
status are recomputed from the current input, support table, and options. The
imported output must contain all current input requirement columns except
`tc_id`; unknown extra columns, malformed cells, duplicate imported specs,
incompatible specs, zero-coverage specs, and specs that do not cover every
current testcase must be rejected.

## Performance Requirements

The final solver is expected to handle about 3000 testcase rows in less than
10 minutes on a normal developer machine. This is a bounded-runtime
requirement: the solver must return the best valid deterministic solution found
within the configured timeout, even if it cannot prove global optimality.

Default performance behavior:

- The default solve timeout is 600 seconds.
- The solver must always keep exact per-testcase candidates so every valid
  testcase remains coverable.
- Candidate generation must be bounded by deterministic budgets, such as
  per-bucket candidate caps and global candidate caps.
- Default candidate budgets are 20000 total candidates, 250 generated merge
  candidates per bucket, and a maximum merge window width of 55 testcase rows.
- Candidate generation must be deterministic. Exact per-testcase candidates must
  be created in input order and retained before generated-merge caps are applied.
  Generated merge candidates must use stable bucket keys, stable merge ordering,
  stable deduplication by normalized rendered spec signature, and stable
  truncation when a cap is reached. No randomized candidate ordering is allowed
  by default.
- The solver must avoid all-pairs or exhaustive merge enumeration across all
  testcase rows unless bounded by a configured cap.
- Coverage checks should use compact precomputed structures, such as testcase
  indexes or bitsets, rather than repeatedly scanning every row for every
  candidate when avoidable.
- When the timeout is reached after a complete valid solution is found, output
  that solution with `solve_status` set to `FEASIBLE_TIMEOUT`.
- OR-Tools `FEASIBLE` results mean the generated-candidate optimum was not
  proven and must be reported as `FEASIBLE_TIMEOUT`; plain `FEASIBLE` is not a
  public output status.
- When `--low-use-refinement-timeout` is set, the low-use refinement pass must
  still run for up to that dedicated budget after a primary `FEASIBLE_TIMEOUT`
  result. The final status remains `FEASIBLE_TIMEOUT` if the primary solver
  timed out or if refinement does not complete.
- The output also reports per-pass statuses. `main_solve_status` preserves the
  primary optimizer status even when final `solve_status` changes after
  low-use checking. `low_use_refinement_status` reports whether refinement was
  disabled, completed unchanged, completed with changes, or returned a valid
  incumbent without completing/proving the refinement search.
- When low-use analysis is enabled and completes without changing the primary
  solution, output that solution with `solve_status` set to
  `FEASIBLE_LOW_USE_CHECKED`.
- When the primary solution is improved by the fast low-use merge pass,
  output that solution with `solve_status` set to
  `FEASIBLE_LOW_USE_REFINED`.
- `OPTIMAL` means optimal over the generated candidate pool under the primary
  guardrails and budgets, not over every theoretical possible spec. Low-use
  refinement prioritizes fast useful improvement over proof. If the refinement
  timeout expires, the final status must be timeout-style while preserving any
  accepted improvements.

## First Milestone Expected Output

Before optimization is implemented, the parser milestone must print or return a
structured representation of both parsed input files.

The parser output must include:

| Field | Type | Meaning |
| --- | --- | --- |
| `input.path` | string | Path of the testcase requirements CSV. |
| `input.columns` | string array | Ordered testcase CSV columns. |
| `input.rows[].row_number` | integer | Source row number. |
| `input.rows[].raw` | object | Raw cell values by column. |
| `input.rows[].tokens` | object | Parsed token alternatives by non-`tc_id` column. |
| `ru_band.path` | string | Path of the RU-band support CSV. |
| `ru_band.columns` | string array | Ordered support CSV columns. |
| `ru_band.rows[].row_number` | integer | Source row number. |
| `ru_band.rows[].raw` | object | Raw cell values by column. |
| `ru_band.rows[].tokens` | object | Parsed token alternatives by column. |

Example token JSON shape:

```json
{
  "lte band": [["b1"], ["b2"]],
  "ru": [["any"]],
  "cc location": [["inter cc"]]
}
```

## Validation Requirements

The program must reject invalid input with a clear error message when:

- The testcase CSV has no header row.
- The testcase CSV is missing `tc_id`.
- A testcase row has an empty `tc_id`.
- More than one testcase row has the same `tc_id` after trimming whitespace.
- The testcase CSV has no testcase rows.
- The final solver input is missing `ru`.
- A CSV row has more values than headers.
- The RU-band CSV has no header row.
- The RU-band CSV is missing `ru`, `lte_band`, or `nr_band`.
- A support-table RU cell is blank, `any`, `intra`, `inter`, contains multiple
  slots, or contains multiple `/` alternatives.
- Support-table band values contain `any`, `intra`, or `inter`.
- A numeric equipment testcase cell is non-blank and is not exactly one
  non-negative base-10 integer token.
- A concrete testcase RU or band does not exist in the support table.
- A testcase has no compatible RU-band realization.

## Domain Rules For Final Solver

### Coverage

- Every testcase must be covered by at least one selected spec.
- A non-empty requirement must be covered by distinct compatible spec slots.
- Two slots are compatible when either side is `any`.
- Two slots are compatible when their alternatives intersect case-insensitively.
- Blank testcase requirement cells parse to no tokens and mean no requirement.
  Explicit `any` tokens must be covered as wildcard slots.
- A covering non-numeric spec should not have more than one extra slot beyond
  the testcase requirement by default.
- A spec must have at least as many `ru` slots as the testcase requires. For
  example, a testcase with `ru=any + any` requires two RU slots and is not
  covered by a spec with only `ru=a`, even when RU `a` supports all requested
  bands by itself.
- No known requirement column is single-select by default. For example,
  `cc location=intra cc + inter cc` is a valid two-slot requirement unless a
  future requirement document gives that column special single-select behavior.

### Assignment And Excess

Coverage answers whether a selected spec can cover a testcase. Assignment
answers which selected spec the testcase actually uses for optimization. Every
testcase must be assigned to exactly one selected spec that covers it.

Assignment excess is the sum of practical overage introduced by that assignment:

| Overage type | Default score |
| --- | --- |
| Extra non-numeric spec slot beyond the testcase requirement | `+1` per extra slot |
| Extra concrete alternative inside a matched slot | `+1` per extra alternative |
| Spec-side `any` covering a concrete testcase slot | `+1` per wildcard-broadened slot |
| Numeric overcapacity | `spec_capacity - testcase_capacity` |

When more than one distinct-slot matching can cover a testcase column, coverage
and assignment excess must use the matching with the lowest excess. Ties are
broken by testcase slot order, then by the earliest compatible spec slot indexes.
For example, testcase slot `B` matched against spec `A/B + B` uses the exact `B`
slot and has `0` extra-alternative excess, instead of matching `A/B`.

The wildcard-broadening score does not need to be large because assignment
excess is minimized before selected spec count. A score of `1` is enough for an
exact same-equipment assignment to beat a broader assignment before the solver
tries to reduce the number of selected specs.

Low-use handling is applied as a fast post-solve merge pass. During refinement,
reducing low-use selected specs and low-use deficit is primary, then cumulative
equipment delta from the starting solution is minimized. Assignment excess is
ignored during this pass. The pass only moves testcase rows assigned to low-use
specs into existing non-low receiver specs. Replacing a receiver with a broader
merged receiver preserves that receiver's current assignments. If no non-low
receiver exists, assigned low-use specs are left unchanged.

Example:

| Item | RU requirement | Equipment | Covers |
| --- | --- | --- | --- |
| Testcase `T1` | `A` | | |
| Testcase `T2` | `B` | | |
| Spec `S1` | `A` | `1` | `T1` |
| Spec `S2` | `B` | `1` | `T2` |
| Spec `S3` | `A + B` | `2` | `T1`, `T2` |

Selecting `S1 + S2` and selecting only `S3` both use total equipment `2`.
`S1 + S2` has assignment excess `0`; assigning both testcases to `S3` has extra
slot excess. Therefore `S1 + S2` wins even though it has more selected specs.

### Spec Construction

Merging must not invent new `/` alternatives from separate required slots. A
slot with alternatives remains one slot, and a new concrete requirement from
another testcase becomes another required slot.

Examples:

| Merge input | Merged spec |
| --- | --- |
| `A/B` with `C` | `A/B + C` |
| `A` with `B` | `A + B` |
| `A/B` with `A` | `A/B` |
| `A/B` with `B/C` | `A/B/C` |

An extra alternative is an additional choice inside one slot compared with the
testcase. For example, a spec slot `A/B` assigned to a testcase slot `A` has one
extra alternative, `B`. A spec slot `A/B/C` assigned to testcase slot `A/B` has
one extra alternative, `C`.

Compatible overlapping slots may merge by unioning alternatives into one slot.
Disjoint concrete slots must remain separate required slots; this is why `A/B`
with `B/C` becomes `A/B/C`, but `A/B` with `C` becomes `A/B + C`.

### Practicality Guardrails

The solver must avoid "monster specs": selected specs that technically cover
many testcases by being much broader than the actual testcase requirements.
Guardrails apply while deciding whether a candidate spec can cover a testcase,
before the optimizer rewards a lower spec count.

Default guardrails:

- A non-numeric spec column must not have more than one extra slot beyond the
  testcase column it covers.
- A matched non-numeric spec slot must not add more than one extra concrete
  alternative beyond the testcase slot it covers, unless the testcase slot is
  `any`.
- A spec-side `any` slot covering a concrete testcase slot counts as wildcard
  broadening and adds assignment excess. Solver options may reject this
  broadening entirely for selected columns.
- Numeric capacity overage counts as assignment excess. By default, numeric
  coverage is impractical when the spec capacity is more than two times the
  testcase capacity and more than one unit above it.
- Candidate generation may create broader specs for comparison, but the final
  selected solution must satisfy these guardrails unless an explicit option
  relaxes them.

### Numeric Capacity

- `enb`, `vdu`, `au`, `cu`, and `ue` use numeric capacity semantics when both
  sides contain one non-negative base-10 integer token.
- Numeric equipment columns do not support `+`, `/`, `any`, negative numbers, or
  decimal numbers. A blank numeric equipment testcase cell means no requirement.
- Numeric coverage passes when the spec capacity is greater than or equal to the
  testcase capacity.
- Numeric merge keeps the maximum total capacity needed by any merged testcase.

### RU-Band Compatibility

- Concrete RUs and bands must be known in the support table.
- RU, LTE band, and NR band wildcards must be resolved to compatible output
  domains before final specs are emitted. A domain is rendered as all compatible
  alternatives for that slot, not as one arbitrary concrete value.
- A concrete LTE or NR band slot is compatible when at least one selected RU can
  support at least one of that slot's alternatives.
- Compatibility checks use the union of bands supported by all selected RUs.
- Band slots do not consume RU slots one-for-one. One selected RU can satisfy
  multiple band slots when the testcase requires only one RU slot and that RU
  supports those bands. For example, if RU `a` supports `b1 + b2`, then
  `ru=a, lte band=b1 + b2` can cover a testcase requiring one RU slot and those
  two LTE band slots.
- RU slot count still matters before band compatibility is checked. For example,
  if RU `a` supports `b1 + b2`, a testcase requiring `lte band=b1 + b2` and
  `ru=any + any` is not covered by `ru=a` because the testcase requires two RU
  slots. It may be covered by `ru=a + a` or `ru=a + b` when the selected RUs
  collectively support the required bands.
- If a testcase or spec has two RU wildcard slots and both slots have the same
  compatible RU domain, output should preserve both slots, such as
  `a/b/c + a/b/c`, instead of collapsing them into one slot.

### Band Relationships

`intra` and `inter` describe relationships between band slots:

- An explicit `intra` token satisfies an intra-band requirement.
- An explicit `inter` token satisfies an inter-band requirement.
- `b1 + b1` satisfies `intra`.
- `b1 + b3` satisfies `inter`.
- `b1 + any` can satisfy either `intra` or `inter`.
- One concrete band slot by itself satisfies neither `intra` nor `inter`.

### Equipment Count

Equipment count includes only:

| Group | Columns | Count rule |
| --- | --- | --- |
| DU | `enb`, `vdu`, `au`, `cu` | Sum integer capacity; blank means `0`. |
| RU | `ru` | Count RU slots. |
| UE | `ue` | Sum integer capacity; blank means `0`. |

Other requirement columns do not affect equipment count.

## Success Criteria

- The program can parse `input.csv` and `ru-band.csv` without losing column
  order, row order, raw values, or token alternatives.
- The parser produces predictable tokens for blank requirement cells, `+`, `/`,
  explicit `any`, and duplicate alternatives.
- The final solver output covers every testcase.
- The final solver output contains only RU-band compatible specs.
- The final solver does not select a single broad "catch-all" spec when a
  smaller-footprint set of specs satisfies the same coverage.
- The final solver reports selected specs below the low-use assignment threshold
  and may refine them away while minimizing the extra equipment and assignment
  excess needed to remove low-use specs.
- The final solver returns a valid result for about 3000 testcase rows within
  the configured 600 second default timeout.
- The final solver output is deterministic for the same input files and options.
