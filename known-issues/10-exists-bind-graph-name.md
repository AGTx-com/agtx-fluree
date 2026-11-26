# 10. An EXISTS whose BIND computes its GRAPH name never matches

Found on 26 November 2026, during the policy-reuse engine work on the `policy-reuse/engine` branch. Confirmed the same day on the stock v4.2.9 release and on the full-patch build. **Not fixed.** Not reported to Fluree.

**Severity: report privately.** The `NOT EXISTS` form makes a policy allow what it should refuse, as bugs 1 to 4 do. Keep this record internal until Fluree gives a private channel.

**Cause in brief.** Fluree evaluates this kind of check once for all rows instead of once per row. It has a guard that forces per-row evaluation for a related case, a graph name that comes from outside the check, but the guard doesn't cover a graph name computed inside the check. Fluree's code points to its own issue #1443 for that related case. The fix is to extend the guard to this shape. Details under [Cause](#cause) and [Proposed fix](#proposed-fix).

## What goes wrong

The trigger is a `FILTER EXISTS` or `FILTER NOT EXISTS` whose inner patterns do two things:
- they compute a named-graph IRI with `BIND`, from a value;
- the `GRAPH` block they name with it matches that same value.

```sparql
PREFIX ex: <http://example.org/ns/>
SELECT ?u ?d WHERE {
  VALUES ?u { ex:bob ex:alice }
  ?d ex:workspace ?w
  FILTER EXISTS {
    BIND(IRI(CONCAT("http://example.org/members/", STRAFTER(STR(?w), "http://example.org/ns/"))) AS ?g)
    GRAPH ?g { ?u ex:memberOf ?w }
  }
}
```

Whatever the data:
- `EXISTS` is false for every row;
- `NOT EXISTS` is true for every row.

The same happens in a policy's `f:query`, where `$value` takes the place of `?w` and `$identity` the place of `?u`.

## Scope, measured

Each case ran on the stock v4.2.9 release (`fluree 4.2.9`, the official binary) and on our full-patch build, both before and after `fluree reindex`. The two builds gave identical results. Data:
- `ex:doc1` belongs to `ex:ws1`, and `ex:doc2` to `ex:ws2`;
- bob's ws1 membership is in graph `members/ws1`;
- alice's ws2 membership is in `members/ws2`;
- alice's ws1 membership is recorded only in `members/ws2`. It is a foreign value: a check that names the workspace's own graph must not count it.

