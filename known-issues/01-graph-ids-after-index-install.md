# 1. After an index install, a named graph reads another graph's unindexed facts

First fixed from a reading of the index-install code, in a private engine patch for an earlier release; the date it was first found is not recorded, and until 22 November 2026 it had never been observed. Reproduced on the stock v4.2.9 release (`3b8d19e649a6`) on 23 November 2026, while rerunning each of that patch's fixes against stock. **Fixed by `000e120`** on `main` (`ledger: remap novelty graph IDs by IRI when an index is installed`). Not reported to Fluree.

**Severity: report privately.** The most serious bug in this list. For a while after an index is installed, a query of one named graph returns another named graph's recent facts and misses its own. Updates made in that window store a wrong result for good, policy conditions allow and refuse the wrong identities, and SHACL accepts data it should reject.

**Cause in brief.** Two parts of the engine number named graphs independently. The live graph registry numbers a commit's new graphs in alphabetical IRI order; the indexer numbers them in the order it first meets their facts. Commits not yet indexed are kept in memory under the registry's numbers. When an index is installed while later commits are still unindexed, the ledger takes the index's numbering but leaves the in-memory commits filed under the old numbers, so each graph reads the other's recent facts until the next index covers them. The fix re-files the in-memory commits by graph IRI at install. Details under [Cause](#cause) and [The fix](#the-fix).

## What goes wrong

One commit creates two named graphs. The subject of z's fact sorts before a's, so the indexer meets z first:

```sparql
# t=1, with enough filler in the default graph that its index build takes a few hundred milliseconds
INSERT DATA {
  GRAPH <urn:g:z> { <urn:s:1-1> <urn:p:m> "z-1" }
  GRAPH <urn:g:a> { <urn:s:2-1> <urn:p:m> "a-1" }
}
# t=2 .. t=6, sent while t=1 is being indexed (N = 2 .. 6)
INSERT DATA {
  GRAPH <urn:g:z> { <urn:s:1-N> <urn:p:m> "z-N" }
  GRAPH <urn:g:a> { <urn:s:2-N> <urn:p:m> "a-N" }
}
```

The live registry gives `<urn:g:a>` ID 3 and `<urn:g:z>` ID 4; the index for t=1 gives z 3 and a 4. Between the install of that index and the next install, a query of graph z

```sparql
SELECT ?o FROM <repro-1:main#urn:g:z> WHERE { ?s <urn:p:m> ?o }
```

returns `a-2 … a-6, z-1` (z's indexed fact plus graph a's unindexed facts) instead of `z-1 … z-6`. The same holds through `GRAPH <urn:g:z> { … }`, through `GRAPH ?g` (a's facts reported under z's IRI) and for a time-pinned read at the newest commit.

Anything that reads a named graph in that window reads the wrong graph: queries, the `WHERE` of updates, policy conditions and SHACL validation.

**Trigger.**
- An index is installed while later commits are still unindexed, and the index numbers the graphs differently from the live registry.
- One way: a single commit creates two named graphs whose facts sort in the opposite order from their IRIs, and more commits arrive while that commit is being indexed.
- It also happens when the graphs are created one per commit (z, then a), with file storage, and after a restart with unindexed commits. It did not happen when the graphs were created one per commit in alphabetical order.

## Scope, measured

"Stock" is the stock v4.2.9 release; "fixed" is the fork's full-patch build, which carries this fix (as `f952286` on the `full-patch` branch, the same change as `000e120` apart from its test's graph names). Every case opened the same window on both builds. One server process on one machine (macOS arm64, 10 cores), synthetic data, two or three graphs.

**Reads** ([`scope_graph_id_swap.py`](01-graph-ids-after-index-install/scope_graph_id_swap.py), `--scale 2`, counts of reads that started after the install and finished before the next one; [summary](01-graph-ids-after-index-install/results-scope-summary.txt)):

