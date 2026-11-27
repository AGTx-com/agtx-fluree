# 7. A signed transaction's envelope is not kept on a single server

First fixed in a private engine patch for an earlier release, described then as an envelope lost when a write is retried; the date first found is not recorded. Confirmed on the stock v4.2.9 release (`3b8d19e649a6`) on 23 November 2026, while rerunning that patch's fixes against stock: every signed write on a single server is affected, not only retries. **Fixed by `f868a61`** on `main` (`transact: keep the signed envelope when commit options are cloned`). Not reported to Fluree.

**Severity: lost provenance.** A signed write commits with no copy of the signed request, so the signature can no longer be checked against the commit. With `store_raw_txn` set, the parsed body is stored in the envelope's place. Fluree's docs say a signed transaction always keeps its envelope.

**Cause in brief.** `CommitOpts` implements `Clone` by hand and leaves out the pending upload of the raw transaction, because that upload owns a task handle. Its comment assumes every builder clones the options before the upload is attached. The single-server committer, and the builder's optimistic retry loop, clone the options for every attempt after the server has attached the signed envelope, so the clone commits without it. The fix resolves the upload to a content ID before any clone.

## What goes wrong

On a single server with `--data-auth-mode=optional`, one insert is sent as a compact JWS (`Content-Type: application/jose`) signed by a `did:key` identity:

```
POST /v1/fluree/insert/envelope-repro:main
Content-Type: application/jose

<header>.<payload: {"insert": {"@id": "urn:x:c", "urn:x:note": "signed"}, "exp": …, "jti": "signed-1"}>.<signature>
```

It commits (HTTP 200). Reading the commit back through the ledger's commit export (`GET /v1/fluree/commits/<ledger>`, storage proxy) shows no raw transaction blob for it.

## Scope, measured

[`signed_envelope_repro.py`](07-signed-envelope-dropped/signed_envelope_repro.py), file storage, ephemeral keys, 2 runs on stock with identical results. "Fixed" is the fork's full-patch build, which carries this fix.

| Commit | Expected | Stock | Fixed |
|---|---|---|---|
| A. Unsigned insert | no raw transaction | no raw transaction | no raw transaction |
| B. Insert signed as a compact JWS | the exact JWS | **no raw transaction** | the exact JWS (its SHA-256 equals the sent JWS's) |

- **No retry is involved.** The envelope is lost on a plain first attempt.
- **The export does return an envelope when one is stored:** the fixed build's result comes back through the same export.
- **Over HTTP, `opts.storeRawTxn` had no effect** for an unsigned write in these tests; the docs make storing an unsigned payload a Rust-API option (`TxnOpts::store_raw_txn`).

**Not run:** the Raft (clustered) path, which by the source keeps the envelope; signed JSON-LD updates and SPARQL updates; a write that does retry on a conflict; a failed envelope upload.

## Cause

**In plain terms:** the engine copies the commit options for each attempt, and the copy forgets the signed request.

**In the source** (stock v4.2.9):
- `impl Clone for CommitOpts` (`fluree-db-transact/src/commit.rs:237-245`) sets `raw_txn_upload: None`. Its comment: a `PendingRawTxnUpload` owns a Tokio `JoinHandle` and a drop-guard release task and cannot be cloned safely, and "all current builder paths clone the base CommitOpts *before* attaching the spawned upload". The type's own doc comment repeats that assumption.
- `LocalCommitter` (`fluree-db-consensus/src/local.rs:197`) clones the options for every attempt, after the server has attached the envelope. So does the builder's optimistic staging loop (`fluree-db-api/src/tx_builder.rs`).
- Fluree's Raft path takes the upload before it clones, so it keeps the envelope (`fluree-db-consensus/src/raft/queued_transactor.rs:544-555`).

## What the standards say

- **Fluree's docs.** `docs/concepts/time-travel.md`, "Storage Implications": transaction JSON is not stored by default; a transaction opts in with `TxnOpts::store_raw_txn`, "and a signed transaction always keeps its envelope".
- **RFC 7515 (JWS)** defines the signature over the exact header and payload bytes, so only the original compact serialization can be re-verified; a parsed body stored in its place cannot.

## The fix

`f868a61` on `main`:
- `CommitOpts::resolve_raw_txn` (`fluree-db-transact/src/commit.rs`) waits for a pending upload and stores its content ID in `raw_txn_id`; an ID that is already set is kept and never replaced by a parsed body.
- A failed upload fails the transaction, so nothing commits without its envelope.
- `LocalCommitter` (`fluree-db-consensus/src/local.rs`) and the builder's retry loop (`fluree-db-api/src/tx_builder.rs`) call it before cloning, and the raw-body upload is skipped when an ID is already set.
- Tests: `fluree-db-consensus/tests/raw_txn_retention.rs` covers the local committer with and without `store_raw_txn`, a cached ledger handle, and a failed upload that must not advance the ledger. That test file was not rerun after the port to v4.2.9, for lack of disk space at the time; the reproduction above shows the fix working on a v4.2.9 build.

**Cost:** by the code, a signed transaction now waits for its envelope upload to finish before staging starts, instead of overlapping the two; the upload itself is the one content-store write stock was meant to make already. No timing was measured.

## Workaround

Keep the signed request outside the ledger, for example in the client's own store keyed by the commit's `t`. By the source, a Raft deployment keeps the envelope; that was not tested.

## History

- **Before 22 November 2026:** fixed in a private engine patch for an earlier release, described as "lost when the write is retried"; the date first found is not recorded.
- **22 November 2026:** ported to v4.2.9.
- **23 November 2026:** confirmed on the stock v4.2.9 release, 2 runs: no retry is needed, every signed write on a single server loses its envelope, so the earlier description was too narrow. Committed to this fork as `f868a61` on `main` (also on `full-patch`).
- **26 November 2026:** this record written.

## Related

- [Bug 16](16-signed-write-provenance.md): with this fix, a signed write's `f:txn` names its stored request, the only metadata that tells a signed write from one that only named an identity.
- [Bug 15](15-credential-transactions-documented-not-built.md) and [bug 17](17-credential-word-refuses-data.md), the other signing defects: credential transactions documented but always refused, and ordinary data mistaken for a credential.

## Reproduction

```bash
python3 known-issues/07-signed-envelope-dropped/signed_envelope_repro.py <fluree-binary> <port> [output.json]
```

Needs the Python `cryptography` package (Ed25519 signing; Fluree's CLI cannot sign a transaction), so it runs without `-I`. It starts `fluree server run` with file storage in a temporary directory, makes ephemeral keys with `fluree token keygen`, and reads the commit export back with a storage-proxy token. Stock prints `B signed JWS   t=3  raw transaction blobs=0 holds the exact JWS=False` and `signed envelope kept with its commit: False (docs: True)`, and exits 1. The fix prints `raw transaction blobs=1 holds the exact JWS=True` and exits 0. The output records SHA-256 digests only, never the token.

Recorded outputs, 23 November 2026: [stock](07-signed-envelope-dropped/results-stock-4.2.9.json), [full-patch build](07-signed-envelope-dropped/results-full-patch.json). The signer's ephemeral `did:key` was removed from them.