| Case | Expected | Got |
|---|---|---|
| Ordinary query, `EXISTS` (above) | alice doc2; bob doc1 | no rows |
| Ordinary query, `NOT EXISTS` | alice doc1; bob doc2 | all four rows |
| View policy on `ex:workspace`, `ASK { FILTER EXISTS { BIND(… $value …) AS ?g) GRAPH ?g { $identity ex:memberOf $value } } }` | bob sees doc1, alice doc2 | both see nothing |
| The same policy with `FILTER NOT EXISTS` (hide a workspace's documents from its members) | bob sees doc2, alice doc1 | **both see both documents** |

**Controls, all correct on both builds:**
- the same `BIND` and `GRAPH`, compared after the fact: `BIND(… AS ?expected) GRAPH ?g { … } FILTER(STR(?g) = ?expected)`, in a query, an `EXISTS` policy and a `NOT EXISTS` policy;
- the same `BIND` and `GRAPH` as a plain policy `ASK` with no `FILTER EXISTS` wrapper;
- `GRAPH ?g` with no `BIND`.

**Not run:**
- write (`f:modify`) checks. They share the existence check, so they are very likely affected the same way;
- JSON-LD and Cypher spellings of the shape;
- other functions in the `BIND`.

## Cause

**In plain terms:** Fluree evaluates this kind of check once for all rows instead of once per row. It has a guard that forces per-row evaluation for a related case, a graph name that comes from outside the check, but the guard doesn't cover a graph name computed inside the check. Fluree's code points to its own issue #1443 for that related case. The fix is to extend the guard to this shape.

**In the source:** the cause is in planning. `choose_exists_strategy` (`fluree-db-query/src/execute/where_plan.rs:547–592` on `main`) picks a semijoin for this `EXISTS`:
- The inner patterns produce `?u` and `?w` through the `GRAPH` triple.
- They read no outer value they don't produce. So the strategy keys a semijoin on `(?u, ?w)` (`:590`). The semijoin evaluates the inner patterns once, without the outer row, builds the set of keys and probes each row against it.

Fluree already falls back to per-row evaluation when a `GRAPH` variable is bound by the outer side. That is `ExistsFallbackReason::GraphVarCorrelated` (`:484–496`, `:585–588`, upstream issue #1443): a standalone `GRAPH ?g` binds raw IRIs that never hash-match the encoded subjects of the outer scan. Here the `GRAPH` variable is bound inside the inner patterns, by the `BIND`. `has_outer_correlated_graph_var` (`:500`) therefore returns false, and the semijoin is used.

The build yields no key for any row: the policy-reuse branch's work counters record zero build rows for this condition. The exact step inside the build that drops the rows has not been traced. Per-row evaluation decides correctly: the plain `ASK` policy above is the same condition evaluated with the row's values in place.

## What the standards say

SPARQL 1.1 defines `EXISTS` by substitution (section 18.6, `substitute`): the pattern is evaluated with the current row's values in place. With that substitution, the `BIND` computes the row's own graph name and the pattern matches. A semijoin is an optimization and must give the same answer. The planner's own fallbacks exist for exactly the shapes where it cannot.

## Proposed fix

Route this shape per row, as `GraphVarCorrelated` does. In `choose_exists_strategy`, fall back to the per-row `ExistsOperator` in either of these cases:
- a `GRAPH ?g` inside the inner patterns names a variable that an inner `BIND` also binds;
- more generally, a `BIND` reads a key variable that only a later inner pattern produces.

Tests, written first:
- the two shapes in this record, as engine tests for an ordinary query and for a view policy;
- each in `EXISTS` and `NOT EXISTS`, before and after indexing;
- plus the compare-after control.

**Cost of the fix:** these checks run per row instead of building once. For a policy condition over one fact, that is the cheaper plan anyway.

## Workaround

Either of these decides correctly on both builds:
- compute the expected name, then compare it after `GRAPH ?g`: `FILTER(STR(?g) = ?expected)`;
- bind the `BIND`'s input from an earlier pattern before computing the name.

## Downstream check

On 26 November, none of the policies we deploy used this shape. Every policy that computes a graph name does it outside the `EXISTS`, or from a value an earlier pattern binds.

## Related

The `policy-reuse/engine` branch leaves this shape out of its `policy_seeded_semijoin` switch. It pins today's empty result in `fluree-db-api/tests/it_policy_tracking.rs` (`seeded_semijoin_keeps_a_bound_graph_correlated_with_its_row`, condition `MEMBER_IN_THE_BOUND_GRAPH`). The fix on `main` should flip that pin when the branch is rebased onto it.

## Reproduction

Each script needs no server and runs `python3 -I <script> <fluree-binary>` on fresh temporary ledgers:
- [`policy_repro.py`](10-exists-bind-graph-name/policy_repro.py): the policy cases;
- [`query_repro.py`](10-exists-bind-graph-name/query_repro.py): the ordinary-query cases.

Each exits non-zero while the bug is present. Saved outputs: [policy, stock](10-exists-bind-graph-name/results-policy-stock-4.2.9.txt), [policy, patched](10-exists-bind-graph-name/results-policy-patched-4.2.9.txt), [query, stock](10-exists-bind-graph-name/results-query-stock-4.2.9.txt), [query, patched](10-exists-bind-graph-name/results-query-patched-4.2.9.txt).