| Read | Expected | Stock | Fixed | Runs |
|---|---|---|---|---|
| `FROM <ledger#iri>` | own facts | wrong: 1,090 of 1,090 in window; 18,083 of 18,083 with the long window | 0 of 646; 0 of 11,474 | 5; 3 |
| `GRAPH <iri>` | own facts | wrong: 1,115 of 1,115; 17,801 of 17,801 | 0 of 627; 0 of 11,323 | 5; 3 |
| `GRAPH ?g` | facts under their own IRI | wrong: 542 of 542; 8,915 of 8,915 | 0 of 305; 0 of 5,584 | 5; 3 |
| Time-pinned at the newest commit, `FROM <ledger@t:7#iri>` | own facts | wrong: 797 of 797; 18,052 of 18,052 | 0 of 623; 0 of 11,339 | 5; 3 |
| One graph per commit, z then a | own facts | wrong: 4,141 of 4,141 in 5 of 5 runs | 0 of 3,001 | 5 |
| File storage | own facts | wrong: 4,353 of 4,353 in 3 of 3 runs | 0 of 4,006 | 3 |
| Restart with unindexed commits, commits during the start-up catch-up build | own facts | wrong: 3,892 of 3,892 in 3 of 3 runs | 0 of 4,126 | 3 |
| Minimal repro ([`repro_graph_id_swap.py`](01-graph-ids-after-index-install/repro_graph_id_swap.py)) | no wrong read | wrong reads in 10 of 10 runs (30 of 30 rounds), 13,600 of 265,350 reads | 0 of 10 runs, 0 of 273,982 reads | 10 |

**Writes made in the window** (`scope_graph_id_swap.py … writes --scale 2`, file storage, each write on its own ledger and sent 0.3–2.5 ms after the install was logged; [stock](01-graph-ids-after-index-install/results-writes-stock-4.2.9.txt), [fixed](01-graph-ids-after-index-install/results-writes-full-patch.txt)). Graph z held `z-1 … z-6` and graph a held `a-1 … a-6`. The results were the same in 3 of 3 runs, after indexing settled and after a restart:

| Write | Expected (control on both builds; fixed in window) | Stock, in window |
|---|---|---|
| `DELETE { GRAPH <z> {?s ?p ?o} } WHERE { GRAPH <z> {?s ?p ?o} }` | z empty | commits retractions of `z-1` and of `a-2 … a-6` labelled graph z; z keeps `z-2 … z-6` |
| `INSERT { GRAPH <copy> {…} } WHERE { GRAPH <z> {…} }` | copy holds `z-1 … z-6` | copy holds `a-2 … a-6, z-1` |
| `DELETE/INSERT … WHERE { GRAPH <z> {…} BIND(CONCAT(?o, "-edited") …) }` | z holds `z-1-edited … z-6-edited` | z holds `a-2-edited … a-6-edited, z-1-edited` and unedited `z-2 … z-6` |
| `DELETE DATA { GRAPH <z> { <urn:s:1-3> <urn:p:m> "z-3" } }` | a commit retracting `z-3` | HTTP 200 at the existing head, no new commit; `z-3` remains |
| TriG upsert of `<urn:s:1-3>` in z to `"z-3-new"` | `z-3` replaced | only the assertion is committed; z holds both values |

Graph a's own stored facts were never changed: commits name graphs by IRI, so the damage comes only from what a transaction reads in the window.

**Policy and SHACL** ([`repro_window_policy.py`](01-graph-ids-after-index-install/repro_window_policy.py), [`repro_window_shacl.py`](01-graph-ids-after-index-install/repro_window_shacl.py) and the harness [`window_policy_shacl.py`](01-graph-ids-after-index-install/window_policy_shacl.py), 3 runs on each build; [per-case results](01-graph-ids-after-index-install/results-window-batch.txt)). The policy cases use a raised index threshold, which keeps the window open for about 3.5 s; the SHACL cases also reproduce under default settings, where the window is under half a second. Under default settings, policy requests by an identity took 320–450 ms against 2–7 ms for the administrator, so they finished after the window closed and were not counted.

