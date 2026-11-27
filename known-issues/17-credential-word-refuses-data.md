# 17. Unsigned data is refused as an unverifiable credential when its type contains "Credential"

Found on 27 November 2026 while auditing bug 15, by reading how the server decides that a request body is a credential; confirmed the same day on the stock v4.2.9 release (`3b8d19e649a6`) and on the full-patch build (`64f56de8389c`). **Not fixed.** Not reported to Fluree.

**Severity: wrong refusals.** An ordinary, unsigned JSON-LD insert is refused with HTTP 401 and a credential error, which reads like an authentication failure, when its top-level node's type merely contains the word "Credential", or when it has a top-level `jws` key. Nothing is written wrongly, but valid data can't be written in that shape. Real vocabularies hit it: schema.org's `EducationalOccupationalCredential`, the W3C credentials vocabulary's own classes, and any application class named something-Credential.

**Cause in brief.** The server guesses whether a JSON body is a Verifiable Credential from its shape, whatever its content type: a top-level `type` or `@type` whose value contains the substring "Credential", or any top-level `jws` key. A match is sent to credential verification, which always refuses a credential (bug 15). Only the top level is checked, so the same node inside an `@graph` envelope is accepted.

## What goes wrong

```json
{ "@context": { "ex": "http://example.org/", "schema": "http://schema.org/" },
  "@id": "ex:diploma", "@type": "schema:EducationalOccupationalCredential", "schema:name": "BSc" }
```

posted unsigned to `/insert` as `application/json` is refused:

```
HTTP 401 {"error": "Credential error: VerifiableCredential verification not enabled (requires 'vc' feature)",
          "@type": "err:policy/InvalidCredential"}
```

The same node inside `"@graph": [ … ]` commits.

## Scope, measured

On both builds, with the [reproduction](#reproduction), the results are identical:

| Case, an unsigned `/insert` | Result |
|---|---|
| 1. One node typed `schema:EducationalOccupationalCredential` | 401 |
| 2. The same node inside an `@graph` envelope | 200, committed |
| 3. One node typed `ex:Credentialing`: the word only inside a longer name | 401 |
| 4. One node typed `["ex:Thing", "ex:CredentialRecord"]` | 401 |
| 5. One node typed through a `type` alias (`"type": "@type"`) as `ex:BadgeCredential` | 401 |
| 6. One node with a property named `jws`, mapped to an IRI by the context | 401 |
| 7. One node typed `ex:Certificate` (control) | 200, committed |
| 8. One node typed `ex:Thing` (control) | 200, committed |

**Not run:** the other routes that use the same detection: queries, GraphQL, the graph store and streamed queries all take the same `MaybeCredential` extractor (`fluree-db-server/src/routes/`). A JSON-LD query rarely has a top-level type, so those are less likely to be hit.

## Cause

**In plain terms:** the server looks for the word "Credential" at the top of the body and, if it finds it, insists on checking a signature that the engine can't check.

**In the source** (stock v4.2.9), `fluree-db-server/src/extract/credential.rs`:
- `looks_like_credential_json` (line 212) returns true when the body's top-level `type` or `@type` is a string, or an array holding a string, that `contains("Credential")`, or when the body has any top-level `jws` key.
- Line 331: when it returns true, the body goes to `credential::verify_credential(CredentialInput::Json(…))`, which reaches `verify_vc_object` and fails with `VcNotEnabled` (bug 15), mapped to HTTP 401 `err:policy/InvalidCredential`. The request's content type isn't consulted.

## What the docs say

No page says that such data is refused. `docs/reference/compatibility.md` says Verifiable Credentials are "Planned (not yet implemented)", so no JSON body should need to be treated as one; the JSON-LD insert docs accept any node object.

## The fix (proposed)

In `looks_like_credential_json`:
- treat a body as a credential only when its content type says so (`application/vc+ld+json`, `application/vc`), or when it has the structure of one: both a `credentialSubject` and a `proof`;
- treat a top-level `jws` key as a JSON-wrapped JWS only when its value is a compact JWS (the existing `looks_like_jws` test).

Then a refused credential gets bug 15's corrected message, and ordinary data is never routed to credential verification.

## Workaround

Send the node inside an `@graph` envelope (case 2), which the check doesn't look into.

## History

- **27 November 2026:** found while auditing bug 15, by reading the server's credential detection; probed on stock v4.2.9 and the full-patch build, 1 run each; this record written.

## Related

- [Bug 15](15-credential-transactions-documented-not-built.md): the verification these bodies are sent to always refuses.

## Reproduction

```bash
python3 -I known-issues/17-credential-word-refuses-data/credential_shape_probe.py <fluree-binary> [port]
```

Standard library only. It starts an in-memory server and sends cases 1 to 8 as unsigned inserts. Recorded outputs, 27 November 2026: [stock](17-credential-word-refuses-data/results-stock-3b8d19e649a6.txt), [full-patch build](17-credential-word-refuses-data/results-full-patch-64f56de8389c.txt).
