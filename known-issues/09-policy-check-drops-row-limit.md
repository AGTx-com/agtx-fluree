# 9. A policy check drops the one-row limit an existence test is planned with

Found on 25 November 2026 while planning policy-evaluation work in this fork (the `policy-reuse/engine` branch). Confirmed from the stock source and by engine tests on `main`; unlike bugs 1 to 8 it was not reproduced with a run of the stock release, because it changes the work done, not the answer. **Fixed by `34162be`** on `main` (`fix(policy): keep the one-row limit on policy existence checks`), with a test of its known error case in `a04fbf0` (`test(policy): pin the error case a matching first branch skips`). Not reported to Fluree.

**Severity: cost only.** Decisions are right; a check works past its first solution. The clearest case: a `UNION` condition builds and runs every branch after one has matched, for every fact judged. The fix changes no decision except one: an error in work the check now skips no longer surfaces (see [The fix](#the-fix)).

**Cause in brief.** A policy condition is an existence test: one solution decides it. Elsewhere the engine plans that with a limit of one row: SPARQL `ASK` lowers with `LIMIT 1`, and the JSON-LD policy path sets `"limit": 1` on every condition. But the policy path keeps only the condition's `WHERE` patterns and opens their operator tree with no row budget, so no operator learns that one row is enough. The fix gives the tree a one-row budget before opening it, as `LIMIT 1` does for an ordinary query.

## What goes wrong

A condition with two branches:

```sparql
ASK { { $identity <urn:x:role> <urn:x:editor> } UNION { $identity <urn:x:memberOf> ?team . ?team <urn:x:grants> <urn:x:edit> } }
```

For an identity that matches the first branch, an ordinary `ASK` stops after one solution. As a policy condition on stock, the `UNION` builds and drains the second branch too, before it returns anything, and does so for every fact the policy judges.

## Scope, measured

Source line numbers in this record are `main` just before the fix (stock v4.2.9 plus fixes 1 to 8), which matches stock for these lines.

- **Answers are right.** The check still stops at its first non-empty batch, so no decision changes; the cost is work.
- **`UNION`:** for each input row it builds and drains every branch before returning (`fluree-db-query/src/union.rs:324-356`). It has a lever that stops building branches once a row budget is met (`:391-397`); in a policy check it never fires.
- **Batched lanes that size their windows to a row budget** read their full default windows instead:
  - the nested-loop join's batched lanes flush every 100,000 rows without a budget (`FlushSchedule::fixed(BATCHED_JOIN_SIZE)`, `fluree-db-query/src/join.rs:1460`); with a budget of one, its bound-object lane opens at one row (`FlushSchedule::budgeted_in_steps`, `join.rs:1702-1703`) and its other lanes at 1,024, because `FlushSchedule::budgeted` clamps a budget up to `MIN_FLUSH` (`fluree-db-query/src/operator/flush.rs:65-67`); each widens eightfold;
  - the property join's streaming lane opens at one row instead of 1,024 (`FlushSchedule::ramped_from`, `fluree-db-query/src/property_join.rs:1447-1451`);
  - the required side of `OPTIONAL` takes the budget when its switch is on (`optional.rs:2859`), `GRAPH` threads it into its inner plan (`graph.rs:713`), and R2RML scans use it; `BIND` and the dataset operator pass a budget down to these.
- **Every condition language:** SPARQL, JSON-LD and Cypher conditions share the existence check.
- **Reads and writes, both states:** modify checks build the same policy executor (`fluree-db-transact/src/stage.rs:3085`), and pre-state and post-state conditions go through the same check.

**Measured in engine tests** on `main`, written before the fix:

| Test | Before the fix | After |
|---|---|---|
| `union::tests::policy_check_builds_no_branch_after_one_with_a_row`: the policy check's own reader on a two-branch `UNION` whose first branch has a row, counted by the operator's `branch_execs` | 2 branches built | 1 |
| `policy::executor::tests::jsonld_condition_keeps_its_one_row_limit`: a JSON-LD `union` condition through the policy executor, counted by a test-only probe beside `branch_execs` | 2 | 1 |

On the `policy-reuse/engine` branch, whose policy-work counters were rebased onto the fix, a read of four facts under a two-branch SPARQL `UNION` condition (`fluree-db-api/tests/it_policy_tracking.rs`) went from 8 `UNION` branches to 6, from 20 operator trees to 16, from 4 semijoin builds to 2, and from at most 2 rows produced by any one check to 1. A fact that passes both branches now runs one branch.

**Not measured:** wall-clock time, before or after; the batched-lane cases above in any test.

## Cause

**In plain terms:** the policy check runs the condition as a bare pattern, not as an `ASK`, and so loses the "one row is enough" that comes with `ASK`.

**In the source:**
- SPARQL `ASK` lowers with `LIMIT 1` (`fluree-db-sparql/src/lower/ask.rs:51`), and Fluree's planner notes that both query surfaces plan `ASK` that way (`fluree-db-query/src/execute/operator_tree.rs:2466-2467`). The JSON-LD policy path sets `"limit": 1` on every condition (`fluree-db-query/src/policy/executor.rs:339-344`).
- The SPARQL policy hook returns only the `WHERE` patterns, not the lowered query's limit (`fluree-db-api/src/sparql_lang.rs:106-112`), and the JSON-LD path passes on only `parsed.patterns` (`executor.rs:436`).
- `run_existence_check` plans the tree and opens it with no row budget (`executor.rs:555-573`). An ordinary query gets one from its `LIMIT` operator, which hands its limit to the tree before opening it (`fluree-db-query/src/limit.rs:80-85`).

## What the standards say

- **SPARQL 1.1 Query, section 16.3 (`ASK`).** `ASK` tests whether a pattern has a solution; one solution decides it.
- **Fluree's docs.** `docs/security/policy-model.md`: the fact is permitted when the condition's query returns at least one row, so one row is all a check needs. `docs/security/policy-in-queries.md` advises keeping `f:query` cheap because it runs once per fact. Nothing says the policy path should plan differently from `ASK`.

## The fix

`34162be` on `main`, in `fluree-db-query/src/policy/executor.rs`: the new `first_solution_exists` calls `operator.set_row_budget(POLICY_CHECK_ROW_BUDGET)` (a constant, 1) before opening the tree. It is one constant for every condition language, since any condition needs one solution, so a SPARQL `SELECT` condition gets it too. [Bug 6](06-policy-reads-first-batch-only.md)'s loop stays: the budget never ends the read itself, an empty batch is skipped, and a false condition still reads every batch to the end.

Tests in the commit, besides the two above:
- `policy::executor::tests::a_false_condition_still_reads_every_batch` guards bug 6's loop: a row after two empty batches, no row at all, a first branch that misses, no branch matching. It passes before and after, and fails with bug 6's loop reverted.
- `a04fbf0` adds `union::tests::policy_check_skips_an_erroring_branch_after_a_matching_one`, on two-branch unions whose failing branch is an external `SERVICE` (an error at run time): matching then failing gives `Ok(true)` with one branch built; failing then matching gives the `SERVICE` error; missing then failing gives the error with both branches built. Without the budget the first case fails with the error too.

Test groups run after the fix, all passing: `cargo test -p fluree-db-policy -p fluree-db-query` (policy 46; query library 1,889 with 2 ignored, plus the query integration and doc tests) and `cargo test -p fluree-db-api --test grp_policy` (149) at `34162be`; the error-case test with its neighbours, `cargo test -p fluree-db-server --test grp_policy` (95) and `cargo clippy -p fluree-db-query --all-targets -- -D warnings` at `a04fbf0`.

**What else changes:**
- **An error in work the budget now skips no longer surfaces.** A policy evaluation error fails the whole read or write (`fluree-db-query/src/policy/enforcer.rs:176-181`). Work the budget skips (a `UNION` branch after one that matched, input a budget-aware join no longer reads before its first row) used to fail the request when it errored; now the check can finish and allow the fact. `UNION` plans each branch lazily, per input row, so a branch that fails to plan is skipped like one that fails to run. Parse and lowering errors, and errors planning the condition's top-level tree, still always surface.
- **Never fail-open:** the check skips work only after a real solution; when the decision needs the failing work (an earlier branch misses), the error still fails the request. Branch order therefore decides: `{ A } UNION { B }` with A matching and B failing allows, `{ B } UNION { A }` fails. Policy tests should exercise each `UNION` branch on its own.
- **Debug logging:** operators that must not pass a budget on (`FILTER`, `MINUS`, `ORDER BY`, `DISTINCT`, aggregates, subqueries) log a debug-level "row-budget swallowed" event when they receive one, so a check topped by one now logs one such event per evaluation.
- **Switches:** `FLUREE_R2RML_UNION_BUDGET=off` turns off the `UNION` lever and with it most of the saving; the same switch gates `BIND`'s forwarding. `OPTIONAL` and the dataset operator have their own (`FLUREE_R2RML_BUDGET_OPTIONAL`, `FLUREE_R2RML_DATASET_BUDGET`).

**Cost:** less work wherever the first solution comes early. Otherwise the batched lanes take a few more, smaller windows before their first row or their end (opening at one row or 1,024 and widening eightfold, against 100,000 or 1,024 without a budget), the same price `LIMIT 1` pays on an ordinary query. No timing was measured.

## Workaround

None measured. Ordering a `UNION`'s branches does not help on stock, because every branch runs.

## History

- **25 November 2026:** found while planning policy-evaluation work on this fork's `policy-reuse/engine` branch; fixed the same day as `34162be`, test-first, and the error case pinned in `a04fbf0`. The feature branch was rebased onto both.
- **26 November 2026:** this record written. The fix is on `main` only; the `full-patch` branch does not have it.

## Related

- [Bug 6](06-policy-reads-first-batch-only.md): the same existence check read only its first batch.
- [Bug 11](11-lookup-before-bind.md): another policy-condition cost bug, in planning.

## Reproduction

No standalone script: the bug changes work, not answers, so it is reproduced with the fork's own engine tests. On `main`:

```bash
cargo test -p fluree-db-query --lib union::tests::policy_check_builds_no_branch_after_one_with_a_row
cargo test -p fluree-db-query --lib policy::executor::tests::jsonld_condition_keeps_its_one_row_limit
cargo test -p fluree-db-query --lib policy::executor::tests::a_false_condition_still_reads_every_batch
cargo test -p fluree-db-query --lib union::tests::policy_check_skips_an_erroring_branch_after_a_matching_one
```

All four pass on `main`. To see stock's behaviour, delete the line `operator.set_row_budget(POLICY_CHECK_ROW_BUDGET);` in `first_solution_exists` (`fluree-db-query/src/policy/executor.rs`) and rerun: the first two fail with 2 branches built where 1 is expected, the third still passes, and the fourth fails on its first case with the `SERVICE` error.
