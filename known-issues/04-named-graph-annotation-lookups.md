# 4. Once an annotation index exists, a policy condition finds no edge annotation in a named graph

First fixed in a private engine patch for an earlier release; the date it was first found is not recorded. Confirmed on the stock v4.2.9 release (`3b8d19e649a6`) on 23 November 2026, while rerunning that patch's fixes against stock; the same day a deeper run showed one rule form wrongly allowing, from the first write on a default server. **Fixed by `bbab8f1`** on `main` (`query: use the annotation fast path only for the default graph`). Not reported to Fluree.

**Severity: report privately.** A presence rule ("allowed only if the membership is annotated active") refuses valid writes. An absence rule written with `OPTIONAL` ("allowed unless annotated suspended") **allows** the suspended member, because the lookup finds nothing.

**Cause in brief.** Fluree has a fast path for edge-annotation lookups that probes the annotation index with keys naming the default graph. A policy condition on a write runs in the written fact's graph without a dataset, and the gate that admits the fast path does not check which graph is active. So, once an annotation index exists and nothing is unindexed, a condition about a named-graph edge probes default-graph keys and finds nothing. Ordinary queries run with a dataset and take another path, so they stay correct. The fix admits the fast path only for the default graph.

## What goes wrong

Named graph `<urn:g:data>` holds two annotated memberships:

```trig
<urn:g:data> {
  <urn:x:alice> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m1> {| <urn:x:status> "active" |} .
  <urn:x:erin>  <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m2> {| <urn:x:status> "suspended" |} .
}
```

Rob has one write policy and writes a note about a member into `<urn:g:data>`; the condition runs in that graph. Three ways to write the rule:

```sparql
# presence: alice allowed, erin refused
ASK { $this <urn:x:memberOf> ?ws {| <urn:x:status> "active" |} }
# absence with NOT EXISTS: alice allowed, erin refused
ASK { $this <urn:x:memberOf> ?ws .
      FILTER NOT EXISTS { $this <urn:x:memberOf> ?ws {| <urn:x:status> "suspended" |} } }
# absence with OPTIONAL: alice allowed, erin refused
ASK { $this <urn:x:memberOf> ?ws .
      OPTIONAL { $this <urn:x:memberOf> ?ws {| <urn:x:status> ?st |} }
      FILTER(!BOUND(?st) || ?st != "suspended") }
```

On a stock server with default settings, the presence rule refuses alice and the `OPTIONAL` rule allows erin, from the first write. The same lookups as ordinary queries of the named graph, by `GRAPH <urn:g:data> { … }` and by `FROM <ledger#urn:g:data>`, return the right annotations throughout.

## Scope, measured

"Fixed" is the fork's full-patch build, which carries this fix.

**Index state and run mode** ([`annotation_policy_index_states.py`](04-named-graph-annotation-lookups/annotation_policy_index_states.py), 2 stock runs with identical results; decisions before → after the index step):

| Rule | Run mode | Expected | Stock | Fixed |
|---|---|---|---|---|
| Presence, alice | CLI `--direct`, then `fluree reindex` | allowed → allowed | allowed → **refused** | allowed → allowed |
| Presence, alice | server, then `POST /reindex` | allowed → allowed | **refused → refused** | allowed → allowed |
| Presence, alice | server indexing after every commit (`--reindex-min-bytes=1`) | allowed → allowed | **refused → refused** | allowed → allowed |
| `OPTIONAL` absence, erin | server, then `POST /reindex` | refused → refused | **allowed → allowed** | refused → refused |
| `OPTIONAL` absence, erin | server indexing after every commit | refused → refused | **allowed → allowed** | refused → refused |
| `NOT EXISTS` absence, alice and erin | all three | as written | right | right |
| Every other member and rule | all three | as written | right | right |
| Admin queries by `GRAPH` and by `FROM <ledger#graph>` | all three | `"active"` | right | right |

