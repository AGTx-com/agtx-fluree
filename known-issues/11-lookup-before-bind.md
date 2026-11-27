# 11. The planner runs a lookup before the BIND that fills in its key

Found on 26 November 2026 by the policy-reuse engine prototype. The same day it was confirmed on the stock v4.2.9 release with an ordinary query, no policy involved. **Fixed by `b13dc10`** (`fix(query): plan a pattern after the BIND that fills in its key`), `738a4cc` (`fix(query): hold a lookup only where no other pattern sees the difference`) and `ab36f24` (`fix(query): anchor a held lookup through BINDs and plan strict updates as before`), always on, for every query and policy condition. The fixes' descriptions carry this record. `738a4cc` corrects two plans the review of `b13dc10` found wrong, and `ab36f24` three the re-review of `738a4cc` found (below). Not reported to Fluree.

**Severity:** cost only. Answers are correct; the planner takes a long detour to reach them. Same class as bug 9. The fix is meant to change no answer (except which rows a `LIMIT` without `ORDER BY` returns), and it changes no strict-update outcome: an update's WHERE is planned exactly as before. Its tests compare each shape with today's plan or today's answer.

**Cause in brief.** A query fills in a variable with `BIND`, then looks up a record by that variable. Fluree's planner may move the lookup ahead of the `BIND`, because the lookup shares no variable with the patterns before it. The lookup then runs with the variable unbound and scans every candidate record. The `BIND`'s clobber check afterwards throws away every row except the one wanted. Standard SPARQL evaluates the `BIND` first, so the lookup would see the value.

## Shape

```sparql
SELECT ?person WHERE {
  OPTIONAL { GRAPH <urn:g:keys> { ?key <urn:x:did> "did:example:reader" ; <urn:x:account> ?keyAccount } }
  BIND(COALESCE(?keyAccount, <urn:u:target>) AS ?reader)
  GRAPH <urn:g:shared> { ?reader a <urn:x:User> ; <urn:x:person> ?person }
}
```

Writing the `BIND` inside the `GRAPH` block (the control) keeps the lookup bound, and the time stays flat.

## Measured on stock v4.2.9

The fluree CLI ran in `--direct` mode as the administrator, after `fluree reindex`, with the target user sorting last. Each figure is the median of three, including process start-up (about 15 ms). Full output is in [results-stock-4.2.9.txt](11-lookup-before-bind/results-stock-4.2.9.txt).

| Users | Lookup after the BIND | Control (BIND inside the GRAPH) |
|---|---|---|
| 1,000 | 30 ms | 17 ms |
| 10,000 | 150 ms | 19 ms |
| 40,000 | 642 ms | 21 ms |

Both return the same single row.

## Measured with the fix

The same script on a build of `ab36f24`, with the stock release and a build of `738a4cc` run in the same sitting. The fixed binaries are opt-level-1 dev builds; their control runs about 2 ms slower than the release binary's. Full output, with [`review_repro.py`](11-lookup-before-bind/review_repro.py) and [`rereview_repro.py`](11-lookup-before-bind/rereview_repro.py) beside it, is in [results-fixed-ab36f24.txt](11-lookup-before-bind/results-fixed-ab36f24.txt); the earlier runs are in [results-fixed-738a4cc.txt](11-lookup-before-bind/results-fixed-738a4cc.txt) and [results-fixed-b13dc10.txt](11-lookup-before-bind/results-fixed-b13dc10.txt).

| Users | `ab36f24`: lookup after the BIND | `ab36f24`: control | Stock: lookup after the BIND | Stock: control |
|---|---|---|---|---|
| 1,000 | 10 ms | 10 ms | 14 ms | 8 ms |
| 10,000 | 10 ms | 10 ms | 69 ms | 8 ms |
| 40,000 | 11 ms | 11 ms | 250 ms | 9 ms |

Both shapes are flat, and every run returns the same single row. Fuel, which counts the index leaflets and in-memory rows a query reads, is equal at 1,000 and 10,000 users after the fix (1.002 unindexed, 1.022 indexed), in SPARQL and JSON-LD; on stock it was 3 against 21 unindexed and 13.32 against 124.14 indexed in 64-row leaflets (`fluree-db-api/tests/it_query_bind_order.rs`).

