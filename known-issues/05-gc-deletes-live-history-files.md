# 5. Index cleanup deletes a named graph's history file that the current index still uses

Found on 22 November 2026, while upgrading a private engine patch from v4.2.4 to v4.2.9: an application's history test failed deterministically with `history scan open_leaf_handle: Not found: …/index/objects/history/….fhs1`. Confirmed on the stock v4.2.9 release (`3b8d19e649a6`) on 23 November 2026, through time-pinned reads, which stock supports. **Fixed by `9841fd7`** on `main` (`gc: keep named-graph history sidecars that retained roots still use`). Not reported to Fluree.

**Severity: data loss in the index.** Reads of a named graph at earlier commits fail with `Not found`, the failure survives a restart, and stock's own full reindex does not repair it. The commits themselves are intact: a build with the fix, started on the damaged storage, repairs it with a full reindex. Fluree's docs say all history is preserved.

**Cause in brief.** Index files are named by a hash of their content. An incremental index build that rewrites a named graph's leaf, while the leaf's history is unchanged, writes a byte-identical history file with the same name, which the new index still uses; but the build also lists the old (same-named) file as garbage. Garbage collection has a guard for exactly this case, a check that no retained index still references the file, but the guard looks only at files an index root references directly and leaves out everything behind named-graph and annotation branches. So it deletes a file the current index needs. The fix expands those branches in both checks, as Fluree's own sweep already does.

## What goes wrong

With an index build after every commit (`--reindex-min-bytes=1`):

```sparql
# t=1
INSERT DATA { GRAPH <urn:g> { <urn:s> <urn:sel> <urn:rev:1> } }
# t=2: move the edge, so the graph's leaf gets a history file
DELETE DATA { GRAPH <urn:g> { <urn:s> <urn:sel> <urn:rev:1> } } ;
INSERT DATA { GRAPH <urn:g> { <urn:s> <urn:sel> <urn:rev:2> } }
# t=3, t=4, …: additive commits to the same graph only
INSERT DATA { GRAPH <urn:g> { <urn:s> <urn:note> "note 1" } }
```

The time-pinned read

```sparql
SELECT ?rev FROM <repro:main@t:1#urn:g> WHERE { <urn:s> <urn:sel> ?rev }
```

returns `urn:rev:1` until the 11th additive commit (t=13), then fails:

```
Query error: Internal error: V3 cursor: Not found: fluree:memory://repro/main/index/objects/history/4652e53a….fhs1
```

The same file and the same commit in both stock runs.

## Scope, measured

[`gc_time_travel_repro.py`](05-gc-deletes-live-history-files/gc_time_travel_repro.py), [`cleanup_scope_and_recovery.py`](05-gc-deletes-live-history-files/cleanup_scope_and_recovery.py) and [`cleanup_annotation_variant.py`](05-gc-deletes-live-history-files/cleanup_annotation_variant.py), default v4.2.9 GC settings, an index build after every commit. "Fixed" is the fork's full-patch build, which carries this fix.

| Read after the 11th additive commit | Expected | Stock | Fixed |
|---|---|---|---|
| Named graph, time-pinned at t=1 and at t=2 | the edge as it was | **fails, `Not found` on the history file** | right for all 30 commits |
| Named graph, time-pinned annotation reads (at t=2 and t=5) | the annotation as it was | **fails** after the 11th commit | not run |
| Named graph, current state (`GRAPH <urn:g>`, `FROM <ledger#urn:g>`) | `urn:rev:2` | right | right |
| Default graph: current, time-pinned and history reads | as written | right | right |
| Default graph: annotation reads, time-pinned annotation reads and annotation history | as written | right for 40 commits | not run |

**What lasts, and what repairs it** (`cleanup_scope_and_recovery.py … file`, with a second build started on the same storage):

| Step | Stock | Then the fixed build, on the same storage |
|---|---|---|
| After a server restart (file storage) | still fails, same file | still fails before its own reindex: the file is gone |
| After `POST /reindex` (a full rebuild) | HTTP 200 with a new root; the same reads fail on the same file | after its full reindex the reads are right |
| More additive commits | fails again after 1 more | right for 30 more |

So the commits are intact and recovery needs the fix plus a full reindex. Describe it as deleted index files, not lost commits.

