# 16. A signed write's provenance doesn't match the docs, and the Rust API adds `f:author` only when an index is built

Found on 27 November 2026 while probing W3C credentials as transactions, by comparing a signed write's commit metadata with Fluree's docs; confirmed the same day on the stock v4.2.9 release (`3b8d19e649a6`) and on the full-patch build (`64f56de8389c`) over HTTP, and with an engine test on the full-patch source (`ff91289`) for the Rust API. **Not fixed.** Not reported to Fluree.

**Severity: wrong answers.** Three things go wrong, and none loses or forges an identity: the signer is always recorded correctly as `f:identity`.
1. The provenance queries Fluree's docs give for signed transactions return nothing, on every build, before and after indexing.
2. On the Rust API's signed-write path, the same metadata query answers differently before and after an index build: the signer appears as `f:author` only after one, in the field Fluree's vocabulary defines as a user's claim, distinct from `f:identity`.
3. Over HTTP, nothing in a commit's metadata says it was signed: a signed write and an unsigned one naming the same identity through `opts.identity` look the same, unless the stored request can be checked (which needs bug 7's fix).

**Cause in brief.** Two code paths sign writes. The HTTP server's records the signer as the commit's identity and keeps the raw request, but never sets the commit's transaction signature (`txn_signature`). The Rust API's `credential_transact` sets both. When a commit is made, its metadata gets `f:identity` from either; the index build additionally writes a `txn_signature`'s signer as `f:author`. And `docs/transactions/signed-transactions.md` documents fields that don't exist: `f:signature`, `f:timestamp`, `f:assert`, and `f:author` as the signer.

## What goes wrong

**Over HTTP** (both builds, before and after `POST /v1/fluree/reindex`), the "Query provenance" query from `signed-transactions.md`, run verbatim, returns no rows; so does the same query against the ledger's `#txn-meta` graph, and so does the page's later audit query. The metadata actually holds, for the probe's five commits:

```
t=2  author    alice-claim        <- a user's f:author claim in the signed write's envelope
t=2  identity  did:key:z6Mk…      <- the signer
t=2  txn       bagbib…            <- the stored signed request (full-patch build only: bug 7)
t=3  identity  did:key:z6Mk…      <- a signed write with no claim
t=4  author    bob-claim          <- an unsigned write's claim
t=5  identity  did:key:z6Mk…      <- an unsigned write naming the same DID in opts.identity
     time      on every commit, epoch milliseconds
```

On stock, t=3 (signed) and t=5 (unsigned, `opts.identity`) have identical metadata.

**Over the Rust API** (`Fluree::credential_transact`, the engine test below):

```
before index build:
    t=2 f:identity "did:key:z6MkvDqG…"
    t=3 f:identity "did:key:z6MkvDqG…"
after index build:
    t=2 f:author   "did:key:z6MkvDqG…"
    t=2 f:identity "did:key:z6MkvDqG…"
    t=3 f:author   "did:key:z6MkvDqG…"
    t=3 f:identity "did:key:z6MkvDqG…"
```

## Scope, measured

