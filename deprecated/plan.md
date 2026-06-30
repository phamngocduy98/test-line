## Implementation Plan: Local Search Line Packer

---

### Overview

```
solve_lines.py  (new standalone script)
  │
  ├── Data layer       (reuse existing parsing/merging)
  ├── Cost model       (weighted equipment cost)
  ├── Line state       (incremental spec tracking)
  ├── Initial solution (greedy clustering)
  ├── Local search     (LNS + SA moves)
  ├── Validation       (adapted from current)
  └── Output           (new line-oriented format)
```

---

### Module 1: Cost Model (`cost.py` or inline)

**Data structures:**
```python
@dataclass(frozen=True)
class EquipmentWeights:
    du: float = 1.0
    ru: float = 1.0
    ue: float = 1.0

def line_cost(spec, columns, weights) -> float:
    # weighted sum of DU slots + RU slots + UE capacity
    
def assignment_total_cost(lines, columns, weights) -> float:
    # sum of line_cost across all active lines
```

**Notes:**
- Cost is always recomputed from the live spec, never cached stale
- `line_cost` is the single source of truth used by every move evaluator
- Pluggable: future cost functions (e.g. per-RU-model pricing) only touch this module

---

### Module 2: Line State (core data structure)

This is the most critical piece. Every move needs O(1) cost delta — avoid re-merging all cases from scratch on every move.

```python
@dataclass
class Line:
    line_id: int
    case_indices: set[int]          # which test cases are on this line
    spec: dict[str, tuple[str,...]] # current merged spec (maintained incrementally)
    cost: float                     # current cost (maintained incrementally)
```

**Incremental spec tracking strategy:**

Maintain per-column **token frequency counters** — how many cases on this line need each token. Adding a case = merge its tokens in. Removing a case = recompute spec from remaining cases (removal is hard to do incrementally for max-semantics columns).

```python
class LineState:
    def add_case(self, case) -> float:          # returns new cost
    def remove_case(self, case) -> float:       # returns new cost
    def cost_if_add(self, case) -> float:       # dry-run, no mutation
    def cost_if_remove(self, case) -> float:    # dry-run, no mutation
    def cost_if_swap(self, add_case, remove_case) -> float:
```

**Key insight on removal:** For numeric columns (DU/UE), removal requires knowing if the removed case was the max — need to track the full distribution. For non-numeric columns (RU), removal requires re-merging remaining cases. Two strategies:

- **Lazy recompute:** mark line dirty, recompute spec from scratch on next cost read. O(|cases on line|) but simple and correct.
- **Token counters:** maintain `Counter[token]` per column; spec token is present iff count > 0. Works for most columns; numeric columns track sorted list of values.

**Recommend lazy recompute for correctness first, optimize later.**

---

### Module 3: Initial Solution (greedy clustering)

**Goal:** produce a valid assignment as warm start. Doesn't need to be good, needs to be fast and always feasible.

**Algorithm: Greedy First-Fit Decreasing**

```
1. Sort cases by equipment_cost(case) descending  ← heaviest cases first
2. For each case:
   a. Find existing line L where:
      - |L.cases| < max_cases_per_line
      - merge(L.spec, case) is compatible (no single-select conflict)
      - cost_if_add(case, L) is minimized   ← best fit variant
   b. If no such line exists: open new line, assign case to it
3. Return initial line assignment
```

**Why heaviest first:** packs the expensive cases early when lines are empty, reducing waste from forced new lines later. Standard FFD heuristic adapted for our cost model.

**Compatibility fast-reject:** before computing merge cost, check single-select column conflicts in O(1) using a precomputed key per line (same `single_select_key` from current code).

**Expected output:** roughly `ceil(N / L)` lines, likely suboptimal but valid.

---

### Module 4: Move Operators

Four move types, applied in a main loop. Each move is evaluated as a cost delta before being applied.

#### Move 1: TRANSFER
Move one test case from line A to line B.

```
delta = cost_if_add(case, B) - cost(B)
      + cost_if_remove(case, A) - cost(A)
      
constraint: |B.cases| < max_cases_per_line
            merge(B.spec, case) is compatible
            |A.cases| > 1  (don't empty a line — or allow and garbage-collect)
```

