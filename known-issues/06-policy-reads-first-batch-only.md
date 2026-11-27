# 6. A policy condition is decided on its first result batch only

First fixed in a private engine patch for an earlier release, where a `GRAPH` or `UNION` branch with an empty first batch had denied a fact; the date first found is not recorded. A first rerun on the stock v4.2.9 release (`3b8d19e649a6`), on 23 November 2026, did not reproduce it in 14 small shapes; a deeper run the same day did, with an ordinary rule that refuses every identity past the first 1,000 that satisfy it. **Fixed by `22d4ac5`** on `main` (`policy: let f:query pass when any result batch has a row`). Not reported to Fluree.

**Severity: wrong refusals.** An ordinary rule allows user 999 and refuses user 1,000. It only refuses: an empty first batch can turn a true condition false, never the reverse.

**Cause in brief.** The policy executor asks the condition's operator tree for one batch of rows and denies the fact if that batch is empty. Batches hold up to 1,000 rows, and some plans return an empty first batch before a later batch with the matching row: for example a three-triple star over many subjects, filtered afterwards to the requesting identity. Fluree's docs say a fact is permitted when the query returns at least one row. The fix reads batches until one has a row or the tree is exhausted.

## What goes wrong

1,200 users, every one a `User` in a department with level 5, and one policy for `f:view` and `f:modify`:

```sparql
ASK { $identity <urn:x:kind> <urn:x:User> ; <urn:x:dept> ?d ; <urn:x:level> ?lv FILTER(?lv >= 3) }
```

Every user satisfies it, and the same `ASK` run as an ordinary query with the user written in answers true for each. As policy on stock, users 0 and 999 may write and read; users 1,000 and 1,199 are refused both. With 900 users, all are allowed.

## Scope, measured

"Fixed" is the fork's full-patch build, which carries this fix.

**The minimal case** ([`first_batch_minimal_repro.py`](06-policy-reads-first-batch-only/first_batch_minimal_repro.py), CLI `--direct`):

| Users in the ledger | User 0 | User 999 | User 1,000 | Last user |
|---|---|---|---|---|
| Expected | allowed | allowed | allowed | allowed |
| 1,200, stock | allowed | allowed | **refused** | **refused** |
| 900, stock | allowed | n/a | n/a | allowed |
| 1,200, fixed | allowed | allowed | allowed | allowed |

**Breadth** ([`first_batch_breadth_repro.py`](06-policy-reads-first-batch-only/first_batch_breadth_repro.py)). Each condition is checked against its own answer as an ordinary query, as a read and as a write policy, in SPARQL and JSON-LD, in four index states (unindexed, after `fluree index`, after `fluree reindex`, and after `fluree index` plus one newer commit). On stock 172 of 500 decisions contradict the query (Part A 56 of 112, Part B 114 of 386, Part C 2 of 2); on the fixed build 0 of 500. Every stock contradiction is a refusal of a true condition.

| Shape (abridged) | Stock | Fixed |
|---|---|---|
| B1: identity star, 3 triples, `FILTER(?lv >= 3)`, 3,000 users | positions 1,000 and 2,999 refused in all 8 state/action combinations | right |
| B1 as JSON-LD | same as B1 | right |
| B2: document star `$this kind Doc ; owner $identity ; tier ?t FILTER(?t < 3)` | the owner reads the tier of exactly 1,000 of his 3,000 documents | 3,000 of 3,000 |
| B3: 3-triple star with `BIND`, no `FILTER` | same as B1 | right |
| B4: `BIND(<g:big> AS ?g) GRAPH ?g { $identity may true }` | same as B1 | right |
| B10, B11: 3-triple star with a range `FILTER`, or a constant object and `FILTER(?d != …)` | same as B1 | right |
| A1, A2: `VALUES ?g { … } GRAPH ?g { $identity … }` (small data) | refused in all 8 | right |
| A3, A4: `VALUES ?st { "active" … } $identity memberOf ?ws {\| status ?st \|}` | refused in all 8 | right |
| Controls B5 (no `FILTER`), B6 (two-triple star), B7 (constant `GRAPH`), B8, B9; A5–A11 | right | right |
| Part C: a reader's own `FILTER NOT EXISTS { ?d hold true }` under a wrongly refused view policy on `hold` | **lists the held document** | right |

