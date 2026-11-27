# 13. Shapes taken from another ledger are skipped until the data ledger has registered their IRIs

Found on 26 November 2026 while checking a design in which many data ledgers take their SHACL shapes from one shared ledger. Confirmed the same day on the stock v4.2.9 release (`3b8d19e649a6`) and on the fork's full-patch build, with identical results. **Not fixed.** Not reported to Fluree.

**Severity:** validation silently off. A ledger that takes its SHACL shapes from another ledger accepts data the shapes forbid, with no error or warning, until its own namespace registry happens to contain the shapes' IRIs.

**Cause in brief.** Before validating, Fluree translates the shapes it pulls from the model ledger into the data ledger's terms, and drops every shape triple whose subject, predicate or object IRI the data ledger has not registered. A shape's own node IRIs (`urn:shape:Order`, a named property shape) exist only in the model ledger, so on a ledger that has never used their namespaces every triple of the shape is dropped and the shape does not apply.

## What goes wrong

`ref:main` holds, in graph `<urn:g:shapes>`, a node shape for `urn:x:Order` whose property shape requires one `urn:x:product` that is a `urn:x:Product`, and the value set (`urn:x:widget` and `urn:x:gadget` are products) beside it, as Fluree's [shared value-sets pattern](../docs/guides/cookbook-shacl.md#shared-value-sets-with-shclass) describes. `co:main` enables SHACL and takes its shapes from `ref:main`:

```trig
GRAPH <urn:fluree:co:main#config> {
  <urn:cfg:ledger> a f:LedgerConfig ; f:shaclDefaults <urn:cfg:shacl> .
  <urn:cfg:shacl>  f:shaclEnabled true ; f:shapesSource <urn:cfg:sref> .
  <urn:cfg:sref>   a f:GraphRef ; f:graphSource <urn:cfg:ssrc> .
  <urn:cfg:ssrc>   f:ledger <ref:main> ; f:graphSelector <urn:g:shapes> .
}
```

An order with no product, and one naming `urn:x:unobtainium`, should both be rejected. On a fresh `co:main` both commit.

## Scope, measured

[`shapes_repro.py`](13-cross-ledger-shapes-skipped/shapes_repro.py), one in-memory server per case. Full output: [stock](13-cross-ledger-shapes-skipped/results-stock-4.2.9.txt), [full-patch build](13-cross-ledger-shapes-skipped/results-patched-4.2.9.txt).

| Case | No product (reject) | Widget (pass) | Unobtainium (reject) |
|---|---|---|---|
| Control: the same shapes in `co:main` itself | rejected | passes | rejected |
| Cross-ledger, fresh `co:main` | **passes** | passes | **passes** |
| Cross-ledger, `co:main` has used the data IRIs (`urn:x:product`, `urn:x:Product`, `urn:x:Order`, `urn:x:widget`) | **passes** | passes | **passes** |
| Same, plus an unrelated `urn:shape:Unrelated` | **passes** | passes | **passes** |
| Same, plus both shape node IRIs | rejected | passes | rejected |
| Blank-node property shape, plus the node shape's IRI | rejected | passes | rejected |

So the check is by namespace: `urn:shape:Unrelated` registers `urn:shape:` but not `urn:shape:Order:`, the namespace of the named property shape. Blank-node property shapes survive once the node shape's namespace is known. Once the IRIs are registered, the cross-ledger value set works: `sh:class` sees the products declared in the model ledger's shapes graph.

**Not run:** JSON-LD spellings; `sh:sparql` constraints; a model ledger that changes after the data ledger first validated; whether the ledger-info diagnostics report anything.

## Cause

`ShapesArtifactWire::translate_to_schema_bundle_flakes` (`fluree-db-api/src/cross_ledger/types.rs`) encodes each wire triple with `NamespaceRegistry::lookup_sid_for_iri`, lookup only, and `continue`s past any triple whose subject, predicate or IRI object is not registered on the data ledger. Its doc comment gives the reason: an IRI the data ledger has never seen "couldn't apply to data D doesn't have anyway", and allocating codes for model-only IRIs would churn D's namespaces. That holds for target classes and values. It does not hold for the shape's own node IRIs, which are model-only by design, so dropping their triples removes the shape. `docs/security/cross-ledger-policy.md` documents the silent drop but not this consequence.