| Check during the window | Expected | Stock | Fixed |
|---|---|---|---|
| View and modify policy `ASK { GRAPH <urn:g:z> { $identity <urn:p:grant> true } }`; Rob's grant-shaped fact is in graph a | Rob refused | Rob's read and write **allowed**, 3 of 3 | refused, 3 of 3 |
| Same policy; Sam's grant is in graph z | Sam allowed | Sam's read and write refused, 3 of 3 | allowed, 3 of 3 |
| Per-fact view rule "title visible if marked public in graph z" (`window_policy_shacl.py … policy-filter --long-window`) | sees only titles marked in z | **sees a title marked public only in graph a** and misses one marked in z, 3 of 3 (9 wrong decisions) | right, 3 of 3 |
| SHACL `sh:maxCount 1`; a second value written into graph z | rejected | **accepted**, 3 of 3; both values stay stored after a restart, and `/validate` then reports z as non-conforming | rejected |
| SHACL `sh:maxCount 1`; a first value written into z while graph a holds one | accepted | rejected, 3 of 3 | accepted |
| Which graph's per-graph SHACL setting (`f:graphOverrides`) applies | the written graph's | right | right |

An identity with no grant anywhere was refused in every run, so policy is enforced on this path. A write that policy wrongly allowed is committed like any other and nothing marks it.

**Not affected** (stock, `scope_graph_id_swap.py`): the default graph (0 wrong of 9,593 reads); time-pinned reads below the newest commit (0 of 48,152); history ranges over the default graph (0 of 7,779); any read once indexing had caught up, or after a restart (0 of 418); a restart with indexing off in both runs (no install, no window).

**How long the window lasts.**
- Default settings: one index build, from the install that renumbers the graphs until the next install covers the old tail; 66–478 ms in these shapes. It grows with how much was committed while the previous build ran.
- With a raised `--reindex-min-bytes`, small commits start no build, so the window stays open until the periodic catch-up sweep: 3.4–3.6 s with `--indexer-catchup-interval-secs=2`, and 598 s once with the default 300-second interval (4,640 of 4,646 sampled reads wrong on stock, 0 on the fixed build).
- From the source, not run: the sweep queues a ledger only once its `commit_t` stops moving (`fluree-db-indexer/src/orchestrator.rs:2627-2729`), so steady small writes could hold such a window open longer.

**Not run:** JSON-LD, Cypher, GraphQL, multi-query and streaming reads in the window; policies or shapes stored in a named graph; other SHACL constraints; signed requests in the window; `CREATE GRAPH` of empty graphs; graph drops; `/reindex` (by the source it is safe: it refuses when the ledger has moved and evicts the cached ledger, `fluree-db-api/src/admin.rs:2369-2390`); query peers; many graphs.

## Cause

**In plain terms:** the ledger and the indexer each give named graphs numbers, by different rules. In-memory commits are filed by number. Installing an index adopts the index's numbers without re-filing the in-memory commits.