- **HTTP, both builds, 1 run each:** a signed envelope insert with an `f:author` claim, a signed insert without one, an unsigned insert with a claim and an unsigned insert with `opts.identity`; the three docs queries and a listing of `f:identity`, `f:author`, `f:time`, `f:txn`, `f:signature` and `f:timestamp`, before and after a reindex. The two builds agree except that only the full-patch build has `f:txn` (bug 7). Reindexing changes nothing over HTTP, because the HTTP path never sets `txn_signature`.
- **Rust API, 1 run:** an engine test on the full-patch source (`ff91289`) makes two signed updates with `credential_transact`, reads the metadata, rebuilds the index and reads it again. The code involved (`credential_transact`, the commit-time identity code and the indexer's author block) is unchanged from stock v4.2.9. The test's second update also carried an `f:author` claim in its update body; it was recorded on neither side, as expected, since the docs say claims ride the envelope form.

**Not run:** the incremental index build (the test rebuilt the index in full; the indexer writes the author at both of its emission sites, lines 443 and 2030); the Raft (clustered) path; signed SPARQL updates.

## Cause

**In plain terms:** the index build writes something the commit didn't, but only for the path that records a signature; and the page about signed transactions describes fields that were never built.

**In the source** (stock v4.2.9):
- `fluree-db-api/src/tx.rs`, `credential_transact` (line 4830): the Rust API's signed path sets the commit's identity, its raw request and its transaction signature (`.with_txn_signature(…)`, line 4908).
- `fluree-db-server/src/routes/transact.rs`, `build_commit_opts` (line 316): the HTTP path sets the identity and the raw request only; nothing on the server sets `txn_signature`.
- `fluree-db-transact/src/commit.rs`, line 781: at commit time, the metadata's `f:identity` is the transaction signature's signer, or else the commit's identity. No `f:author` is written from either.
- `fluree-db-indexer/src/run_index/resolve/resolver.rs`, lines 443–454 and 2030–2041: "ledger:author (STRING) -- transaction signer DID": the index build writes a `txn_signature`'s signer as `f:author`.
- `fluree-vocab/src/lib.rs`, module `db`: there is no `signature`, `timestamp` or `assert` term; the commit's time is `time`, and `author` is a user claim (`TXN_META_USER` is `message` and `author`).

## What the docs say

- **Right:** `docs/transactions/overview.md` (lines 293–294) and `docs/reference/vocabulary.md` (lines 37–38): `f:identity` is the "authenticated identity acting on the transaction … verified DID for signed requests, otherwise from `opts.identity`"; `f:author` is an "Author claim — user-supplied via `f:author` in the transaction body (optional). Distinct from `f:identity`"; `f:time` is the commit's time in epoch milliseconds (line 32). The HTTP path matches these; the Rust API path contradicts them after an index build.
- **Wrong:** `docs/transactions/signed-transactions.md`, "Transaction Provenance" (line 167): "Signed transactions include author information in commit metadata", with `author`, `signature` and `timestamp` fields, and a query using `f:author` and `f:timestamp` (line 191) without naming the `#txn-meta` graph where commit metadata lives. Its audit query (line 432) adds `f:assert`, which doesn't exist either.
- **Wrong for HTTP:** `docs/security/commit-signing.md` (line 22) says a transaction signature is "Stored in: Commit envelope (`txn_signature`)". Only the Rust API's path stores one.

## The fix (proposed)

- **Engine:** stop the index build from writing a transaction signature's signer as `f:author` (`resolver.rs`, lines 443–454 and 2030–2041). Metadata is then the same before and after indexing, and `f:author` stays the user claim the vocabulary defines; the signer remains `f:identity`. Writing the author at commit time too would remove the difference but contradict the vocabulary, so it is not proposed.
- **Docs:** rewrite the provenance section of `signed-transactions.md` to query `f:identity` and `f:time` `FROM <ledger#txn-meta>`, with `f:txn` for the stored request, and drop `f:signature`, `f:timestamp` and `f:assert`; correct the "Stored in" row of `commit-signing.md`.
- **Open:** a system-set marker that a commit was signed, distinct from `opts.identity`, would be a feature, not a fix.

## Workaround

Read the signer from `f:identity` and the time from `f:time`, in `<ledger#txn-meta>`. Don't read `f:author` as the signer. To tell a signed write from one that only named an identity, check that `f:txn` names a stored request that is a JWS; that needs bug 7's fix.

## History

- **27 November 2026:** the HTTP metadata seen in a credential probe didn't match `signed-transactions.md`; traced through the source; probed over HTTP on stock v4.2.9 and the full-patch build, 1 run each, and over the Rust API with an engine test, 1 run; this record written.

## Related

- [Bug 7](07-signed-envelope-dropped.md): on stock, the signed request isn't kept, so `f:txn` is missing and a signed write can't be told from an unsigned one at all.
- [Bug 8](08-history-invents-retraction.md): another answer that changes with index state.
- [Bug 15](15-credential-transactions-documented-not-built.md): the same page documents credential transactions that are always refused.

## Reproduction

**HTTP:**

```bash
python3 known-issues/16-signed-write-provenance/provenance_probe.py <fluree-binary> [port]
```

Needs the Python `cryptography` package (Ed25519 signing), so it runs without `-I`. Recorded outputs, 27 November 2026: [stock](16-signed-write-provenance/results-http-stock-3b8d19e649a6.txt), [full-patch build](16-signed-write-provenance/results-http-full-patch-64f56de8389c.txt).

**Rust API:** copy [`bug16_author_after_index.rs`](16-signed-write-provenance/bug16_author_after_index.rs) into `fluree-db-api/tests/`, register it in `fluree-db-api/Cargo.toml` (the crate sets `autotests = false`):

```toml
[[test]]
name = "bug16_author_after_index"
path = "tests/bug16_author_after_index.rs"
required-features = ["credential"]
```

and run `cargo test -p fluree-db-api --features credential --test bug16_author_after_index -- --nocapture`. Recorded output: [full-patch source `ff91289`](16-signed-write-provenance/results-rust-api-full-patch-ff91289.txt).
