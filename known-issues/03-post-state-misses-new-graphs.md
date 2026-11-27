# 3. A post-state policy condition cannot see a named graph the same write creates

First fixed in a private engine patch for an earlier release; the date it was first found is not recorded. Confirmed on the stock v4.2.9 release (`3b8d19e649a6`) on 23 November 2026, in both directions, while rerunning that patch's fixes against stock. **Fixed by `415fb48`** on `main` (`transact: let post-state policy queries see graphs the write creates`). Not reported to Fluree.

**Severity: report privately.** A post-state rule that passes only when something is absent passes wrongly, because the new graph is invisible to it, so a write the rule exists to block is let through. The same blindness refuses valid writes.

**Cause in brief.** An `f:queryState f:postState` condition runs against a staged view built from the committed ledger plus the transaction's staged facts. The view's graph registry holds only committed graphs, so a `GRAPH <g>` pattern in the condition finds nothing in a graph this transaction creates, although the staged facts for it are there. The fix registers the transaction's new graphs in an isolated copy of the snapshot used only for the post-state view.

## What goes wrong

Rob's only write policy is a post-state condition that reads a named graph:

```json
{ "@id": "urn:x:rule", "@type": ["f:AccessPolicy", "urn:x:Policies"],
  "f:action": [{"@id": "f:modify"}],
  "f:queryState": {"@id": "f:postState"},
  "f:query": {"@type": "https://ns.flur.ee/db#sparql",
              "@value": "ASK { GRAPH <urn:g:items> { $this <urn:x:owner> $identity } }"} }
```

He writes an item he owns:

```sparql
INSERT DATA { GRAPH <urn:g:items> { <urn:x:item> <urn:x:owner> <urn:x:rob> } }
```

If `<urn:g:items>` already exists, the write is allowed. If this write is the one that creates it, stock refuses it.

The reverse, with an absence rule:

```sparql
ASK { $this <urn:x:owner> ?anyone .
      FILTER NOT EXISTS { GRAPH <urn:g:items> { $this <urn:x:flag> "blocked" } } }
```

A write that adds an item flagged `"blocked"` must be refused. If the write creates `<urn:g:items>`, stock allows it.

## Scope, measured

[`post_state_new_graph_repro.py`](03-post-state-misses-new-graphs/post_state_new_graph_repro.py), CLI `--direct`, `--as urn:x:rob`, 2 runs on stock with identical results. "Fixed" is the fork's full-patch build, which carries this fix.

| Case | Expected | Stock | Fixed |
|---|---|---|---|
| Graph exists; Rob writes an item he owns | allowed | allowed | allowed |
| Graph exists; Rob writes an item someone else owns | refused | refused | refused |
| Graph created by this write; Rob writes an item he owns | allowed | **refused** | allowed |
| Graph created by this write; Rob writes an item someone else owns | refused | refused | refused |
| Absence rule, graph exists; Rob writes a blocked item | refused | refused | refused |
| Absence rule, graph created by this write; Rob writes a blocked item | refused | **allowed** | refused |

**Where it applies:** only post-state conditions, and only on the write that first creates the graph. Pre-state conditions, the default, read committed state and are unaffected; on the read path pre-state and post-state coincide.

**Not run:** JSON-LD and Cypher conditions; `GRAPH ?g` over new graphs; a new graph whose IRI uses a namespace the same transaction introduces (the fix handles it; no case tests it); signed requests.

## Cause

**In plain terms:** the post-state view adds this write's facts to the committed data, but not this write's new graph names, so it cannot route a `GRAPH` pattern to them.

**In the source** (stock v4.2.9): `enforce_modify_policies` (`fluree-db-transact/src/stage.rs:2803`) builds the post-state view as `StagedLedger::new(ledger.clone(), staged.to_vec(), reverse_graph)` (`:2896`) when the policy has post-state conditions. The cloned ledger's snapshot carries the committed `graph_registry` only. The transaction's own graph routing (`reverse_graph`) is applied to the staged flakes, but not to the registry that the condition's `GRAPH` lookup consults. The staged ledgers the stage paths return (`:1237`, `:2631`) are built the same way.

## What the standards say

- **Fluree's docs.** `docs/security/policy-model.md` defines `f:postState` as "committed + this transaction's staged flakes" (the `f:queryState` row and the "Condition state" section), with no exception for a graph the write creates. The same section documents two real limits, which the fix leaves alone: on the read path the two states coincide, and one condition cannot mix both states (a `GRAPH f:postState { … }` extension is planned).
- **SPARQL 1.1 Query, section 13.3.** `GRAPH <g> { P }` matches P against the named graph g of the dataset. The post-state dataset holds graph g with the staged facts, so the pattern must match them.

## The fix

`415fb48` on `main`, in `fluree-db-transact/src/stage.rs`:
- The post-state view registers the transaction's graphs in an isolated copy of the snapshot (`apply_envelope_deltas` with the decoded graph IRIs), and checks that each gets the graph ID the transaction routed it to; a mismatch fails the transaction ("Staged policy graph routing differs from transaction routing").
- Pre-state checks still use the committed registry.
- A new graph IRI can use a namespace that the same transaction introduces. `enforce_modify_policies` therefore takes the transaction's namespace delta and applies it to a copy of the snapshot used only to decode IRIs; no staged facts are read through it. Staging of pre-built flakes passes an empty delta.

**Cost:** one snapshot copy per write that has post-state conditions, plus one when the transaction introduces namespaces. No timing was measured.

No engine test in the commit covers it; the reproduction script is the evidence.

## Workaround

Create the graph in an earlier transaction, before any write that a post-state condition must judge. Avoid absence tests (`NOT EXISTS`, `MINUS`, `OPTIONAL` with `!BOUND`) on a graph that the judged write may create.

## History

- **Before 22 November 2026:** fixed in a private engine patch for an earlier release; the date first found is not recorded.
- **22 November 2026:** ported to v4.2.9 and listed in that patch's reference as a bug fix with no test.
- **23 November 2026:** confirmed on the stock v4.2.9 release in both directions, 2 runs. Committed to this fork as `415fb48` on `main` (also on `full-patch`).
- **26 November 2026:** this record written.

## Related

- [Bug 4](04-named-graph-annotation-lookups.md) is another policy condition that cannot see a named graph's data, and it also has an absence form that wrongly allows.

## Reproduction

```bash
python3 -I known-issues/03-post-state-misses-new-graphs/post_state_new_graph_repro.py <fluree-binary> [output.json]
```

CLI in `--direct` mode on fresh temporary ledgers; no server. It prints one line per case. Stock prints `NOT AS DOCUMENTED` on the two "created by this write" rows above and exits 1; the fix prints `as documented` on all six and exits 0.

Recorded outputs, 23 November 2026: [stock](03-post-state-misses-new-graphs/results-stock-4.2.9.json), [full-patch build](03-post-state-misses-new-graphs/results-full-patch.json). Temporary paths in them are shown as `<tmp>`.