**In the source** (stock v4.2.9):
- **Live registry.** `GraphRegistry::apply_delta` (`fluree-db-core/src/graph_registry.rs:482-529`) deduplicates a commit's new graph IRIs, sorts them and numbers them from `next_id`. The module doc (`:21-22`) states that assignment is deterministic.
- **Indexer.** A full rebuild starts with only the system graphs (`fluree-db-indexer/src/build/rebuild.rs:278`) and `resolve_graph` (`fluree-db-indexer/src/run_index/resolve/resolver.rs:1661-1677`) numbers each graph when it first meets one of its flakes, in commit order. An incremental build keeps the previous root's order and appends new graphs in first-met order (`fluree-db-indexer/src/run_index/build/incremental_resolve.rs:296`).
- **Novelty.** Unindexed flakes are held per numeric graph ID (`fluree-db-novelty/src/lib.rs:608-611`), routed through the registry at write time and read by ID.
- **Install.** `LedgerState::apply_loaded_db` (`fluree-db-ledger/src/lib.rs:817-966`) replaces the registry with the root's and carries the old graph IRIs forward. Its comment says this keeps graph routing for the remaining novelty (`:849-852`), but the novelty's segments stay in their old numeric slots, which now belong to other graphs. `apply_index` (`:740-805`) carries nothing forward at all.
- **Known elsewhere.** Upstream already matches graphs by IRI for drop records, because "a rebuilt root may number the graphs differently" (`carry_graph_drops`, `fluree-db-ledger/src/lib.rs`), but not for novelty.

Commits name graphs by IRI (`fluree-db-core/src/flake.rs:168-172`), which is why stored facts are never damaged directly.

## What the standards say

- **RDF 1.1 Concepts, section 4 (RDF datasets).** A named graph is a pair of a graph name and an RDF graph. The name identifies the graph; numeric IDs are an internal encoding and must not change which triples a graph holds.
- **SPARQL 1.1 Query, section 13 (RDF dataset).** `FROM <g>` builds the default graph from graph g, and `GRAPH <g> { … }` matches only against the named graph g. Neither may return triples of another graph.
- **SPARQL 1.1 Update, section 3.1.3 (`DELETE`/`INSERT`).** The `WHERE` is evaluated against the dataset and the templates are instantiated from its solutions, so a delete over graph z removes z's triples and no others.
- **SHACL, section 4.2.2 (`sh:maxCount`).** The count is of the value nodes in the data graph being validated.
- **Fluree's docs** say nothing about this. Fluree's source states the intent: deterministic graph IDs (`graph_registry.rs:1`, `:21-22`) and a carry-forward that keeps graph routing (`fluree-db-ledger/src/lib.rs:849-852`). No design could intend one graph's facts to be returned for another.

## The fix

`000e120` on `main`:
- `Novelty::remap_graph_ids` (`fluree-db-novelty/src/lib.rs`) moves graph segments to their new IDs and rebuilds the per-graph fact-state index. It rejects a missing or duplicate mapping.
- `carry_post_index_entries` (`fluree-db-ledger/src/lib.rs`) carries namespace codes and graph IRIs forward, then remaps the novelty by graph IRI. `apply_loaded_db` and `apply_index` both call it when novelty remains; before the fix `apply_index` did not carry namespace codes or graph IRIs either.
- Test: `index_install_remaps_surviving_named_graph_assertions_and_retractions` in `fluree-db-ledger/src/lib.rs`.

**Cost:** nothing when the index and the live registry agree (the remap returns at once). Otherwise one pass over the remaining in-memory flakes at that install, to re-file them and rebuild the fact-state index.

The fix was judged by outcome (the reproductions above), not reviewed line by line.

## Workaround

None reliable on stock. Partial mitigations:
- Keep the default index threshold, so the window lasts one index build rather than until the catch-up sweep.
- A reader that must be correct can wait until `GET /v1/fluree/info/<ledger>` shows `index-t` equal to `commit-t`. Writes cannot be held back the same way.
- Creating each named graph in its own commit, in alphabetical IRI order, was not affected in 3 runs. That it is safe in general is not established.

A restart does not help: a restart with unindexed commits opens the window too.

## History

- **Before 22 November 2026:** fixed from source reading in a private engine patch for an earlier release; the date first found is not recorded. Never observed.
- **22 November 2026:** the fix was ported to v4.2.9 and listed as a latent bug, not seen in practice.
- **23 November 2026:** reproduced on the stock v4.2.9 release (wrong reads in 10 of 10 runs, then 3 of 3 reruns, about 1,750 wrong of 31,400 reads each). The same day, writes, policy decisions and SHACL validation in the window were shown wrong on stock and right on the fixed build. Committed to this fork as `000e120` on `main` and `f952286` on `full-patch`.
- **26 November 2026:** this record written.

