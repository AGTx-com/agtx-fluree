# 14. A policy condition runs again for every fact, even when nothing it reads differs

Found on 25 and 26 November 2026 while measuring policy cost in this fork (the `policy-reuse` branches). Confirmed from the stock source and Fluree's own docs, and reproduced on the stock release (see [Reproduction](#reproduction)). **Not fixed on `main`.** Reuse is being built behind switches on this fork's feature branches (see [The fix](#the-fix)). Not reported to Fluree.

**Severity: cost only.** Decisions are right. Each applicable `f:query` condition runs once for every fact a read or write touches, even when it reads exactly the same inputs from the same state for each of them. A request costs (facts touched) × (one evaluation), so a heavy condition in a bulk write grows steadily into a timeout.

**Bug or design?** Checking every fact is the design, and it is right: facts in one request can get different answers, and each must be judged. Running a condition again when its answer is already known is not designed. It was planned and left undone:
- Stock's own module docs say the results are cached, "by `(restriction_id, subject, identity)` to avoid re-executing the same query for every flake. This is critical for performance when policies use f:query" (`fluree-db-query/src/policy/mod.rs:22-26`).
- The enforcer those docs describe has "Caching (TODO)", "Future versions will cache f:query results", and a commented-out `cache: Arc<PolicyQueryCache>` field (`fluree-db-query/src/policy/enforcer.rs:36-45`). `PolicyQueryCache` is defined nowhere in the tree. The write path has no cache either.
- Fluree's user docs describe the real behaviour and advise around it: `f:query` "runs once per targeted flake" (`docs/security/policy-model.md:353`).
- Stock does reuse the neighbouring work across facts: class lookups per state and subject (`fluree-db-policy/src/evaluate.rs:109`, filled in batch at `fluree-db-transact/src/stage.rs:2699`), the policy set and the identity's properties once per transaction (`docs/security/policy-in-transactions.md:342-343`), and one executor per graph (`stage.rs:3004`). The condition's result is the piece left out.

So it is neither a small slip nor a choice against reuse. It is a planned optimization that was never delivered, and the module docs say it was. That doc mismatch is a plain bug. The missing cache is the cost.

## What goes wrong

A write policy whose condition reads nothing about the fact being written:

```sparql
ASK { $identity <urn:x:adminOf> <urn:x:team> .
      FILTER NOT EXISTS { ?m <urn:x:memberOf> <urn:x:team> ; <urn:x:seats> ?s . FILTER(?s > 1000) } }
```

When the team's administrator writes K facts in one transaction, every one of them binds the same `$identity`, and the condition reads the same pre-state. One evaluation decides all K. Stock runs it K times, and each run reads every member of the team.

The same happens when a condition reads the fact's subject and the write has several facts about one subject: every fact about that subject has the same inputs.

## Scope, measured

Source line numbers in this record are stock v4.2.9 (`9e4b4f8` in this repository, the release tree).

**On the stock release** (`fluree-4.2.9`, release binary `3b8d19e649a6`), with the [reproduction](#reproduction): a team of 5,000 members, one transaction of K facts, median of three runs, measured on 26 November 2026 on an otherwise idle machine. `executed` is stock's own per-policy count, reported with `fluree-track-policy: true`.

| K facts | Condition reads no fact variable (K new subjects) | Condition reads `$this` (K facts, one subject) | Same write, no policy |
|---|---|---|---|
| 1 | 13 ms, executed 1 | 1 ms, executed 1 | 0 ms |
| 10 | 115 ms, executed 10 | 8 ms, executed 10 | 0 ms |
| 100 | 1,150 ms, executed 100 | 78 ms, executed 100 | 1 ms |
| 1,000 | 11,727 ms, executed 1,000 | 914 ms, executed 1,000 | 4 ms |

- **One evaluation per fact,** in both cases, although every fact in each write has the same inputs and one evaluation decides them all.
- **Time grows with K at the cost of one evaluation,** about 11.7 ms and 0.9 ms here; the write itself costs 4 ms at 1,000 facts.
- **Every write was allowed** (HTTP 200), and a non-administrator's write was refused (403), so the policy was enforced throughout.

**In an application's write policies,** on this fork's prototype build (`policy-reuse/memo-spike`, the `full-patch` features plus work counters). With its switches off it evaluates conditions per fact exactly as stock does. The workload grants a role to N members of a group that already has 1,000 members, under the application's write policies as they stood that day: an administrator check plus an integrity check of each membership the write touches. Submit time:

| Write | Per fact, as stock | With write reuse (spike, `FLUREE_POLICY_MEMO_WRITES=1`) |
|---|---|---|
| grant to 1 member | 581 ms | 327 ms |
| grant to 10 members | 5,609 ms | 869 ms |
| grant to 100 members | timed out at 30 s | 6,954 ms |
| a refused grant | refused | refused, same message |

Decisions and post-state memberships were identical in both columns. These are the spike's figures (26 November); the qualified measurements of the ported switches are in progress and will be added here.

- **Reads and writes:** reads filter each flake through `QueryPolicyEnforcer`, which is where the TODO sits; writes check each staged flake in `enforce_modify_policy_per_flake`. Neither keeps a condition's result.
- **Both states:** pre-state and post-state conditions run per fact alike.
- **Every condition language:** SPARQL, JSON-LD and Cypher conditions go through the same executor.

## Cause

**In plain terms:** the engine judges each fact on its own and keeps nothing from one fact's check for the next, so a condition with the same inputs runs again from scratch.

**In the source:**
- `enforce_modify_policy_per_flake` (`fluree-db-transact/src/stage.rs:2994`) loops over the staged flakes (`:3013`) and calls `allow_modify_flake_write_async_detailed` for each (`:3054`). It reuses the executor per graph (`:3004`), not any result.
- `allow_modify_flake_write_async_detailed` (`fluree-db-policy/src/evaluate.rs:758`) selects the applicable restrictions and runs each `f:query` condition through the executor, counting each run with `tracker.policy_executed` (`:854`, `:869`, `:914`). The view path does the same (`:500-666`).
- `QueryPolicyEnforcer` (`fluree-db-query/src/policy/enforcer.rs:36-45`) holds only the policy context; the cache field is commented out.

## What the docs say

- **The module docs** (`fluree-db-query/src/policy/mod.rs:22-26`): results "are cached by `(restriction_id, subject, identity)`". Not true of the code.
- **The enforcer** (`enforcer.rs:36-45`): caching is a TODO.
- **`docs/security/policy-model.md`:** "Cost is proportional to the number of touched flakes × the average per-flake check cost" (`:348`); "Keep `f:query` cheap. It runs once per targeted flake" (`:353`).
- **`docs/security/policy-in-queries.md:253-256`:** the same advice for reads.
- **`docs/security/policy-in-transactions.md:340`:** "Policy checks add a small per-flake cost on top", which holds only for cheap conditions.

## The planned key is not enough

The key the module docs name, `(restriction_id, subject, identity)`, would give wrong answers:
- **for conditions that read the fact's value or operation:** stock binds `?$value` and `?$op` too (`fluree-db-query/src/policy/executor.rs:351-355`);
- **for impure conditions,** such as `NOW()` or `RAND()`. Stock itself refuses to key its query-result cache on a view policy with an `f:query` restriction for exactly this reason (`hash_view_policy`, `fluree-db-policy/src/evaluate.rs:188-195`);
- **across states and graphs:** a pre-state and a post-state condition, or the same condition in two named graphs, read different data;
- **for any further input** an engine binds. This fork binds `?$predicate` and transaction-metadata values.

A safe key holds every value the condition reads, plus the snapshot and graph it reads them from. A condition with an impure function, an external service or a search pattern runs every time. A failed or unfinished evaluation is never kept. Because the key carries everything the condition reads, the reuse cannot change a decision; recognising a condition that reads no fact variable only decides whether reuse is worth trying.

## The fix

Proposed, not on `main`. This fork builds the reuse behind switches, all off by default, on its feature branches, for the `full-patch` build:
- **Reads:** `1fdf569` on `policy-reuse/engine-on-bug11` ("reuse fact-free subplans and lowerings within a read, behind switches"). Reviewed on 26 November: no input that can differ between two facts is missing from the key.
- **Writes:** the write reuse (a result memo and fact-free reuse for write checks) is being ported behind `FLUREE_POLICY_*_WRITES` switches, test-first, after the 26 November spike.

It stays off `main` because it is a new mechanism, not a small correction. Its risk is a security risk, not a cost: a key that misses one input would let a saved "allowed" decide a fact that should be refused. Two points are still open from review: rows kept for reuse are bounded by count, not charged to the request's memory budget, and keys identify the snapshot by its address within one request.

## Workaround

- **Keep conditions cheap,** as Fluree's docs advise: read the identity's own properties rather than traversing the graph.
- **Target the expensive check** with `f:onProperty` or `f:onClass`, so it runs only on the facts that need it.
- **Check only what the write touches.** A condition that re-checks a whole graph's integrity on every fact can usually be narrowed to the subjects or edges the write changes. In the application above, that kind of narrowing was the larger part of the cure.

## History

- **25 November 2026:** found while planning policy-evaluation work on this fork's `policy-reuse/engine` branch. [Bug 9](09-policy-check-drops-row-limit.md) was found and fixed on the way.
- **26 November 2026:** a prototype measured the write reuse (the figures above); the read reuse was committed behind switches and reviewed; the write port started. This record written, with the stock reproduction.

## Related

- [Bug 9](09-policy-check-drops-row-limit.md): a condition worked past its first solution, for every fact.
- [Bug 11](11-lookup-before-bind.md): a condition's lookup scanned every candidate record, for every fact.
- [Bug 6](06-policy-reads-first-batch-only.md): the same existence check read only its first batch.

## Reproduction

Folder [`14-policy-condition-rerun-per-fact/`](14-policy-condition-rerun-per-fact/):

```bash
python3 -I rerun_repro.py <fluree-binary> [port]
```

It starts an in-memory server, seeds a team of 5,000 members, and has the team's administrators write K = 1, 10, 100 and 1,000 facts in one transaction, three runs each: facts about K new subjects under a condition that reads no fact variable, and K facts about one subject under a condition that reads `$this`. A write with no identity gives the stage cost alone, and a non-administrator's write is refused, which shows the policy is enforced. Recorded output on the stock release: `results-stock-4.2.9.txt`.
