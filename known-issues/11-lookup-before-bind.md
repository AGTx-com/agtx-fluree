# 11. The planner runs a lookup before the BIND that fills in its key

Found on 26 November 2026 by the policy-reuse engine prototype. The same day it was confirmed on the stock v4.2.9 release with an ordinary query, no policy involved. **Not fixed on `main`.** A prototype fix exists on the `policy-reuse/memo-spike` branch, behind the switch `FLUREE_POLICY_BIND_ORDER`, limited to policy read conditions. Not reported to Fluree.

**Severity:** cost only. Answers are correct; the planner takes a long detour to reach them. Same class as bug 9.

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

## Effect in policies (measured on our build)

A read policy evaluates its condition once per fact, so the scan repeats for every fact a read touches. In AGTx's `va:VerificationReadPolicy`, the reader lookup had this shape. At 1,000 rows, reads as the user took 3.5–26 s. With the prototype fix alone they took 0.7–1.4 s, and they returned identical rows. The cost per check had grown with the number of users.

## Proposed fix

The planner keeps a triple, or a constant-graph `GRAPH` block, after an earlier-written `BIND` when the pattern reads that `BIND`'s variable in subject or object position, and the `BIND`'s own inputs are available at that point. BINDs never move. The prototype also covers plain `UNION` consumers.

**Before this goes on `main` as an always-on bug fix:**
- **Ordinary queries.** The prototype applies it to policy conditions only. Three cases need tests:
  - strict BIND-error mode, where the BIND now runs even if the lookup would have found nothing;
  - `LIMIT` without `ORDER BY`, where which rows come back can change;
  - cost, where holding a cheap scan behind a large BIND input could be slower.
- **Tests first:**
  - the shape above, timed flat;
  - decision equivalence on the policy test groups;
  - the three cases above.

## Reproduction

[`query_repro.py`](11-lookup-before-bind/query_repro.py): `python3 -I query_repro.py <fluree-binary>`. It needs no server and uses fresh temporary ledgers.
