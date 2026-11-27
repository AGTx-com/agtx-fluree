# 15. Verifiable Credential transactions and ECDSA signatures are documented as working but always refused

Found on 27 November 2026 while checking whether Fluree can take W3C Verifiable Credentials as transactions, by reading the credential module; confirmed the same day on the stock v4.2.9 release (`3b8d19e649a6`) and on the full-patch build (`64f56de8389c`). **Not fixed.** Not reported to Fluree.

**Severity: docs only; fails closed.** Nothing is written wrongly and no check is skipped: every refused request is refused before anything commits. The cost is that two of Fluree's pages describe signing features that don't exist, so anyone following them builds against a refusal, and the refusal's message points at a build flag that doesn't help.

**Cause in brief.** The credential module verifies one thing: a compact JWS signed with EdDSA. `verify_vc_object` returns "not enabled" whether or not the `vc` feature is compiled in, because a credential's Data Integrity proof needs RDF canonicalization (URDNA2015), which Fluree doesn't have; `verify_jws` refuses any `alg` other than `EdDSA`. Fluree's reference page says exactly that. Its concept page on verifiable data and its signed-transactions page say the opposite.

## What goes wrong

The Verifiable Credential transaction from `docs/transactions/signed-transactions.md`, posted as that page says (`Content-Type: application/vc+ld+json` to `/upsert`), is refused:

```
HTTP 401 {"error": "Credential error: VerifiableCredential verification not enabled (requires 'vc' feature)",
          "@type": "err:policy/InvalidCredential"}
```

An ES256 JWS, the algorithm `docs/concepts/verifiable-data.md` uses in its examples and lists as supported, properly signed with a P-256 key, is refused too:

```
HTTP 401 {"error": "Credential error: Unsupported algorithm: ES256. Expected EdDSA", ...}
```

An EdDSA compact JWS carrying the same kind of insert is accepted.

## Scope, measured

