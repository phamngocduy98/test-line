# Solver Implementation Plan

## Summary

Build the solver in small slices: parser, validation, domain coverage, candidate
generation, optimization, and output. Implement parser and validation first, then
the solver. The requirement document is the behavior source of truth. Deprecated
code is a reference only when it does not conflict with `docs/REQUIREMENT.md`.

The key design choice is to avoid monster specs by optimizing practical
footprint before spec count. A single broad catch-all spec should lose to a
slightly larger set of focused specs when the broad spec adds equipment,
capacity, wildcard breadth, alternatives, or extra slots beyond the testcase
requirements.

## Key Changes

- Add a context-aware parser:
  - Blank testcase requirement cells parse as no requirement.
  - RU-band blank band cells parse as empty support lists.
  - Header names match exactly after BOM removal.
  - `tc_id` is validated and preserved, not treated as a requirement column.
- Add validation for all documented input failures, unknown concrete RUs/bands,
  duplicate `tc_id`, missing final-solver `ru`, invalid support-table RU
  alternatives, and no compatible RU-band realization.
- Add `--parse-only` as a parser/debug mode. It prints the parsed JSON data
  model and exits without requiring final-solver-only testcase columns, solving,
  or writing `output_specs.csv`.
- Implement domain coverage:
  - Distinct-slot matching for token lists.
  - Lowest-excess matching when multiple distinct-slot matchings can cover the
    same testcase column.
  - Numeric capacity logic for `enb`, `vdu`, `au`, `cu`, and `ue`, accepting only
    one non-negative integer token per non-blank numeric equipment cell.
  - Band relation handling for `intra` and `inter`.
  - No single-select behavior for `cc location`; `intra cc + inter cc` is valid.
  - RU slot-count coverage before RU-band compatibility. For example,
    `ru=any + any` is not covered by a one-slot spec such as `ru=a`.
  - RU-band compatibility using the union of selected RU support after slot
    counts and required values are covered. One RU can satisfy multiple band
    slots if only one RU slot is required and that RU supports those bands.
- Implement merge/spec construction without inventing new alternatives:
  - `A/B` merged with `C` becomes `A/B + C`.
  - `A` merged with `B` becomes `A + B`.
  - `A/B` merged with `A` remains `A/B`.
- Generate candidates from exact testcase realizations, compatible merges,
  overlap buckets, and deterministic sliding windows. Keep exact candidates so
  every valid testcase remains coverable.
  - Freeze candidate generation as deterministic behavior before solver coding:
    exact candidates in input order, stable bucket keys, deterministic merge
    ordering, dedupe by normalized rendered spec signature, and caps applied only
    after exact candidates are retained.
- Expand `ru=any`, LTE-band `any`, and NR-band `any` into compatible per-slot
  domains using the RU-band support table. Preserve the full compatible domain
  in output, such as `RU1/RU2/RU3 + RU2/RU3/RU4`, instead of choosing one
  arbitrary realization.
- Keep runtime bounded for about 3000 testcases:
  - Precompute normalized column tokens, support lookups, coverage signatures,
    and candidate coverage bitsets.
  - Bucket before merging; do not run unrestricted all-pairs merges.
  - Use default budgets of 20000 total candidates, 250 generated merge
    candidates per bucket, and max merge window width 55.
  - Always build a greedy incumbent before exact search so timeout can still
    return a valid solution.
- Apply practicality guardrails during coverage and candidate selection:
  - Default max non-numeric extra slots: `1`.
  - Default max extra alternatives per matched slot: `1`.
  - Numeric overcapacity above both `2x` and `+1` is impractical by default.
  - Spec-side `any` covering a concrete testcase value adds `1` assignment
    excess by default, is scored before spec count, and may be rejected per
    column by option.
- Optimize lexicographically:
  - Satisfy guardrails.
  - Minimize total equipment count.
  - Minimize total assignment excess.
  - Minimize low-use selected specs and low-use deficit when doing so does not
    worsen equipment or assignment excess.
  - Minimize selected spec count.
  - Use deterministic tie-breaks.
- Model assignment during optimization. Every testcase is assigned to exactly one
  selected spec for scoring even when `--auto-assign` is not used. The
  `--auto-assign` option only controls whether assigned testcase IDs are emitted
  in the output CSV.
- Compute `covered_tc_ids` and `assigned_tc_ids` against the final emitted spec
  domains after RU/band wildcard expansion, so output never claims coverage that
  the rendered spec cannot actually provide.
- Share final expanded assignment evaluation between output and low-use
  analysis. Run a bounded post-solve low-use refinement pass that may remove or
  replace low-use specs only when complete coverage, total equipment, and final
  assignment excess are not worsened. The pass uses the remaining `--timeout`
  budget and reports `FEASIBLE_TIMEOUT` if it cannot complete low-use checking.
- Add `--low-use-refinement-timeout` so low-use refinement can receive a
  dedicated post-solve budget. When set, refinement still runs after a primary
  `FEASIBLE_TIMEOUT`; the final status remains `FEASIBLE_TIMEOUT` if the
  primary solver timed out or refinement itself times out.
