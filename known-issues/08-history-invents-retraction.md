# 8. History invents a retraction when an unchanged fact is re-sent

First seen on a patched build and set aside as a possible engine bug; the date is not recorded. Reproduced on the stock v4.2.9 release (`3b8d19e649a6`) on 23 November 2026, while rerunning history problems against stock; its cause was traced the same day by controlling each index step. **Fixed by `366220b`** on `main` (`query: report only state changes when a fact is re-sent in history`). Not reported to Fluree.

**Severity: wrong history.** The same history query over the same commits gives three answers depending only on index state, and one of them reports a retraction that no commit contains. A reader that treats history rows as events (for example "a new assertion means a new selection") is misled.

**Cause in brief.** Re-sending a fact that is already asserted writes a commit with one redundant assertion and no retraction. Three layers then disagree about it. The in-memory layer drops a re-assertion only of a fact it holds itself. An incremental index build that absorbs the copy replaces the indexed row and records the replacement as a synthesized same-`t` retract entry, an internal marker for replay. The history reader passes both the in-memory copy and the marker through as events. A full rebuild sees the whole log at once and reports one assertion. The fix makes the history reader report only state changes, as Fluree's own index format promises.

## What goes wrong

A fact inserted at t=3 and sent again, unchanged, at t=4:

```sparql
INSERT DATA { <urn:x:s> <urn:x:link> <urn:x:b> }   # t=3
INSERT DATA { <urn:x:s> <urn:x:link> <urn:x:b> }   # t=4: the commit holds one assertion, no retraction
```

```sparql
PREFIX f: <https://ns.flur.ee/db#>
SELECT ?t ?op FROM <repro:main@t:1> TO <repro:main@t:latest>
WHERE { << <urn:x:s> <urn:x:link> ?o >> f:t ?t ; f:op ?op } ORDER BY ?t ?op
```

| State when history is asked | Stock answer |
|---|---|
| Original indexed, the re-sent copy only in memory | `(t3, assert)`, `(t4, assert)` |
| After an incremental index build absorbed the copy | `(t3, assert)`, `(t4, retract)`, `(t4, assert)` |
| After a full reindex | `(t3, assert)` |

`GET /v1/fluree/show/repro:main?commit=t:4` shows one assertion and no retraction in every state.

## Scope, measured

"Fixed" is a build of the fork's `full-patch` branch at `f5689bf`, whose change is the same as `366220b` on `main`.

**By index state** ([`history_resend_by_index_state.py`](08-history-invents-retraction/history_resend_by_index_state.py); background indexing held back, each index step requested by the script; 2 stock runs and one run on the full-patch build before the fix, all identical):

| Step | Expected | Stock | Full-patch before the fix | Fixed |
|---|---|---|---|---|
| 0: insert and re-send with nothing indexed in between | one assertion | one assertion | one assertion | one assertion |
| 2: original indexed, copy only in memory | one assertion (t3) | **two assertions** | two assertions | one assertion |
| 3: after the incremental build absorbed the copy | one assertion (t3) | **assert, then retract and assert at t4** | the same | one assertion |
| 4: after a full reindex | one assertion (t3) | one assertion | one assertion | one assertion |

**In a named graph** (the same script with `RESEND_GRAPH=urn:g`). Stock cannot query named-graph history: its docs say a history query reads the default graph alone, and stock answers a `FROM NAMED` history query with no rows. The full-patch build adds that query; before the fix it gave the same three answers as in the default graph, and with the fix one assertion in every step.

**By write form** ([`resend_index_repro.py`](08-history-invents-retraction/resend_index_repro.py), default background indexing, with a synchronous `POST /reindex` between insert and re-send in the "indexed" half):

| Re-send form | Commit | History right after | After a reindex | Fixed |
|---|---|---|---|---|
| Unguarded JSON-LD insert, ledger not yet indexed | 1 assertion, 0 retractions | one assertion | one assertion | one assertion, both times |
| Guarded JSON-LD update and SPARQL `INSERT DATA`, not yet indexed; all three forms when indexed | 1 assertion, 0 retractions | **assertion, then retraction and assertion at the re-send** | one assertion | one assertion, both times |

Under default background indexing without forced reindexes, a re-send sometimes shows as a second assertion with no retraction: 4 of 50 re-sends on stock, all in one run (7 of 20 on the full-patch build before the fix). Once it starts in a run, every later re-send in that run is affected, which points to a background build landing mid-run; the exact condition was not pinned down.

**Kept by the fix:** a real update still shows its retraction and its new assertion; a fact retracted in the index and re-asserted later keeps that assertion.

**Not run:** JSON-LD and Cypher history queries; re-sends inside a multi-fact transaction; imported or pushed commits.

## Cause

**In plain terms:** history is assembled from three places, and each treats a repeated fact differently.

**In the source** (stock v4.2.9):
- **In memory.** `fluree-db-novelty/src/lib.rs` (around `:1108`, "RDF set semantics") skips an assertion whose fact is already asserted in this graph's novelty window. It does not know facts that are already indexed, so the copy is kept.
- **History reader.** `fluree-db-query/src/binary_history.rs` merges indexed history with novelty and emits every op, so the in-memory copy appears as a second assertion.
- **Incremental index.** `fluree-db-indexer/src/run_index/build/novelty_merge.rs` (`:219`, `:312`): for an assert of a continuously present fact, the merge emits the novelty row as a replacement, encoded as "a synthesized same-`t` retract entry paired with the row's own assert", which is how replay reads "row replaced while present". The history reader emits that entry as a real retraction.
- **Full rebuild.** It sees the whole log as one window and drops the repeat.