On both builds, with the [reproduction](#reproduction), the results are identical:

| Case | Result |
|---|---|
| A. The docs' credential, `application/vc+ld+json`, to `/upsert` | 401, "VerifiableCredential verification not enabled (requires 'vc' feature)" |
| B. The same credential, `application/json` | 401, the same message: the server routes a JSON body to credential checking by its shape, not its content type (bug 17) |
| C. The same credential with its proof value replaced by garbage | 401, the same message: the proof is never examined |
| D. An ES256 compact JWS, properly signed | 401, "Unsupported algorithm: ES256. Expected EdDSA" |
| E. An EdDSA compact JWS (control) | 200, committed |

**Not run:** ES384 and ES512, which the same check refuses by the source (any `alg` other than `EdDSA`); a build with the `vc` feature, which by the source returns the same error, since both branches of `verify_vc_object` do; signed queries, which use the same verification.

## Cause

**In plain terms:** the credential module only knows how to check an EdDSA JWS. Everything else the docs describe was planned and never built.

**In the source** (stock v4.2.9):
- `fluree-db-credential/src/lib.rs`, `verify_vc_object` (line 203): under `#[cfg(feature = "vc")]` and without it, it returns `Err(CredentialError::VcNotEnabled)` (lines 211 and 216). The comment says why: "VC verification requires JSON-LD URDNA2015 canonization. This is NOT implementable with the current Rust json-ld crate which only provides RFC 8785 JSON canonicalization." The crate's own header says the same: "VerifiableCredential format (feature-gated, requires JSON-LD canonization) … which is not yet implemented."
- `fluree-db-credential/src/jws.rs`, line 93: `if header.alg != "EdDSA"` returns `UnsupportedAlgorithm`.
- `fluree-db-credential/src/error.rs`, line 54: the `VcNotEnabled` message, "VerifiableCredential verification not enabled (requires 'vc' feature)", which suggests a build flag would help; it wouldn't.

## What the docs and the standards say

**Fluree's reference page is right:** `docs/reference/compatibility.md` says Verifiable Credentials are "Planned (not yet implemented)" ("The credential module currently supports JWS verification only. Full VC support (proof verification, JSON-LD canonicalization) is planned but not yet available", line 281) and that EdDSA is the "Only supported algorithm" (line 268). Its feature table still describes the `credential` feature as "DID/JWS/VerifiableCredential support for signed queries/transactions" (line 314).

**Two pages say otherwise:**
- `docs/concepts/verifiable-data.md`: "Fluree supports cryptographically signed transactions using industry-standard formats (JWS and Verifiable Credentials)" (line 3); "Fluree supports Verifiable Credentials (VC) for credential-based transactions" (line 85); "Fluree supports standard signature algorithms" listing ES256, ES384, ES512 and Ed25519 (lines 158–163), with ES256 in its JWS examples (lines 70 and 148); "Fluree automatically verifies signed transactions" (line 221); a credential-revocation section (line 387); and "Signatures are stored with transactions … Queryable: Signatures can be queried like any data" (lines 522–527).
- `docs/transactions/signed-transactions.md`: a "Verifiable Credentials" section (line 93) with an `Ed25519Signature2020` credential and the `curl` command posting it as `application/vc+ld+json` (line 129). Its "Supported Algorithm" section (line 133) does say EdDSA only. Its DID section lists `did:web` and `did:ion` as ways to identify authors (lines 145–158); `compatibility.md` lists both as not supported, and the server derives the signer from the JWS's embedded key.

**The standards:** W3C Verifiable Credentials Data Model 2.0 has two securing mechanisms. Data Integrity proofs (such as `Ed25519Signature2020`, or the `eddsa-rdfc-2022` cryptosuite) sign a canonicalized graph, so verifying one needs RDF canonicalization. Credentials secured with JOSE (VC-JOSE-COSE) are a compact JWS whose payload is the credential, and need none. Fluree's JWS path accepts the second: the probe for `full-patch`'s [planned feature 15](https://github.com/AGTx-com/agtx-fluree/blob/full-patch/features/15-pinned-json-ld-contexts.md) sends one as a transaction.

## The fix (proposed)

- **Docs:** make `verifiable-data.md` and `signed-transactions.md` describe what works: a compact EdDSA JWS. List credentials with embedded proofs, ES256/ES384/ES512, `did:web` and `did:ion` as not supported, as `compatibility.md` does; replace the ES256 examples with EdDSA ones; say what is actually stored and queryable (bug 16); describe the `credential` feature as JWS only.
- **The error message:** drop "requires 'vc' feature". Say instead that credentials with embedded proofs aren't supported, and that a signed request must be a compact EdDSA JWS.
- **Not proposed here:** verifying Data Integrity proofs, which needs RDF canonicalization and is a feature, not a fix.

## Workaround

Sign the request as a compact EdDSA JWS (`application/jose`). For a W3C credential, secure it with JOSE: a compact JWS whose payload is the credential is accepted as a signed transaction. Its `@context` then meets the documented URL-context deviation (see [Documented deviations](README.md#documented-deviations-that-store-wrong-data)).

## History

- **27 November 2026:** found by reading `fluree-db-credential` while checking whether credentials can be Fluree transactions; the docs read against `compatibility.md`; probed on stock v4.2.9 and the full-patch build, 1 run each; this record written.

## Related

- [Bug 17](17-credential-word-refuses-data.md): the same credential check refuses ordinary, unsigned data whose type contains "Credential" (case B above is that detection).
- [Bug 16](16-signed-write-provenance.md): the same signed-transactions page documents provenance fields that don't exist.
- [Bug 7](07-signed-envelope-dropped.md): on stock, a signed request's envelope isn't kept.

## Reproduction

```bash
python3 known-issues/15-credential-transactions-documented-not-built/credential_docs_probe.py <fluree-binary> [port]
```

Needs the Python `cryptography` package (Ed25519 and P-256 signing), so it runs without `-I`. It starts an in-memory server, gives an ephemeral `did:key` an allow-all policy with an unsigned setup write, and sends cases A to E. Recorded outputs, 27 November 2026: [stock](15-credential-transactions-documented-not-built/results-stock-3b8d19e649a6.txt), [full-patch build](15-credential-transactions-documented-not-built/results-full-patch-64f56de8389c.txt).