Best case: case fits on B with zero marginal cost (B's spec already covers it). Worst case: opens a new RU slot.

#### Move 2: SWAP
Exchange case `i` from line A with case `j` from line B.

```
delta = cost_if_swap(A, remove=i, add=j) - cost(A)
      + cost_if_swap(B, remove=j, add=i) - cost(B)
      
constraint: both swapped cases compatible with their new lines
            capacity unchanged (swap is always size-neutral)
```

This is the most powerful move — it can reduce cost on both lines simultaneously. O(N²) to find the best swap naively; use random sampling in practice.

#### Move 3: LINE MERGE
Merge all cases from line B into line A, delete line B.

```
delta = cost(merge(A.spec, B.spec)) - cost(A) - cost(B)

constraint: |A.cases| + |B.cases| ≤ max_cases_per_line
            merged spec is compatible
```

Strictly reduces line count. Accept whenever delta ≤ 0. This naturally minimizes lines as a secondary effect.

#### Move 4: LINE SPLIT
Split line A into two lines A' and A''.

```
Only triggered when: cost(A) is high AND a split reduces total cost
Strategy: partition A.cases into two groups minimizing cost(A') + cost(A'')
```

This is expensive (combinatorial partition) so use a heuristic: sort cases on line A by their "contribution" to the most expensive column, then split at the median. Apply only when line A's cost exceeds a threshold.

Split increases line count but can dramatically reduce cost if one case is forcing an expensive RU slot for all others on the line.

---

### Module 5: Search Strategy

**Outer loop: Large Neighborhood Search (LNS)**

```python
def local_search(
    initial_assignment: list[Line],
    cases: list[TestCase],
    weights: EquipmentWeights,
    time_limit: float,
    temperature_start: float = 2.0,
    temperature_end: float = 0.01,
    cooling_rate: float = 0.995,
) -> list[Line]:

    current = initial_assignment
    best = deepcopy(current)
    best_cost = total_cost(best)
    T = temperature_start

    while time_remaining():
        move = select_move(current)        # see move selection below
        delta = evaluate_move(move)
        
        if delta < 0 or random() < exp(-delta / T):
            apply_move(current, move)
            if total_cost(current) < best_cost:
                best = deepcopy(current)
                best_cost = total_cost(current)
        
        T *= cooling_rate
        
        # Periodic restart from best
        if iterations % restart_interval == 0:
            current = deepcopy(best)
            T = temperature_start * 0.5   # warm restart

    return best
```

**Move selection policy:**

```
Every iteration, pick move type by adaptive probability:
  - TRANSFER:    40% base weight
  - SWAP:        40% base weight  
  - LINE_MERGE:  15% base weight
  - LINE_SPLIT:   5% base weight

Adaptive: increase weight of move type that produced the last improvement.
```

**Candidate selection within move type:**

Don't try all O(N²) pairs every iteration. Use **restricted candidate lists**:

```python
# For TRANSFER: pick random case, try K=10 candidate lines (random sample)
# For SWAP: pick random pair of lines, try random case from each
# For MERGE: pick random pair of lines (biased toward smallest lines)
# For SPLIT: pick the highest-cost line
```

This gives O(1) move evaluation per iteration, allowing millions of iterations in the time budget.

---

### Module 6: Validation

Adapted directly from current `validate_solution`. Key checks:

```python
def validate_assignment(
    lines: list[Line],
    cases: list[TestCase],
    requirement_columns: list[str],
    support: RuBandSupport,
    max_cases_per_line: int,
) -> None:
    # 1. Every case appears on exactly one line
    # 2. Every line has ≤ max_cases_per_line cases
    # 3. Every case's requirements are covered by its line's spec
    # 4. Every spec is RU-band compatible
    # 5. No single-select column has multiple concrete values
```

Run validation once before output. No delta enforcement (lines are merged specs, not bounded by the single-token delta rule from the original solver).

---

### Module 7: Output Format

New output CSV, one row per physical line:

```
line_id, spec_id, line_cost, equipment_count, du_count, ru_count, ue_count,
covered_tc_ids, covered_count, solve_status,
<all requirement columns>
```

Where `spec_id` groups lines that share the same spec (multiple lines, same spec = same test setup replicated). Summary metrics printed to stdout:

```
total_lines=12
unique_specs=4
total_cost=38.5
max_line_cost=6.0
runtime_seconds=14.3
initial_cost=61.0
improvement=37.8%
```

---

### CLI

```
python solve_lines.py \
  --input input.csv \
  --output output_lines.csv \
  --ru-band-support ru_band_support.csv \
  --max-cases-per-line 250 \
  --du-weight 1.0 \
  --ru-weight 1.0 \
  --ue-weight 1.0 \
  --time-limit 300 \
  --initial-strategy greedy \
  --temperature-start 2.0 \
  --cooling-rate 0.995 \
  --restart-interval 10000 \
  --seed 42
```

---

### Implementation Order

```
Step 1  cost.py + EquipmentWeights           (30 min, fully testable in isolation)
Step 2  LineState with lazy recompute        (1 hr, unit test add/remove/cost)
Step 3  Greedy initial solution              (1 hr, verify all cases assigned, valid)
Step 4  TRANSFER move + acceptance loop      (1 hr, already useful at this point)
Step 5  SWAP move                            (1 hr)
Step 6  LINE_MERGE move                      (30 min)
Step 7  Simulated annealing temperature      (30 min)
Step 8  LINE_SPLIT move                      (1 hr, most complex)
Step 9  LNS restart logic                   (30 min)
Step 10 Validation + output                  (1 hr)
Step 11 Test suite (mirror test_solve_test_lines.py structure)  (2 hr)
```

Total: ~10 hours of focused implementation. Steps 1–6 already give a working solver; steps 7–11 add quality and robustness.

---

### What to Reuse vs Replace

| Current | New | Action |
|---|---|---|
| `parse_cell`, `render_cell` | same | **reuse as-is** |
| `merge_column`, `merge_cases` | same | **reuse as-is** |
| `covers_column`, `coverage_delta` | same | **reuse as-is** |
| `equipment_count` | extended with weights | **extend** |
| `load_cases`, `load_ru_band_support` | same | **reuse as-is** |
| `validate_support_references` | same | **reuse as-is** |
| `spec_has_compatible_ru_bands` | same | **reuse as-is** |
| `Candidate` generation | **eliminated** | replace with LineState |
| `solve_with_ortools` | **eliminated** | replace with local search |
| `greedy_set_cover_hint` | adapted | becomes initial solution |
| `write_output` | **replaced** | new line-oriented output |

---

Ready to implement? I'd suggest starting with Steps 1–3 to get a working (if unoptimized) end-to-end pipeline first, then layering in the search.