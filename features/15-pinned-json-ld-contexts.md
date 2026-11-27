# 15. Pinned JSON-LD contexts

**Status: planned, not started.** For this branch, behind switches that are off by default, after the current engine release work. Generic: it names no application, vocabulary or deployment. **No context is ever fetched from the network:** a pinned context is a local file, checked against its SHA-256 when the server starts.

**What it fixes.** Fluree reads a URL in `@context` as a vocabulary prefix and never loads the document behind it. With the standard W3C verifiable-credential context, a write succeeds and stores wrong facts, with no warning. The feature lets a server map a context URL to a pinned local copy and refuse any URL context it has no copy for.

## What goes wrong today

A W3C Verifiable Credential 2.0 must start its `@context` with `https://www.w3.org/ns/credentials/v2` (VC Data Model 2.0, §4.3). That URL points to a context document: the dictionary saying that `id` is the node's own IRI, `type` its class, `issuer` and `credentialSubject` links to other nodes, and `validFrom` a date. Sent to Fluree as a signed transaction (a compact JWS, `application/jose` or `application/vc+jwt`), the credential is accepted, the signature is checked and the signer is recorded, but Fluree never opens the dictionary, so the facts are wrong (case 2 of the reproduction, the same on stock v4.2.9 and on the full-patch build):

```
_:b0  <https://www.w3.org/ns/credentials/v2/id>                 "urn:uuid:acceptance-2"
_:b0  <https://www.w3.org/ns/credentials/v2/type>               "VerifiableCredential"
_:b0  <https://www.w3.org/ns/credentials/v2/issuer>             "did:key:z6Mk…"
_:b0  <https://www.w3.org/ns/credentials/v2/credentialSubject>  _:b1
_:b1  <https://www.w3.org/ns/credentials/v2/id>                 "urn:ex:agreement:a2"
_:b1  <urn:ex:acceptedBy>                                       _:b2
```

`id` and `type` lose their special meaning, so the credential and its subject become blank nodes, `issuer` and `validFrom` become text, and the claims never reach the subject `urn:ex:agreement:a2`. With an equivalent inline context (case 3), the same credential gives the right facts: a node typed `https://www.w3.org/2018/credentials#VerifiableCredential` with its issuer, `validFrom` as `xsd:dateTime` and `credentialSubject`, and the claims on `urn:ex:agreement:a2`.

## Cause in the source

- **A URL context becomes `@vocab`.** `fluree-graph-json-ld/src/context.rs`, `parse_guarded`: a string context sets `active.vocab` to the string plus a trailing slash ("String context: default vocab"). Nothing is read, locally or remotely.
- **There is no setting for a local copy.** The parser knows only contexts written inline. A ledger's default context applies only to queries that bring no context of their own, and to Cypher writes (`docs/concepts/iri-and-context.md`), never to JSON-LD writes. Vocabulary data in the ledger (for example the W3C credentials vocabulary as Turtle) doesn't help: the parser never reads ledger data.
- **W3C's own file wouldn't parse unchanged.** `parse_container_string` accepts only `@list`, `@set`, `@language` and `@index`, and refuses any other `@container` value while a context is parsed, even for a term the document never uses. The W3C credential context defines some terms with `"@container": "@graph"`, among them `proof` and `verifiableCredential`.

## What the standards and Fluree's docs say

- **JSON-LD 1.1 Processing Algorithms, Context Processing:** a string in `@context` is an IRI reference to a context, which the processor must dereference; if it can't, processing fails with `loading remote context failed`. Dereferencing may use a document loader with local or cached copies; it need not touch the network.
- **JSON-LD 1.1, protected terms:** a protected term may be redefined only with an identical definition; anything else is a `protected term redefinition` error.
- **Fluree's docs** list this as a deviation from JSON-LD 1.1: "A remote context is not fetched. A string `@context` sets `@vocab` to that string, so `"@context": "https://schema.org"` reads bare terms as schema.org IRIs; inline any other definitions the document needs" (`docs/reference/compatibility.md`). The same page lists `@container` values `@id`, `@type` and `@graph` as refused.

**Why this is a feature on this branch, not a fix on `main`:** the code does exactly what Fluree's docs promise; the promise itself deviates from the standard. Every standard-conforming change makes the same input behave differently from the docs: a pinned URL loads a document instead of becoming `@vocab`, and strict mode refuses a URL that today becomes `@vocab`. So the change goes behind switches here, and `main` records the hazard among its documented deviations.

## The change

**Required, two parts:**
1. **Pinned contexts.** A startup setting names a manifest of pinned context documents: for each, the URL it stands for, a local file and the file's SHA-256. At start-up the server reads each file, refuses to start if a hash doesn't match or a file doesn't parse, and parses each document once. When a document's `@context`, or an item of a `@context` array, equals a pinned URL, the parser processes the pinned document as if it were written inline, with its `@protected`, `@version`, `@vocab` and type-scoped contexts. This applies to every JSON-LD input: transactions, queries and bulk import.
2. **Strict contexts.** A switch that refuses any URL context that isn't pinned, with an error naming the URL, instead of reading it as `@vocab`. With the switch off, a URL that isn't pinned keeps today's documented behaviour, so `"@context": "https://schema.org"` still works unchanged.