- Add `--refine-output` as a refinement-only mode. It imports selected specs
  from an existing output CSV, ignores old output metadata, rebuilds
  coverage/assignment from the current input and RU-band support, regenerates
  candidates, runs only low-use refinement, and writes a new output CSV.
- Use `FEASIBLE_LOW_USE_CHECKED` when low-use analysis completes without a
  change, and `FEASIBLE_LOW_USE_REFINED` when the bounded refinement changes the
  selected specs.
- Use OR-Tools CP-SAT as the preferred `auto` backend when installed, with the
  standard-library branch-and-bound solver kept behind `--solver stdlib` and as
  an `auto` fallback when OR-Tools is unavailable. Report OR-Tools
  feasible-but-not-proven results as `FEASIBLE_TIMEOUT`, not as a public
  `FEASIBLE` status.

## Public Interfaces

- `python3 solve_test_lines.py` reads `input.csv`, reads `ru-band.csv`, and
  writes `output_specs.csv`.
- CLI options:
  - `--input PATH`
  - `--output PATH`
  - `--ru-band PATH`
  - `--ru-band-support PATH` as a legacy alias
  - `--parse-only`
  - `--limit-rows N`
  - `--auto-assign`
  - `--ignore-optional-columns`
  - `--ignore-tech-and-ue-capa` as a legacy alias for `--ignore-optional-columns`
  - `--timeout SECONDS`
  - `--low-use-refinement-timeout SECONDS`
  - `--refine-output PATH`
  - `--solver auto|stdlib|ortools`
  - `--solver-threads N`
  - `--max-candidates N`, default `20000`
  - `--max-candidates-per-bucket N`, default `250`
  - `--max-merge-width N`, default `55`
  - `--max-extra-slots N`
  - `--max-extra-alternatives N`
  - `--max-numeric-overage-ratio FLOAT`
  - `--max-numeric-overage-units N`
  - `--reject-spec-side-wildcard COLUMN`, repeatable
  - `--min-assigned-cases-per-spec N`, default `10`; `0` disables low-use
    analysis and refinement
- CLI progress and elapsed-time messages are written to stderr so parse-only
  JSON remains the only stdout payload.

## Test Plan

- Parser tests for BOM, exact header names, testcase blank-as-empty, support
  blank-as-empty, `+`, `/`, duplicate alternatives, explicit `any`, `null`,
  unknown columns, row order, column order, and `tokens` excluding `tc_id`.
- Validation tests for every rejection listed in `docs/REQUIREMENT.md`.
- Validation tests for duplicate `tc_id`, support-table RU `/` alternatives, and
  invalid numeric equipment forms such as `1 + 2`, `1/2`, `any`, negative
  numbers, and decimals.
- Domain tests for wildcard matching, distinct slots, numeric capacity,
  overcapacity guardrails, band relations, and RU-band compatibility.
- Domain tests proving lowest-excess slot matching is used, such as testcase `B`
  matching the exact `B` slot in spec `A/B + B`.
- Domain tests proving RU slot count is enforced before RU-band compatibility,
  including `ru=any + any` not being covered by a one-slot `ru=a` spec.
- Domain tests proving one RU can satisfy multiple band slots when only one RU
  slot is required and the support table allows it.
- RU/band domain tests proving `ru=any` expands to all compatible alternatives
  per slot and does not collapse to one arbitrary concrete solution.
- Merge tests proving separate concrete requirements are not converted into new
  `/` alternatives, such as `A/B` with `C` becoming `A/B + C`, while overlapping
  alternatives such as `A/B` with `B/C` merge to `A/B/C`.
- Candidate tests proving exact testcase candidates are retained and broad
  merged candidates are pruned or penalized by guardrails.
- Solver tests proving a broad catch-all candidate loses to focused specs when
  it has greater equipment or assignment excess.
- Solver tests proving low-use specs are reported, can be disabled with
  `--min-assigned-cases-per-spec 0`, and are refined only when equipment and
  assignment excess do not get worse. Include status tests for checked, refined,
  disabled, and timeout cases.
- Tests proving a primary `FEASIBLE_TIMEOUT` does not starve refinement when
  `--low-use-refinement-timeout` is set.
- Refinement-only tests proving imported output metadata is ignored, current
  input coverage is recomputed, stale or malformed output is rejected, and
  low-use specs can be checked or refined without running the primary optimizer.
- Performance tests with synthetic 3000-row fixtures verifying a valid solution
  is produced within a bounded timeout and reports `FEASIBLE_TIMEOUT` when
  proof of optimality is not completed.
- Output tests for deterministic ordering with final spec-signature tie-breaks,
  stable `spec_id`, ignored columns, `covered_tc_ids` after RU/band expansion,
  `--parse-only`, and `--auto-assign`.

## Assumptions

- The final solver is exact over the generated candidate pool, not over every
  possible theoretical spec.
- Guardrails are enabled by default because the operational goal is practical
  reusable specs, not the fewest CSV rows at any cost.
- The 3000-row target prioritizes valid bounded results over exhaustive global
  optimality.
- Requirement changes here supersede conflicting deprecated behavior.
