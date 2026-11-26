# 12. A GRAPH block drops a variable the row before it left unbound

Found on 26 November 2026 while fixing bug 11 (`b13dc10`). Confirmed the same day on the stock v4.2.9 release (`3b8d19e649a6`), on the pinned AGTx build (`64f56de8389c`) and on a build of `b13dc10`, which does not change it. **Not fixed.** Not reported to Fluree.

**Severity:** wrong answers. A solution loses a binding it should have. No policy we deploy reaches this path (see [Downstream check](#downstream-check)). A policy that later reads the dropped variable would see it unbound.

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

Each case ran on the three builds above, before and after `fluree reindex`. All builds gave identical results. Full output: [stock](12-graph-drops-unbound-variable/results-stock-4.2.9.txt), [pinned AGTx build](12-graph-drops-unbound-variable/results-patched-4.2.9.txt), [`b13dc10`](12-graph-drops-unbound-variable/results-b13dc10.txt).

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

## Downstream check

On 26 November, by inspection of the 24 OPTIONALs in the GTO policies (`gto/core/identity-access`, `gto/core/verification-authority`, GTX-Spring `e7bb7a35`), no `GRAPH` block reads a variable an earlier OPTIONAL or BIND can leave unbound:
- the GRAPH blocks after the signer OPTIONAL read `?readerAccount`, which `COALESCE(?keyAccount, $identity)` always binds;
- the others read variables a default-graph triple binds first.

This was not exhaustive, and nothing was run.

## Related

Bug 11's fix (`b13dc10`) avoids sending new plans through this path. A lookup with a GRAPH block waits for its BIND only when every pattern that could run before it binds its variables on every row.

## Reproduction

[`query_repro.py`](12-graph-drops-unbound-variable/query_repro.py): `python3 -I query_repro.py <fluree-binary>`. It needs no server, uses a fresh temporary ledger, and exits non-zero while the bug is present.
