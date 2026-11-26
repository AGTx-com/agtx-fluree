# 13. Shapes taken from another ledger are skipped until the data ledger has registered their IRIs

Found on 26 November 2026 while checking a multi-ledger design. Confirmed the same day on the stock v4.2.9 release (`3b8d19e649a6`) and on the pinned AGTx build (`64f56de8389c`), with identical results. **Not fixed.** Not reported to Fluree.

**Severity:** validation silently off. A ledger that takes its SHACL shapes from another ledger accepts data the shapes forbid, with no error or warning, until its own namespace registry happens to contain the shapes' IRIs. Nothing we deploy today takes shapes from another ledger: the current build keeps its shapes in its own ledger.

**Cause in brief.** Before validating, Fluree translates the shapes it pulls from the model ledger into the data ledger's terms, and drops every shape triple whose subject, predicate or object IRI the data ledger has not registered. A shape's own node IRIs (`urn:shape:Listing`, a named property shape) exist only in the model ledger, so on a ledger that has never used their namespaces every triple of the shape is dropped and the shape does not apply.

## What goes wrong

`ref:main` holds, in graph `<urn:g:shapes>`, a node shape for `urn:x:Listing` whose property shape requires one `urn:x:commodity` that is a `urn:x:Commodity`, and the value set (`urn:x:coffee` and `urn:x:cocoa` are commodities) beside it, as Fluree's [shared value-sets pattern](../docs/guides/cookbook-shacl.md#shared-value-sets-with-shclass) describes. `co:main` enables SHACL and takes its shapes from `ref:main`:

```trig
GRAPH <urn:fluree:co:main#config> {
  <urn:cfg:ledger> a f:LedgerConfig ; f:shaclDefaults <urn:cfg:shacl> .
  <urn:cfg:shacl>  f:shaclEnabled true ; f:shapesSource <urn:cfg:sref> .
  <urn:cfg:sref>   a f:GraphRef ; f:graphSource <urn:cfg:ssrc> .
  <urn:cfg:ssrc>   f:ledger <ref:main> ; f:graphSelector <urn:g:shapes> .
}
```

A listing with no commodity, and one naming `urn:x:unobtainium`, should both be rejected. On a fresh `co:main` both commit.

## Scope, measured

[`shapes_repro.py`](13-cross-ledger-shapes-skipped/shapes_repro.py), one in-memory server per case. Full output: [stock](13-cross-ledger-shapes-skipped/results-stock-4.2.9.txt), [pinned AGTx build](13-cross-ledger-shapes-skipped/results-patched-4.2.9.txt).

| Case | No commodity (reject) | Coffee (pass) | Unobtainium (reject) |
|---|---|---|---|
| Control: the same shapes in `co:main` itself | rejected | passes | rejected |
| Cross-ledger, fresh `co:main` | **passes** | passes | **passes** |
| Cross-ledger, `co:main` has used the data IRIs (`urn:x:commodity`, `urn:x:Commodity`, `urn:x:Listing`, `urn:x:coffee`) | **passes** | passes | **passes** |
| Same, plus an unrelated `urn:shape:Unrelated` | **passes** | passes | **passes** |
| Same, plus both shape node IRIs | rejected | passes | rejected |
| Blank-node property shape, plus the node shape's IRI | rejected | passes | rejected |

So the check is by namespace: `urn:shape:Unrelated` registers `urn:shape:` but not `urn:shape:Listing:`, the namespace of the named property shape. Blank-node property shapes survive once the node shape's namespace is known. Once the IRIs are registered, the cross-ledger value set works: `sh:class` sees the commodities declared in the model ledger's shapes graph.

**Not run:** JSON-LD spellings; `sh:sparql` constraints; a model ledger that changes after the data ledger first validated; whether the ledger-info diagnostics report anything.

## Cause

`ShapesArtifactWire::translate_to_schema_bundle_flakes` (`fluree-db-api/src/cross_ledger/types.rs`) encodes each wire triple with `NamespaceRegistry::lookup_sid_for_iri`, lookup only, and `continue`s past any triple whose subject, predicate or IRI object is not registered on the data ledger. Its doc comment gives the reason: an IRI the data ledger has never seen "couldn't apply to data D doesn't have anyway", and allocating codes for model-only IRIs would churn D's namespaces. That holds for target classes and values. It does not hold for the shape's own node IRIs, which are model-only by design, so dropping their triples removes the shape. `docs/security/cross-ledger-policy.md` documents the silent drop but not this consequence.

## Possible fixes, not chosen

- Translate shape-internal subjects (node shapes, named property shapes, list cells) into a namespace space of their own, so they never depend on D's registry.
- Allocate namespace codes for model-only IRIs that appear only as shape subjects.
- At minimum, fail closed: report a configuration error, or log and refuse, when a shape from `f:shapesSource` loses its node triples, instead of validating with fewer shapes.

A deployment-side workaround, until a fix: when a data ledger is created, write one triple that registers every namespace the model's shapes use, and re-run it on each standards release.