The reviews' cases ([`review_repro.py`](11-lookup-before-bind/review_repro.py), [`rereview_repro.py`](11-lookup-before-bind/rereview_repro.py); fuel from `fluree-db-api/tests/it_query_bind_order.rs`). The `b13dc10` column is from its own sitting.

| Case | Stock | `b13dc10` | `738a4cc` | `ab36f24` |
|---|---|---|---|---|
| C1: `FILTER(?reader != <urn:u:1>)` rows (unindexed / indexed) | 4 / 4 | 0 / 0 | 4 / 4 | 4 / 4 |
| C1: `FILTER(BOUND(?reader))` rows | 5 / 5 | 0 / 0 | 5 / 5 | 5 / 5 |
| I1: 1,000 / 10,000 labels | 8 / 8 ms | 18 / 88 ms | 9 / 10 ms | 11 / 10 ms |
| I1 fuel, unindexed (1,000 / 10,000) | 1.006 / 1.006 | 2.001 / 11.001 | 1.006 / 1.006 | 1.006 / 1.006 |
| I1 fuel, indexed (64-row leaflets) | 1.076 / 1.076 | 12.171 / 112.581 | 1.076 / 1.076 | 1.076 / 1.076 |
| I1b `BIND(STR(?o))`: 1,000 / 10,000 documents | 8 / 8 ms | | 16 / 76 ms | 10 / 10 ms |
| I1b `BIND(STR(?o))` fuel, unindexed | 2.003 / 1.006 | | 3 / 21 | 2.003 / 1.006 |
| I1b `BIND(STR(?o))` fuel, indexed | 2.233 / 1.106 | | 2.181 / 12.591 | 2.233 / 1.106 |
| I1b `BIND(IRI(…))`: 1,000 / 10,000 documents | 8 / 8 ms | | 16 / 78 ms | 10 / 10 ms |
| I1b `BIND(STRLEN(STR(?o)))` rows (unindexed / indexed) | both size-7 documents | | `<urn:doc:7>` only | both |
| N1 UNION: 1,000 / 10,000 items | 8 / 8 ms | | 26 / 1,055 ms | 10 / 9 ms |
| N1 UNION fuel, unindexed / indexed (1,000 → 10,000) | flat | | 2 → 11 / 12.16 → 112.57 | flat |
| N2 strict update, the BIND fails on `<urn:u:2>`'s row | fails | | succeeds, writes `0` | fails |
| N2 strict update, fallback without a role | succeeds, writes nothing | | fails | succeeds, writes nothing |

On stock, the `BIND(STR(?o))` case reads the labels at 1,000 documents and probes them at 10,000, so its work does not grow; `ab36f24` plans it as stock does. Stock's fuel there was measured by planning with every hold off, which is stock's planner.

## Effect in policies (measured on our build)

A read policy evaluates its condition once per fact, so the scan repeats for every fact a read touches. In AGTx's `va:VerificationReadPolicy`, the reader lookup had this shape. At 1,000 rows, reads as the user took 3.5–26 s. With the prototype fix alone they took 0.7–1.4 s, and they returned identical rows. The cost per check had grown with the number of users.

## The fix

A lookup that reads a variable an earlier-written `BIND` fills in stays out of the planner's source race until that `BIND` is placed. It is a positional barrier, like the planner's left-join barrier, so the lookup then competes as before, with the variable bound. BINDs never move. A `BIND` the planner places first anyway (no inputs, or inputs from the seed) changes nothing.