Part C is the one wrong positive: the policy hides the hold flag from the reader although its condition is true, so his own query for "documents without a hold" lists a held document. It is not a grant on a false condition.

**`VALUES` with `GRAPH ?g`** ([`values_graph_first_batch_repro.py`](06-policy-reads-first-batch-only/values_graph_first_batch_repro.py), small data): `ASK { VALUES ?g { <urn:grp:4> } GRAPH ?g { $identity <urn:x:may> true } }` is refused as a condition while the same query answers true. Rob's fact is in two graphs, `<urn:g:b>` and `<urn:grp:4>`, which an unbound `GRAPH ?g` visits in that order. Restricting the rule to `<urn:g:b>` is allowed, to `<urn:grp:4>` refused, and listing both allowed. That shows it is this bug: the `GRAPH` operator returns one batch per named graph, so with the `VALUES` on top a match only in a later graph comes after an empty batch. Stock: 6 contradictions; fixed: 0.

**What it takes, observed:** three or more triples on the same subject plus a `FILTER` or `BIND` and more than 1,000 subjects that satisfy the star, with the requested subject outside the first 1,000; or a written `VALUES` above an operator that returns more than one batch (a `GRAPH ?g`, or the annotation chain, which returned one batch per base edge here). The boundary was exactly 1,000 in every state.

**Not triggered:** 14 small shapes with the match in a later `UNION` branch, a later `GRAPH` or a later seed row; `VALUES` above `UNION`, a property path, a two-triple star or a sub-`SELECT` (A8–A11) on small data, which were not tried with data large enough to split their output.

**Not run:** BM25, vector, geo and S2 conditions; Cypher conditions; anything but the CLI `--direct` path.

## Cause

**In plain terms:** the policy check reads only the first page of results.

**In the source** (stock v4.2.9):
- `QueryPolicyExecutor::run_existence_check` (`fluree-db-query/src/policy/executor.rs:520`) opens the operator tree and calls `next_batch` once (`:575-584`). `Ok(Some(batch))` with an empty batch is taken as "no solution".
- The default batch size is 1,000 rows (`fluree-db-query/src/context.rs:656`).
- A star of three or more triples with a `FILTER` or `BIND` plans as a `PropertyJoinOperator` with no upstream seed (`fluree-db-query/src/execute/where_plan.rs:1201-1202`, width threshold at `:1073`), and the block's `VALUES`, including the one the executor injects for `$identity` and `$this`, goes on top of it (`:1731-1733`). The property join returns at most one batch of subjects at a time (`fluree-db-query/src/property_join.rs:1470-1474`); the injected `VALUES` then keeps only the requested subject, so the first batch is empty unless that subject is among the first 1,000.
- With a written `VALUES` above a `GRAPH ?g`, `GraphOperator` returns one batch per named graph, so a match in the second graph comes after an empty first batch.

Absence tests inside a condition read their inner side to the end (`EXISTS`/`NOT EXISTS`, `MINUS`, `OPTIONAL`), and every row in a batch is a complete solution, so an empty first batch can only turn a true condition false.

## What the standards say

- **Fluree's docs.** `docs/security/policy-model.md`, the `f:query` row: "The targeted flake is permitted when the query returns at least one row." Not "at least one row in the first batch".
- **SPARQL 1.1 Query, section 16.3 (`ASK`).** `ASK` answers whether the pattern has any solution. Batching is an execution detail and must not change the answer; here the same condition answers true as a query and false as a policy.