## What the standards say

- **RDF 1.1 Concepts, section 3.** An RDF graph is a set of triples, so adding a triple it already holds changes nothing.
- **Fluree's docs.** `docs/transactions/insert.md`, "Duplicate Prevention": inserting the same triple again is a no-op. `docs/transactions/upsert.md`: a repeated upsert commits nothing. `docs/concepts/time-travel.md`: a history row's `@op` mirrors the stored fact's operation.
- **Fluree's source states the design.** `fluree-db-binary-index/src/format/transitions.rs`, "What the index promises about history": index-served history is transition-grade, and "a re-assert of an already-present fact changes no state and leaves no trace here". The same file says a re-asserted fact's visible `t` follows indexing windows "by design".
- **So part of this is deliberate:** the synthesized retract is an internal replay marker, and the window-dependent `t` is intended. What is not intended is the marker, and the in-memory re-assertion, reaching history queries.

## The fix

`366220b` on `main`. Only `fluree-db-query/src/binary_history.rs` changes; the indexer, the history file format, novelty and staging do not. `transition_grade_events` makes the merged events transition-grade before policy filtering:
- **Indexed events:** a retract and an assert of one fact identity at one `t` are a replaced row, and both are dropped.
- **In-memory events:** ops are folded per fact identity in `t` order, starting from the fact's presence in the index. An op that repeats the current state is dropped. Ops at one `t` follow Fluree's same-`t` rule (the assertion holds).
- In-memory ops before the range start are read too, because they decide presence; they are not returned.
- Presence in the index is read with a `NoOverlay` equality range read as of the index `t`, once per distinct in-memory fact whose first op is an assertion. A fact whose first in-memory op is a retraction is taken as present. Without a binary store nothing is read.

Every state above now reports the single original assertion, and a range that starts at the re-send reports nothing for the fact. The row's visible `t` still follows the indexing window, as `transitions.rs` describes; only the history events change.

**Cost:** one index lookup per distinct newly asserted in-memory fact that matches the pattern, charged through the query's tracker; in-memory ops before the range start are now charged fuel like other retained flakes. A broad history query over a large unindexed tail therefore does one lookup per new fact.

**Tests:** ten unit tests in `fluree-db-query/src/binary_history.rs` (`history_drops_*`, `history_keeps_*`, `history_resolves_*`, `history_folds_*`), and `history_of_a_resent_fact_is_one_assert_in_every_index_state` in `fluree-db-api/tests/it_query_history_sparql.rs`, which drives a full rebuild, an incremental build and another full rebuild. The integration test and the six unit tests of the defect failed before the fix; the four of behaviour to keep passed before and after. The `grp_query_history` group (72 tests) passes.

**Limits of the fix:**
- A same-`t` retract and assert held in memory (possible in imported, pushed or older merged commits) now shows only the assertion when the fact was absent, and nothing when it was present.
- Not fixed: the re-send still writes a commit holding one redundant assertion, which sits oddly with `insert.md`'s "no-op".

## Workaround

Read history after a full reindex, or treat history rows as transitions on the client: drop a retraction and an assertion of the same fact at the same `t`, and drop an assertion of a fact already asserted. Avoid re-sending unchanged facts where history matters.

## History

- **Before 23 November 2026:** seen on a patched build and set aside; first described, wrongly, as "a guarded write records a retraction and an assertion".
- **23 November 2026:** reproduced on the stock v4.2.9 release with six write forms (unguarded writes are affected too, and the write is not the cause); the cause traced to the history reader by controlling each index step; fixed as `f5689bf` on `full-patch` and committed to `main` as `366220b`, with the tests above. On a binary built from `f5689bf` both scripts gave one assertion in every state, in the default graph and in a named graph.
- **26 November 2026:** this record written.

## Related

- [Bug 5](05-gc-deletes-live-history-files.md): another history read that depends on index state.

## Reproduction

```bash
python3 -I known-issues/08-history-invents-retraction/history_resend_by_index_state.py <fluree-binary> <port> [output.json]
python3 -I known-issues/08-history-invents-retraction/resend_index_repro.py <fluree-binary> <port> <output.json>
```

- `history_resend_by_index_state.py` starts `fluree server run --memory -- --reindex-min-bytes=100000000 --indexer-catchup-interval-secs=2`, so each index step happens only when asked (the incremental build in step 3 is the catch-up sweep's). Stock prints `same history answer in steps 2, 3 and 4: False` and exits 1; the fix prints `True` and exits 0. `RESEND_GRAPH=<graph IRI>` runs it in a named graph; only a build with named-graph history (the `full-patch` branch) answers that.
- `resend_index_repro.py` prints one line per write form and mode with the commit's assertion and retraction counts and the history before and after a reindex. It always exits 0; read the lines. Stock shows `('4', 'false'), ('4', 'true')`-style pairs right after the re-send in five of six lines; the fix shows the single original assertion in all six.

Recorded outputs, 23 November 2026: by index state on [stock](08-history-invents-retraction/results-index-states-stock-4.2.9.json), on the [full-patch build before the fix](08-history-invents-retraction/results-index-states-full-patch-before-fix.json) and [with the fix](08-history-invents-retraction/results-index-states-fixed.json); in a named graph [before](08-history-invents-retraction/results-index-states-named-graph-before-fix.json) and [with the fix](08-history-invents-retraction/results-index-states-named-graph-fixed.json); by write form on [stock](08-history-invents-retraction/results-write-forms-stock-4.2.9.json) and [with the fix](08-history-invents-retraction/results-write-forms-fixed.json). Temporary paths in them are shown as `<tmp>`.