It holds a triple, a `GRAPH <iri>` block, or a `UNION` of such branches, with FILTERs that read only their own list or the seed. It applies only where both orders give the same rows:
- **The BIND binds on every row.** Every `BIND` of the variable is a constant, a variable the seed or an earlier VALUES binds on every row, or a COALESCE with such an argument. Every other pattern that reads the variable (a FILTER, BIND, EXISTS) runs after the first pattern that binds it: today the lookup, held the BIND. With a value on every row they read the same value either way. The shapes this bug was found in, `COALESCE(?keyAccount, $identity)` and `COALESCE(…, <iri>)`, qualify.
- **Not in a strict update.** With strict BIND errors (an update's WHERE), nothing is held, and the group is planned exactly as before. Held, a `BIND` that reads the lookup's variables would meet other rows than it meets today: `BIND(IF(?reader = <urn:u:2>, ?reader + 1, 0) AS ?n)` after the lookup fails today on `<urn:u:2>`'s row, and held it would not; with a fallback that has no role it is the other way round. The planner learns this from `PlanningContext::strict_bind_errors`, which the update WHERE path sets (as does the query runner's one-call entry point when asked for strict errors).
- **No MINUS before it reads the lookup's variables.** A MINUS waits for what is written before it, not for its binders, so it could run before the held lookup.
- **Positions.** The variable read as a subject; as an object only when every `BIND` of it yields an IRI, because a probed literal matches numerics across datatypes while the clobber check compares terms. Never as a predicate, or for a variable an `UNWIND` binds.
- **Anchors.** A lookup is held only when every other source sharing a variable with it is held too. Its variables here include the outputs of every `BIND` and `UNWIND` that reads them, directly or through another, so `BIND(STR(?o) AS ?name) ?doc <label> ?name` anchors it. Otherwise a source that joins it would be placed first, joined to nothing: a scan where today it is a probe. Through a `BIND` that could also change an answer: the `BIND` would then compare its value with the scan's as terms, where today's probe matches numbers across datatypes. A pattern the planner defers as an uncorrelated sub-SELECT's pipeline consumer never uses its hold, and it cannot block this rule: it shares the sub-SELECT's output with the sub-SELECT, which is never held, so the rule releases it (re-review N4; pinned by a test).
- **GRAPH blocks.** A lookup with a `GRAPH` block waits only when every other pattern that binds one of its variables, and the seed, does so on every row. A `GRAPH` block that runs after a row leaving one of its variables unbound drops its own binding of it, which is bug 12.
- **No waiting loop.** The `BIND`'s inputs come from the seed or patterns written before it, and no `BIND` written before the lookup reads anything the lookup or a later pattern binds.
- **Cost guard.** A lookup that statistics price at one row or none keeps its place, so one that finds nothing still skips the `BIND`'s inputs. A `GRAPH` block is priced from its body's statistics here, and a `UNION` by its costliest branch (each branch's estimate is at least one row, so their sum never was). Policy conditions are planned without statistics, so there it does not apply.

**`LIMIT` without `ORDER BY`:** which solutions come back may differ; their number does not.

**What `b13dc10` alone got wrong** (found by its review, corrected by `738a4cc`):
- It held a lookup behind a `BIND` that may leave its variable unbound, such as `COALESCE(?keyAccount)` when the key is missing. Another pattern reading that variable then ran between the `BIND` and the lookup and saw it unbound. `… BIND(COALESCE(?keyAccount) AS ?reader) ?reader <urn:x:role> ?role FILTER(?reader != <urn:u:1>)` returned no rows instead of four, and `FILTER(BOUND(?reader))` none instead of five. A strict update could also fail where it had succeeded.
- It held a selective lookup that another source joined through a second variable. That source then scanned in full: fuel grew with the number of labels, where today it stays flat.

**What `738a4cc` still got wrong** (found by its re-review, corrected by `ab36f24`):
- I1b: the anchor rule saw only variables a source shares with the lookup directly. A source joined through a `BIND` (`BIND(STR(?o) AS ?name) ?doc <urn:x:label> ?name`) still ran first and scanned: fuel 3 against 21 unindexed at 1,000 and 10,000 documents. Through `BIND(STRLEN(STR(?o)) AS ?len) ?doc <urn:x:size> ?len` it dropped `"7"^^xsd:long`, which today's probe matches with the integer 7.
- N1: a `UNION` lookup could never meet the cost guard, so one over two absent predicates waited behind N items (fuel 2 against 11 unindexed at 1,000 and 10,000 items).
- N2: a strict update could fail where it succeeded, or succeed where it failed, through a `BIND` after the lookup that reads its variables.

## A related bug found while fixing this one

On v4.2.9, a `GRAPH` block (`<iri>` or `?g`) that runs after a row leaving one of its variables unbound returns the block's rows without that variable. For example `OPTIONAL { <urn:x:a> <urn:x:none> ?reader } GRAPH <urn:g:shared> { ?reader <urn:x:person> ?person }` returns `?person` with no `?reader`. Default-graph triples and a `UNION` of them keep the binding. It is [bug 12](12-graph-drops-unbound-variable.md), recorded on its own; the bug-11 fix avoids routing new plans through it.

## Reproduction

[`query_repro.py`](11-lookup-before-bind/query_repro.py): `python3 -I query_repro.py <fluree-binary>`. It needs no server and uses fresh temporary ledgers.