## The fix

`22d4ac5` on `main`, in `run_existence_check` (`fluree-db-query/src/policy/executor.rs`): read batches until one has a row (allow) or the operator is exhausted (deny). An empty batch is skipped; an error still fails the check.

**Cost:** a false condition now reads every batch instead of the first. That is the price of the correct answer, but Fluree advises keeping `f:query` cheap because it runs once per fact (`docs/security/policy-in-queries.md`). [Bug 9](09-policy-check-drops-row-limit.md)'s fix later gave the check a one-row budget, which keeps this loop.

No engine test in the commit covers it; the reproduction scripts are the evidence. Bug 9's commit added `policy::executor::tests::a_false_condition_still_reads_every_batch`, which fails with this fix's loop reverted.

## Workaround

None general. In the tests, two-triple stars and stars without a `FILTER` or `BIND` decided correctly, and so did `BIND(<iri> AS ?g) GRAPH ?g { … }` in place of `VALUES ?g { <iri> } GRAPH ?g { … }` on small data.

## History

- **Before 22 November 2026:** fixed in a private engine patch for an earlier release, for a `GRAPH` or `UNION` branch that yields an empty first batch; the date first found is not recorded.
- **22 November 2026:** ported to v4.2.9.
- **23 November 2026:** a first stock rerun with 29 query shapes did not reproduce the first-batch case (14 shapes put the match in a later branch, graph or seed row), but found the `VALUES … GRAPH ?g` refusal (8 of 8 cases). A deeper run the same day showed that refusal is this bug, found the 1,000-subject boundary with an ordinary rule, and counted 172 of 500 wrong decisions on stock. Committed to this fork as `22d4ac5` on `main` (also on `full-patch`).
- **25 November 2026:** [bug 9](09-policy-check-drops-row-limit.md)'s fix adds a row budget to the same check and a test that guards this loop.
- **26 November 2026:** this record written.

## Related

- [Bug 9](09-policy-check-drops-row-limit.md): the same existence check drops the one-row limit an `ASK` is planned with.
- [Bug 4](04-named-graph-annotation-lookups.md) was at first taken for this bug in server mode; it is a separate defect.

## Reproduction

```bash
python3 -I known-issues/06-policy-reads-first-batch-only/first_batch_minimal_repro.py <fluree-binary> [users] [output.json]
python3 -I known-issues/06-policy-reads-first-batch-only/first_batch_breadth_repro.py <fluree-binary> [output.json]
python3 -I known-issues/06-policy-reads-first-batch-only/values_graph_first_batch_repro.py <fluree-binary> [output.json]
```

All three use the CLI in `--direct` mode on fresh temporary ledgers; no server.
- `first_batch_minimal_repro.py` (default 1,200 users): stock prints `WRONG` for users 1000 and 1199 and `users decided against the condition's own answer: 2 of 4`, and exits 1. The fix prints `0 of 4` and exits 0. With `900` as the second argument stock is right.
- `first_batch_breadth_repro.py`: stock prints `cases where the policy decision contradicts the query answer: 172` and exits 1; the fix prints 0.
- `values_graph_first_batch_repro.py`: stock prints six `INCONSISTENT` rows and exits 1; the fix prints none.

Recorded outputs, 23 November 2026: minimal on [stock](06-policy-reads-first-batch-only/results-minimal-stock-4.2.9.json) and the [full-patch build](06-policy-reads-first-batch-only/results-minimal-full-patch.json); breadth on [stock](06-policy-reads-first-batch-only/results-breadth-stock-4.2.9.txt) and [full-patch](06-policy-reads-first-batch-only/results-breadth-full-patch.txt); `VALUES` with `GRAPH ?g` on [stock](06-policy-reads-first-batch-only/results-values-graph-stock-4.2.9.txt) and [full-patch](06-policy-reads-first-batch-only/results-values-graph-full-patch.txt).
