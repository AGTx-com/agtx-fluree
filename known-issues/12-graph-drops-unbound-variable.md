# 12. A GRAPH block drops a variable the row before it left unbound

Found on 26 November 2026 while fixing bug 11 (`b13dc10`). Confirmed the same day on the stock v4.2.9 release (`3b8d19e649a6`), on the fork's full-patch build and on a build of `b13dc10`, which does not change it. **Not fixed.** Not reported to Fluree.

**Severity:** wrong answers. A solution loses a binding it should have. A policy that reads the dropped variable after the block sees it unbound (see [Who is affected](#who-is-affected)).

**Cause in brief.** A `GRAPH` block runs once per incoming row and joins its own solutions to that row. When the row already has a column for one of the block's variables, Fluree copies the row's value into the result, even when that value is unbound and the block bound the variable. SPARQL's join keeps the block's value: an unbound variable is compatible with any value.

## What goes wrong

The row reaching the `GRAPH` block leaves `?reader` unbound, and the block binds `?reader` itself:

```sparql
SELECT ?reader ?person WHERE {
  OPTIONAL { <urn:x:a> <urn:x:none> ?reader }
  GRAPH <urn:g:shared> { ?reader <urn:x:person> ?person }
}
```

SPARQL answers `(u:1, p:1)` and `(u:2, p:2)`. Fluree returns `p:1` and `p:2` with no `?reader`.

Two ways the row gets there:
- an OPTIONAL that found nothing, written before the block and sharing the variable (its left-join barrier keeps the block after it);
- a BIND whose expression errors, so the variable is unbound on that row: `VALUES ?v { "x" } BIND(IRI(?v + 1) AS ?reader)`, with the block planned after the BIND.

## Scope, measured

Each case ran on the three builds above, before and after `fluree reindex`. All builds gave identical results. Full output: [stock](12-graph-drops-unbound-variable/results-stock-4.2.9.txt), [full-patch build](12-graph-drops-unbound-variable/results-patched-4.2.9.txt), [`b13dc10`](12-graph-drops-unbound-variable/results-b13dc10.txt). The reworked bug 11 fix, `738a4cc`, also leaves it unchanged: [`738a4cc`](12-graph-drops-unbound-variable/results-738a4cc.txt).

| Case | Expected | Got |
|---|---|---|
| OPTIONAL found nothing, then `GRAPH <iri>` | u:1 p:1; u:2 p:2 | - p:1; - p:2 |
| BIND errors, then `GRAPH <iri>` | u:1 p:1; u:2 p:2 | - p:1; - p:2 |
| BIND errors, then `GRAPH ?g` | u:1 p:1; u:2 p:2 | - p:1; - p:2 |
| BIND errors, then a UNION with a `GRAPH` branch | u:1 p:1; u:2 p:2 | - p:1; - p:2 |

**Controls, all correct:**
- the same rows followed by a default-graph triple that binds `?reader`, after the OPTIONAL and after the erroring BIND;
- a BIND that binds `?reader`, then the `GRAPH <iri>` block.

A VALUES column that is all `UNDEF` did not trigger it in a probe, probably because the planner drops such a column before planning (`drop_undef_values_columns`); not traced.

**Not run:** JSON-LD and Cypher spellings; a partly-`UNDEF` VALUES column; R2RML graph sources, which take the batched path.

## Cause

In `GraphOperator::next_streamed` (`fluree-db-query/src/graph.rs`, the per-row arm around line 679 on `main`), each output column that belongs to the parent schema is filled by copying the parent row's binding into every inner solution:

```rust
Ok(if i < parent_len {
    // Copy the parent binding into every row.
    vec![parent_row[i].clone(); kept.len()]
```

The inner subplan was seeded with the parent row, so when the parent's value is bound, the inner value equals it and the copy is harmless. When the parent's value is unbound, the inner scan runs with the variable free and binds it, and the copy overwrites that binding with the parent's unbound value. Native graphs always take this per-row arm. The batched arm, used only for R2RML graph sources, was not traced.

## What the standards say

SPARQL 1.1 section 18.5 defines Join by merging compatible solutions. A solution that leaves a variable unbound is compatible with one that binds it, and the merged solution takes the bound value. Fluree's OPTIONAL merge already follows this (`fluree-db-api/tests/it_optional_after_union.rs`).

## Proposed fix

In the per-row arm, take the inner solution's value for a parent column when the parent's binding is unbound and the inner batch carries that column. Keep the parent's value otherwise: the inner value equals it then. A `Poisoned` parent binding (Cypher's null) must stay poisoned, matching nothing.

Tests, written first:
- the four cases above, before and after indexing, in SPARQL and JSON-LD;
- the controls;
- a bound parent value (the inner value must equal it, so nothing changes).

## Workaround

Make the variable bound on every row before the block, for example `BIND(COALESCE(?keyAccount, $identity) AS ?reader)`; or bind a fresh variable inside the block and test compatibility yourself: `GRAPH <g> { ?r <urn:x:person> ?person } FILTER(!BOUND(?reader) || ?r = ?reader)`. The second form was not run.

## Who is affected

A query or policy condition in which a `GRAPH <iri>` or `GRAPH ?g` block binds a variable that an earlier `OPTIONAL` (written before the block and sharing the variable) or an erroring `BIND` can leave unbound, and that reads the variable afterwards, in the projection or in a later pattern or `FILTER`. Not affected: a `GRAPH` block whose variables are bound on every incoming row (for example by `COALESCE` with a constant or `$identity` fallback), and default-graph triples.

## History

- **26 November 2026:** found while writing bug 11's first fix (`b13dc10`), whose hold could route a plan through this path. Confirmed the same day on stock, on the full-patch build and on `b13dc10`. Recorded as `21ba453` on `fix/bug-11-bind-order` and as `562a685` on `main`; the run on `738a4cc` and the completed record added in `a82e1bb`. Not fixed.

## Related

[Bug 11](11-lookup-before-bind.md)'s fix (`5b5f18d` on `main`, first written as `b13dc10`) avoids sending new plans through this path: a lookup with a `GRAPH` block waits for its `BIND` only when every pattern that could run before it binds its variables on every row.

## Reproduction

[`query_repro.py`](12-graph-drops-unbound-variable/query_repro.py): `python3 -I query_repro.py <fluree-binary>`. It needs no server, uses a fresh temporary ledger, and exits non-zero while the bug is present.