## Related

- [Bug 13](13-cross-ledger-shapes-skipped.md) also lets SHACL accept data its shapes forbid.
- [Bug 5](05-gc-deletes-live-history-files.md) is another named-graph index defect, in index cleanup.

## Reproduction

Each script needs Python 3's standard library only, starts its own server in a fresh temporary directory, and removes both when it finishes. The bug is a race: the scripts size their commits so that the window opens on a typical laptop, and print the index installs they saw so that a run in which no window opened can be told apart.

```bash
python3 -I known-issues/01-graph-ids-after-index-install/repro_graph_id_swap.py <fluree-binary>
```

About 8 s, three rounds. Stock prints `WRONG …` lines and `RESULT: WRONG READS: <n> of <m> reads returned another graph's facts or missed their own`, and exits 1. The fix prints `RESULT: no wrong reads in <m> reads` and exits 0. Each round's `index installs` line should show `(1, 60058)`: index t=1 installed with unindexed flakes left.

```bash
python3 -I known-issues/01-graph-ids-after-index-install/repro_window_policy.py <fluree-binary> [port]
python3 -I known-issues/01-graph-ids-after-index-install/repro_window_shacl.py <fluree-binary> [port]
python3 -I known-issues/01-graph-ids-after-index-install/scope_graph_id_swap.py <fluree-binary> <port> writes --scale 2
```

- `repro_window_policy.py`: stock prints `RESULT wrong in-window decisions with a right control: 4` and exits 1; the fix prints 0 and exits 0.
- `repro_window_shacl.py`: stock prints 2 wrong decisions, `k1=first, k1=second` stored in z after settling and after a restart, and `/validate` `conforms=False`; it exits 1. The fix prints 0 and exits 0.
- `window_policy_shacl.py <fluree-binary> <port> <case> --long-window` takes the cases `policy-allow`, `policy-refuse`, `policy-filter`, `shacl-accept`, `shacl-reject` and `shacl-graph-setting`, and prints `RESULT case=<case> wrong_in_window=<n> …`; stock gives 6, 6, 9, 3, 3 and 0 over 3 runs, the fix 0 in every case.
- `scope_graph_id_swap.py` also takes the cases `reads`, `one-per-commit`, `one-per-commit-alpha`, `file-storage`, `restart-unindexed`, `restart-unindexed-off` and `long-window` (see its docstring). It exits 1 on any wrong read or wrong final state.

Recorded outputs, 23 November 2026: minimal repro on [stock](01-graph-ids-after-index-install/results-swap-stock-4.2.9.txt) and on the [fixed build](01-graph-ids-after-index-install/results-swap-full-patch.txt); writes on [stock](01-graph-ids-after-index-install/results-writes-stock-4.2.9.txt) and [fixed](01-graph-ids-after-index-install/results-writes-full-patch.txt); policy on [stock](01-graph-ids-after-index-install/results-window-policy-stock-4.2.9.txt) and [fixed](01-graph-ids-after-index-install/results-window-policy-full-patch.txt); SHACL on [stock](01-graph-ids-after-index-install/results-window-shacl-stock-4.2.9.txt) and [fixed](01-graph-ids-after-index-install/results-window-shacl-full-patch.txt); the per-fact view rule on [stock](01-graph-ids-after-index-install/results-window-filter-stock-4.2.9.txt) and [fixed](01-graph-ids-after-index-install/results-window-filter-full-patch.txt); every scope case on both builds in [the summary](01-graph-ids-after-index-install/results-scope-summary.txt). "Fixed" there is the fork's full-patch build. A binary built from `main` with fixes 1 to 8 also passed the reproduction scripts on 23 November; those outputs were not kept.