## What the standards say

- **SHACL Core.** `sh:minCount 1` on a property the focus node has no value for is a violation, whatever the data graph has seen before. `sh:class` reports a value that is not an instance of the class. Neither depends on whether the data graph has ever used the shape's IRIs.
- **Fluree's own docs.** `docs/security/cross-ledger-policy.md` says M's shapes are compiled against D and accept or reject D's transactions accordingly. It also says IRIs D has never seen are dropped silently, because "the shape couldn't apply to data D doesn't have". That reasoning holds for target classes and values. It fails for the shape's own node IRIs, and for a `sh:path` D has never used, where `sh:minCount` is exactly the case that must fire.
- **No warning.** Ledger info reports `configDiagnostics` for other config states that don't do what they appear to, such as shapes that no config enables. In the fresh cross-ledger case, ledger info carries no `configDiagnostics` at all ([`info_probe.py`](13-cross-ledger-shapes-skipped/info_probe.py), stock).

## Proposed fix

Encode every IRI in the pulled shapes into a validation-local overlay, instead of dropping triples whose IRIs the data ledger lacks. The overlay lives only for that validation, so D's persistent namespace registry stays untouched, which is the churn the current code avoids. An IRI D has never used then simply matches none of D's data. That gives SHACL's answers: a required property is missing, and a value is not an instance of an unseen class.

Short of that, fail closed. Refuse the transaction, or reject the config at write time, when translating a shape from `f:shapesSource` would drop any of its triples, instead of validating with fewer shapes than the model holds.

Other options considered:
- Allocate permanent namespace codes in D for model-only IRIs. This works, but it is the churn the doc comment rejects.
- Translate only shape-internal subjects (node shapes, named property shapes, list cells) into their own namespace space. This misses a `sh:path` D has never used.

## Workaround

- Keep shapes in the data ledger itself (same-ledger `f:shapesSource`). Nothing is dropped then, as the control case shows.
- Or, when a data ledger is created and on every model release, write one transaction into the data ledger that registers every namespace the model's shapes use, including the namespaces of the shape node IRIs and every `sh:path` and `sh:class` IRI. Then prove it with a probe write the shapes must reject.

## Who is affected

- **Affected:** any ledger whose `f:shapesSource` names a graph in another ledger (an `f:GraphRef` whose source has `f:ledger`), until the data ledger has registered the namespaces of every IRI in the shapes, the shape node IRIs included.
- **Not affected:** a `f:shapesSource` that names a graph in the same ledger, with no `f:ledger`.

## History

- **26 November 2026:** found while checking a shared-shapes design, and confirmed the same day on stock and on the full-patch build. Recorded on `main` as `c50d754`; completed, with the ledger-info probe, in `a82e1bb`, and its usage line corrected in `e23c063`. Not fixed.

## Related

- [Bug 1](01-graph-ids-after-index-install.md) (graph IDs after an index install) also lets SHACL accept invalid data, in its window.
- The design work's own probes are not in this repository; [`shapes_repro.py`](13-cross-ledger-shapes-skipped/shapes_repro.py) reproduces their result.

## Reproduction

```bash
python3 -I known-issues/13-cross-ledger-shapes-skipped/shapes_repro.py <fluree-binary>
```

It starts one in-memory server per case and prints each case's three outcomes. It exits 1 while any outcome is wrong. On stock and on the full-patch build it reports 6 wrong outcomes, in the three cross-ledger cases without the shape IRIs registered; the control and the two registered cases are correct. [`info_probe.py`](13-cross-ledger-shapes-skipped/info_probe.py) runs the same cases and also prints ledger info for the fresh cross-ledger ledger.