**GC settings.** The earlier form of the repro checked with a `FROM NAMED` history query, which only the full-patch build answers (it is that branch's feature; stock answers with no rows and so cannot show the failure). On 22 November it gave:

| Build and GC settings | Result |
|---|---|
| v4.2.9 with the earlier patch, default settings | fails after 11 additive commits |
| Same, ceiling (`--gc-hard-max-old-indexes`) raised | passes; GC releases nothing |
| Same, age guard (`--gc-min-time-mins`) 0 | fails after 1 commit |
| v4.2.4 with the earlier patch, default settings | passes |
| v4.2.4 with the earlier patch, age guard 0 | fails after 6 commits |
| v4.2.9 with the fix, default settings | passes, GC releasing in 21 passes |
| v4.2.9 with the fix, age guard 0 | passes, GC releasing in 31 passes |

The failure after the 11th build matches the default ceiling of 10 old index versions, which overrides the 15-minute age guard. v4.2.4 has the same bug: its age guard hid it in test runs, but a server running longer than 15 minutes would reach it.

**Not run:** named-graph history queries on stock (stock cannot query them; the full-patch build failed this way before the fix, and that failure is how the bug was found); query peers; other storage backends than memory and file; a ledger with many named graphs.

## Cause

**In plain terms:** the cleanup's "is this file still in use?" check does not look inside named graphs.

**In the source** (stock v4.2.9), `fluree-db-indexer/src/gc/collector.rs`:
- `retained_refs` (`:105-112`), the set built when GC plans, collects `entry.root.all_cas_ids()` for each retained root.
- `retain_published_since` (`:728-742`), which adds roots published since the plan at release time, does the same.
- `IndexRoot::all_cas_ids()` lists the IDs a root references directly; named-graph and annotation branch manifests are left out. The comment above `retained_refs` (`:101-104`) states the assumption that nothing behind a named-graph or annotation branch manifest changes the answer. For a history sidecar revived by a rewritten named-graph leaf it does.
- Fluree's full-rebuild garbage diff and its sweep (`fluree-db-indexer/src/gc/sweep.rs:39`, `:333`) already expand those branches with `ChainCasIds`; only these two checks do not.

## What the standards say

No W3C standard covers index retention. Fluree's docs:
- `docs/concepts/time-travel.md`, "Storage Implications": all transaction history is preserved; a historical query reads history from the current index, and superseded index versions are kept only while running queries may still read them.
- `docs/indexing-and-search/background-indexing.md`, "Index Retention", and `docs/operations/configuration.md` describe the age guard (15 minutes), the ceiling (10) and the floor (0). Superseded versions "are not needed for time travel", which holds only if the current index's own files are kept.
- `background-indexing.md` also says `POST /reindex` rebuilds the index but reclaims nothing by itself, which fits the recovery result above.

## The fix

`9841fd7` on `main`, `fluree-db-indexer/src/gc/collector.rs`:
- Both checks expand named-graph and annotation branches with `ChainCasIds`.
- The expansion is strict: a branch manifest that cannot be read fails the GC pass, rather than leaving a live file unprotected.
- `retained_refs` becomes async and takes the content store.
- Tests, both failing without the change: `a_named_graph_sidecar_still_live_on_a_retained_root_is_not_released` and `a_named_graph_sidecar_revived_after_the_plan_is_not_released`.

**Cost:** each GC pass reads the branch manifests of the retained roots, as the sweep already does. No timing was measured.

## Workaround

Raising `--gc-hard-max-old-indexes` stopped the failure in a short test, because GC then released nothing; but the 15-minute age guard still lets GC collect versions on a long-running server, so it only delays the failure. No complete workaround on stock is known short of not collecting garbage; whether that is safe or possible in a given deployment was not checked. After a failure, recovery needs a build with the fix and a full reindex.

## History

- **22 November 2026:** found while upgrading a private engine patch to v4.2.9, through an application's named-graph history test; the cause located in GC (all tests passed with the ceiling raised), fixed in that patch with the two unit tests above, and shown also to be in v4.2.4. On that date the same code was on Fluree's `main` and `v5.0.0-alpha.4`, with no issue or PR mentioning it; not rechecked since.
- **23 November 2026:** confirmed on the stock v4.2.9 release with time-pinned reads (2 runs), including persistence across a restart, no repair by stock's reindex, repair by the fixed build, and the annotation variant. Committed to this fork as `9841fd7` on `main` (also on `full-patch`).
- **26 November 2026:** this record written.

## Related

- [Bug 1](01-graph-ids-after-index-install.md) is another named-graph index defect, at index install.
- [Bug 8](08-history-invents-retraction.md) is another history-read defect that depends on index state.

## Reproduction

```bash
python3 -I known-issues/05-gc-deletes-live-history-files/gc_time_travel_repro.py <fluree-binary> <port>
python3 -I known-issues/05-gc-deletes-live-history-files/cleanup_scope_and_recovery.py <fluree-binary> <port> <memory|file> [output.json]
python3 -I known-issues/05-gc-deletes-live-history-files/cleanup_annotation_variant.py <fluree-binary> <port> [output.json]
```

- `gc_time_travel_repro.py` starts `fluree server run --memory -- --reindex-min-bytes=1` in a temporary directory. Stock prints `FAILURE after additive commit 11 (t=13): … Not found: …/index/objects/history/….fhs1` and exits 1. The fix prints `no failure after 30 additive commits` and exits 0. Extra arguments are passed to the server, so the GC settings above can be tried (`--gc-hard-max-old-indexes=…`, `--gc-min-time-mins=…`).
- `cleanup_scope_and_recovery.py` reports which reads still work, then (file storage) a restart, a full reindex and more commits. With `REPRO_RECOVERY_BINARY=<second-binary>` and `file`, it then starts that build on the same storage and repeats the survey before and after its own full reindex. It exits 1 if the failure was seen.
- `cleanup_annotation_variant.py` reports the first commit at which each annotation read changed or failed.

Recorded outputs, 23 November 2026, all from the stock v4.2.9 release: the [time-travel repro](05-gc-deletes-live-history-files/results-time-travel-stock-4.2.9.txt); the scope survey on [memory storage](05-gc-deletes-live-history-files/results-scope-memory-stock-4.2.9.json) and on [file storage, then the full-patch build on the same storage](05-gc-deletes-live-history-files/results-scope-file-stock-then-full-patch.json); the [annotation variant](05-gc-deletes-live-history-files/results-annotation-stock-4.2.9.json). The fixed build's pass of `gc_time_travel_repro.py` (30 commits) was not saved as a file. Temporary paths in the outputs are shown as `<tmp>`.