**Which lookup lane decides** ([`annotation_lane_separation_repro.py`](04-named-graph-annotation-lookups/annotation_lane_separation_repro.py), which reads stock's own debug events `annotation delegate gates` and `annotation delegate lane`; every direct attempt on a fresh copy of the ledger):

| Mode | Lane taken | Stock decisions |
|---|---|---|
| Direct, unindexed, or after the incremental `fluree index` | generic (no annotation arena) | right |
| Direct, after `fluree reindex` on a never-indexed ledger | arena | presence refuses alice; `OPTIONAL` allows erin |
| Same, with `FLUREE_ANNOTATION_LANE=chain` | generic | right |
| Same, with `FLUREE_ANNOTATION_LANE=arena` | arena | presence, `OPTIONAL` and `NOT EXISTS` all wrong |
| Same, with `FLUREE_ANNOTATION_LANE=enumerate` | enumerate where its gate holds | `NOT EXISTS` allows erin |
| Server, default background indexing | arena from the first write | presence refuses alice; `OPTIONAL` allows erin |
| Server, `--indexing-enabled=false`, then `POST /reindex` | generic, then arena | right, then wrong |
| Server, `FLUREE_ANNOTATION_LANE=chain` | generic | right |
| Default-graph control (same rules, data in the default graph) | arena | right |

Stock made 10 wrong decisions in this script; the fixed build made none, taking the generic lane for the named graph and the arena lane for the default graph.

**Why the modes differ.** A server indexes in the background by default (`--reindex-min-bytes` 100), so the annotation arena already exists at the first write. In `--direct` mode only a full `fluree reindex` of a never-indexed ledger built an arena; `fluree index`, and `reindex` after `index`, did not (not investigated). The gate also requires that nothing is unindexed (`overlay().is_effectively_empty()`), so once a write commits, the next decision takes the generic lane until the next index; that is why the index-states script, which writes several times between index steps, saw fewer wrong decisions in direct mode than the lane script.

**Never seen:** the lookup picking up the default graph's annotation for the same edge (a member "inactive" in the named graph and "active" in the default graph stayed refused, [`named_graph_annotation_repro.py`](04-named-graph-annotation-lookups/named_graph_annotation_repro.py)); a wrong answer from an ordinary query (ten query shapes over the named graph, before and after `fluree reindex`).

**Not run:** JSON-LD and Cypher conditions; read (`f:view`) policies; `GRAPH ?g` conditions; annotations on literal-valued edges.

## Cause

**In plain terms:** a shortcut for annotation lookups only knows how to look in the default graph, and its entry check lets named-graph policy conditions use it.

**In the source** (stock v4.2.9):
- A write policy's condition runs with `QueryPolicyExecutor::with_graph_id(g_id)` and no dataset (`fluree-db-query/src/policy/executor.rs:541-548`; `fluree-db-transact/src/stage.rs:3011-3026`).
- With no dataset, `DefaultGraphSourceOperator::open` builds the single-graph delegate (`fluree-db-query/src/default_graph_source.rs:843-847`).
- Its gate `annotation_probe_gates_pass` (`:713-719`) admits the arena fast path when an annotation index and a content store exist, the query is not a history query, the overlay is empty and the policy enforcer is root, which it is inside a policy condition. It does not check the active graph.
- The arena probe's edge keys are built with `g: None`, the default graph (`fluree-db-query/src/annotation_edge_probe.rs:278-279`). A named-graph edge is never found: the presence rule gets no solution, and the `OPTIONAL` rule's `?st` stays unbound.

Ordinary queries of the same graph run with a dataset and never reach this delegate.

## What the standards say

- **RDF 1.2 (TriG).** An annotation block on a triple inside a `GRAPH { }` block produces the reifier's triples in that same graph. A lookup of the annotation in graph g must find them.
- **SPARQL 1.1 Query, section 16.3 (`ASK`).** The answer is whether the pattern has a solution. The same pattern answers true as an ordinary query and false as a policy condition here, and the answer changes with index state alone.
- **Fluree's docs.** `docs/concepts/edge-annotations.md`, "Where can I write edge annotations?", says an annotation on an edge inside a named graph "is written into that same graph, keeping the edge's graph identity", for JSON-LD and for TriG; its "See also" adds that annotations work in named graphs. A documented limit, not this bug: SPARQL UPDATE cannot write annotations inside a named graph.

## The fix

`bbab8f1` on `main`, one condition in `annotation_probe_gates_pass` (`fluree-db-query/src/default_graph_source.rs`): the gate also requires `ctx.binary_g_id == 0`. Named graphs stay on the ordinary graph-aware chain, including after indexing has emptied the overlay.

**Cost:** named-graph annotation lookups in policy conditions lose the arena fast path and take the generic chain, as they already did before any index existed. Default-graph lookups are unchanged. No timing was measured.

No engine test in the commit covers it directly; the reproduction scripts are the evidence.

## Workaround

Set `FLUREE_ANNOTATION_LANE=chain` on the server or CLI: it pins the generic chain, and every decision in the lane script was right with it on stock. Prefer `NOT EXISTS` to `OPTIONAL … !BOUND` for absence rules; under the default lane choice it decided correctly, though not when a lane was forced.

## History

- **Before 22 November 2026:** fixed in a private engine patch for an earlier release; the date first found is not recorded.
- **22 November 2026:** ported to v4.2.9 and listed as a bug fix with no dedicated test.
- **23 November 2026:** confirmed on the stock v4.2.9 release in `--direct` mode after `fluree reindex` (valid writes refused, failing closed). Later that day a server run showed the refusal from the first write and the `OPTIONAL` form wrongly allowing; the lane script traced both to the arena fast path. The earlier premise "before any index exists" was wrong for a server. Committed to this fork as `bbab8f1` on `main` (also on `full-patch`).
- **26 November 2026:** this record written.

## Related

- [Bug 6](06-policy-reads-first-batch-only.md) was first suspected for the server-mode refusal; the lane script separates the two (`FLUREE_ANNOTATION_LANE=chain` on the same stock executor decides correctly).
- [Bug 3](03-post-state-misses-new-graphs.md) is another policy condition blind to a named graph's data.

## Reproduction

```bash
python3 -I known-issues/04-named-graph-annotation-lookups/named_graph_annotation_repro.py <fluree-binary> [output.json]
python3 -I known-issues/04-named-graph-annotation-lookups/annotation_policy_index_states.py <fluree-binary> <port> [output.json]
python3 -I known-issues/04-named-graph-annotation-lookups/annotation_lane_separation_repro.py <fluree-binary> <base-port> [output.json]
```

- `named_graph_annotation_repro.py`: CLI `--direct`, no server. Stock prints `policy: rob notes alice … after=refused WRONG` and the same for dave, `wrong answers: 2`, and exits 1. The fix prints 0 and exits 0. `REPRO_INDEX_COMMAND=index` uses the incremental `fluree index` instead (no wrong answer on stock).
- `annotation_policy_index_states.py`: three run modes, the presence, `NOT EXISTS` and `OPTIONAL` rules. Stock gets 5 decisions wrong (the rows above) and exits 1; the fix gets none.
- `annotation_lane_separation_repro.py`: uses ports `<base-port>` to `<base-port>+11`. Stock prints `wrong decisions: 10` and exits 1; the fix prints 0.

Recorded outputs, 23 November 2026: CLI on [stock](04-named-graph-annotation-lookups/results-cli-stock-4.2.9.json) and the [full-patch build](04-named-graph-annotation-lookups/results-cli-full-patch.json); index states on [stock](04-named-graph-annotation-lookups/results-index-states-stock-4.2.9.json) and [full-patch](04-named-graph-annotation-lookups/results-index-states-full-patch.json); lanes on [stock](04-named-graph-annotation-lookups/results-lanes-stock-4.2.9.txt) and [full-patch](04-named-graph-annotation-lookups/results-lanes-full-patch.txt). Temporary paths in them are shown as `<tmp>`.