**A trimmed copy instead of W3C's exact file.** Because W3C's file uses container definitions Fluree refuses, a deployment pins its own trimmed copy: W3C's file with the definitions that use refused `@container` values removed (among them `proof` and `verifiableCredential`), everything else unchanged. The pin still names W3C's URL, with the deployment's file and hash. Nothing is lost for credentials secured with JOSE, which carry no embedded `proof`, or for a deployment that stores no presentations. The manifest records what the copy was trimmed from, so the difference stays visible.

**Optional, later, part 3: accept definitions, refuse use.** A term definition whose `@container` is `@graph`, `@id` or `@type` (alone or with `@set` or `@index`) is accepted when a context is parsed; a document that actually uses such a term is refused, naming the term. W3C's exact files (credentials v2, DID v1, Data Integrity v2) could then be pinned unchanged, and the trimmed copy retired.

**Proposed switch names**, following the fork's `FLUREE_…` startup switches: `FLUREE_JSONLD_CONTEXTS=<path to manifest>` and `FLUREE_JSONLD_STRICT_CONTEXTS=1`, each with a matching `fluree server run` flag. Both off by default: the build then behaves exactly as stock.

**Proposed manifest:**

```json
{
  "contexts": [
    { "url": "https://www.w3.org/ns/credentials/v2",
      "file": "credentials-v2.trimmed.jsonld",
      "sha256": "<hash of the pinned file>",
      "trimmedFrom": { "sha256": "<hash of W3C's file>",
                       "removed": ["proof", "verifiableCredential"] } }
  ]
}
```

`trimmedFrom` is informational: the server checks `sha256` only.

## Acceptance tests

- **The credential case:** case 2 of the reproduction, with the trimmed credentials v2 copy pinned, gives the same facts as case 3, with the correct `https://www.w3.org/2018/credentials#` IRIs, including the credential's own type, and the claims on the subject.
- **A pinned file whose hash doesn't match, or that doesn't parse:** the server refuses to start and names the file. W3C's untrimmed file, without part 3, is refused at start-up with an error naming the refused definition.
- **Strict on:** a URL context that isn't pinned is refused with an error naming the URL, and nothing is committed. **Strict off:** `"@context": "https://schema.org"` behaves exactly as on stock.
- **Protected terms:** an identical redefinition of a pinned context's protected term is accepted (case 6's pattern); a different one is refused with `protected term redefinition`.
- **Queries:** a JSON-LD query that names the pinned URL expands and compacts as if the context were inline.
- **Signed writes are unchanged:** the signature is checked, the signer recorded as `f:identity`, and the signed request kept (with bug 7's fix).
- **No network:** the tests run with outbound network blocked; a context is never fetched.
- **Cost:** pinned documents are parsed once at start-up; a request naming a pinned URL is no slower than one carrying the same context inline.
- **With part 3:** W3C's untrimmed file loads, and a document that uses `proof` with an embedded object is refused, naming the term.

## Not in scope

- Fetching contexts over the network, ever.
- Verifying credentials secured with Data Integrity proofs. Fluree's `verify_vc_object` (`fluree-db-credential/src/lib.rs`) always returns "not enabled", because it would need RDF canonicalization (URDNA2015 / RDFC-1.0), which Fluree doesn't have. Credentials secured with JOSE are a compact JWS and need none of it.
- Fluree's own `vc+ld+json` transaction format, which depends on that verification.

## Workaround until then

Start the context with the standard URL, then repeat, inline and word for word, the standard context's own definitions of each term the document uses (case 6):

```json
"@context": [
  "https://www.w3.org/ns/credentials/v2",
  { "id": "@id", "type": "@type",
    "issuer":            { "@id": "https://www.w3.org/2018/credentials#issuer", "@type": "@id" },
    "validFrom":         { "@id": "https://www.w3.org/2018/credentials#validFrom",
                           "@type": "http://www.w3.org/2001/XMLSchema#dateTime" },
    "credentialSubject": { "@id": "https://www.w3.org/2018/credentials#credentialSubject", "@type": "@id" } },
  { "ex": "urn:ex:" }
]
```

On both builds the claims then land on the subject, and the issuer, `validFrom` and `credentialSubject` are right. The credential's own type still comes out as `https://www.w3.org/ns/credentials/v2/VerifiableCredential`, because the standard context's definition of `VerifiableCredential` carries a scoped context that can't be repeated identically. Not yet checked: that a JSON-LD processor loading the real context accepts the repeated definitions.

## History

- **27 November 2026:** found while probing whether a W3C credential secured with JOSE can be a Fluree transaction. Stock v4.2.9 (`3b8d19e649a6`) and the full-patch build (`64f56de8389c`) behave identically, except that stock drops the signed request (bug 7). The same day, the trimmed pinned copy was chosen so that part 3 is optional.

## Reproduce

[`15-pinned-json-ld-contexts/vc_jose_probe.py`](15-pinned-json-ld-contexts/vc_jose_probe.py) starts an in-memory server, gives one Ed25519 `did:key` an allow-all policy, and sends the six cases. Run it as `python3 vc_jose_probe.py <fluree-binary>`; it needs the `cryptography` package. Recorded outputs: [stock v4.2.9](15-pinned-json-ld-contexts/results-stock-3b8d19e649a6.txt) and [the full-patch build](15-pinned-json-ld-contexts/results-full-patch-64f56de8389c.txt).
