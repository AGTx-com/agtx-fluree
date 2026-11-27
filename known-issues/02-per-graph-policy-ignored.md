# 2. A named graph's own policy defaults (`f:GraphConfig`) are ignored

Found on 20 November 2026 on v4.2.4, while qualifying per-graph policy defaults as a replacement for an application's own graph-scoping layer. Confirmed on the stock v4.2.9 release (`3b8d19e649a6`) on 23 November 2026, through the CLI and through signed HTTP requests. **Fixed by `9074b1a`** on `main` (`policy: apply each f:GraphConfig's own policy defaults`). Not reported to Fluree.

**Severity: report privately.** Someone who follows Fluree's own "Different policy per graph" example believes a graph is locked when it is open: any identity writes into it and reads all of it. Fluree's docs disagree with each other about whether per-graph policy exists (see [What the standards say](#what-the-standards-say)), so Fluree may answer by correcting a page rather than the engine; it stays a private report either way.

**Cause in brief.** Fluree resolves a write's SHACL settings per graph, but builds its policy from the ledger-wide settings only: the policy context is resolved with no graph (`resolve_effective_config(c, None)`). An `f:GraphConfig` entry's own `f:policyDefaults` therefore never applies, on writes or reads. The fix compiles one policy context per configured graph and judges each fact by the context of the graph it is in.

## What goes wrong

This is the config from Fluree's `docs/ledger-config/README.md`, "Different policy per graph", with only the ledger name changed:

```trig
@prefix f: <https://ns.flur.ee/db#> .
GRAPH <urn:fluree:repro:main#config> {
  <urn:fluree:repro:main:config:ledger> a f:LedgerConfig ;
    f:policyDefaults [ f:defaultAllow true ; f:overrideControl f:OverrideAll ] ;
    f:graphOverrides (
      [ a f:GraphConfig ;
        f:targetGraph <http://example.org/sensitive> ;
        f:policyDefaults [
          f:defaultAllow false ;
          f:policySource [ a f:GraphRef ; f:graphSource [ f:graphSelector f:defaultGraph ] ] ;
          f:overrideControl f:OverrideNone ] ]
    ) .
}
```

The README says the sensitive graph "requires policy rules and cannot be overridden at query time". No policy rule exists, so the identity `<urn:x:rob>` (no policy class) must be refused there:

```sparql
INSERT DATA { GRAPH <http://example.org/sensitive> { <urn:x:item> <urn:x:note> "c" } }   # as urn:x:rob
```

On stock the write commits, and Rob reads every fact in the graph.

## Scope, measured

"Fixed" is the fork's full-patch build, which carries this fix as `94dc47a` on the `full-patch` branch (see [The fix](#the-fix) for how it differs from `main`).

**CLI, `--direct`, identity asserted with `--as`** ([`per_graph_policy_repro.py`](02-per-graph-policy-ignored/per_graph_policy_repro.py)):

| Request as Rob | Expected | Stock | Fixed |
|---|---|---|---|
| Write into the sensitive graph | refused | **allowed** | refused |
| The same write with `--default-allow` | refused | **allowed** | refused |
| Read the sensitive graph through `GRAPH <…> { … }` | no rows | **all 4 rows** | no rows |
| Read it through `FROM <ledger#graph>` | no rows | **all 4 rows** | no rows |
| Write into, and read, another graph | allowed | allowed | allowed |
| Control: ledger-wide `f:defaultAllow false`, `f:OverrideNone`, no graph override | refused, no rows | refused, no rows | refused, no rows |

**Signed HTTP** ([`per_graph_policy_signed_http.py`](02-per-graph-policy-ignored/per_graph_policy_signed_http.py); `fluree server run --memory -- --data-auth-mode=optional`; the writer is a `did:key` identity that signs each request as a compact JWS, so the server verifies it):

| Request | Expected | Stock | Fixed |
|---|---|---|---|
| Signed write into the sensitive graph | refused | **HTTP 200** | HTTP 403 |
| Signed write into another graph | allowed | HTTP 200 | HTTP 200 |
| Unsigned read of the sensitive graph afterwards | no rows | **its contents, including the signer's new fact** | no rows |
| Control: ledger-wide lock, signed writes to either graph | refused | HTTP 403 | HTTP 403 |

The controls show that stock applies policy defaults to these requests, signed or not; only the per-graph override is ignored. A request with no identity in `--direct` mode is the local administrator and is never checked; those rows in the outputs are informational.

**Not run:** JSON-LD and Cypher reads; `f:GraphConfig` policy classes and `f:policySource` other than the default graph; `#txn-meta` overrides.

**Still unchanged after the fix (not tested).** About ten code paths still check only the ledger-wide root flag, so a root (open) ledger default with a restrictive graph override can skip enforcement there: push staging, `graph_commit_builder.rs:312`, `tx.rs:1987`, `block_fetch.rs:411`, four sites in hydration and two in Cypher procedures. This list was made against the earlier patch and was not rechecked against `9074b1a`.

## Cause

**In plain terms:** the engine knows each written fact's graph and finds that graph's SHACL settings, but it picks one policy for the whole request from the ledger-wide settings.

**In the source** (stock v4.2.9): `build_transact_policy_context` (`fluree-db-api/src/policy_view.rs:427`) resolves the ledger config with `resolve_effective_config(c, None)` (`:440`), and `wrap_identity_policy_view` does the same for reads (`:662`). The `None` is the graph, so the result is always the ledger-wide group. The config resolver already understands `f:GraphConfig`, exact `f:targetGraph`, per-graph policy sources and override controls; it is never asked for a graph's own group when policy is built.

## What the standards say

No W3C standard covers access policy. Fluree's docs disagree:
- **For per-graph policy:**
  - `docs/ledger-config/README.md`, "Different policy per graph", gives this exact config.
  - `docs/ledger-config/override-control.md`, "Per-group truth tables", has the row ledger-wide `defaultAllow: true`, `OverrideAll`; per-graph `defaultAllow: false`, `OverrideNone`; query `defaultAllow: true` → **deny**, "Per-graph OverrideNone blocks query".
  - `docs/ledger-config/setting-groups.md` says every setting group may appear on `f:GraphConfig` unless noted, and that policy defaults apply to reads and writes.
- **Against it:** `docs/security/policy-in-queries.md`, "Multi-graph queries", says policies apply per fact whatever its graph and that there is no per-graph policy override; it suggests `f:onClass`, `f:onSubject` or separate ledgers instead.

The fix follows the ledger-config pages.

## The fix

`9074b1a` on `main`. `PolicyContext` gains compiled graph overrides keyed by target graph IRI. Each fact is judged by the context of the graph it is actually in, never by a value in the request.
- **Writes:** `build_transact_policy_context` compiles one context per `f:GraphConfig` target and shares compiled rules between targets with equal effective defaults. Unconfigured graphs fall back to the ledger group, not to the default graph's override. Modify checks select per flake.
- **Reads:** a policy view on a ledger with graph overrides uses the same compiled contexts, and the query enforcer (`fluree-db-query/src/policy/enforcer.rs`) selects one by the scanned graph. A `#txn-meta` graph uses an override that targets `f:txnMetaGraph`.
- `PolicyContext::is_root` is true only if the ledger default and every override are root, so a root ledger default no longer bypasses a restrictive override. Staging, `WHERE` filtering and pushes that carry graph drops use it.
- Class lookups, the query-result cache key and reasoned queries follow the overrides. Policy and reasoning defaults derive `PartialEq` so that equal defaults share a compiled context.
- The write path resolves cross-ledger schema and policy restrictions through one shared resolution context, so a model ledger's head is captured once. As on stock, only a cross-ledger `f:schemaSource` (one with `f:ledger`) feeds class-based policy checks. The `full-patch` version, `94dc47a`, also uses a local `f:schemaSource` there, which depends on a separate `full-patch` change; otherwise the two are the same.
- Tests: `view_policy_hash_covers_graph_overrides` (`fluree-db-policy/src/evaluate.rs`), `a_graph_override_class_policy_reads_subject_classes` (`fluree-db-query/src/policy/enforcer.rs`), `native_graph_selection_preserves_remote_governance_snapshot` (`fluree-db-api/tests/it_policy_cross_ledger.rs`).

**Cost:** when any override exists, `classify_view_predicate` treats every predicate as covered and gives up that read shortcut. Building the policy compiles one context per distinct set of effective defaults instead of one. No timing was measured.

## Workaround

Express the restriction without a per-graph override, as `docs/security/policy-in-queries.md` suggests: a closed ledger-wide default with targeted policies (`f:onClass`, `f:onSubject`, `f:onProperty`), or a separate ledger for the sensitive data.

## History

- **20 November 2026:** found on v4.2.4 while qualifying per-graph policy defaults; a correction was written into a private engine patch.
- **22 November 2026:** ported to v4.2.9. Five code paths new in v4.2.9 that ignored per-graph policy were made to follow it: class lookups under a graph override, the query-result cache key, reasoned queries, pushes that carry drops, and a dropped identity graph (the last is a `full-patch` feature).
- **23 November 2026:** confirmed on the stock v4.2.9 release through the CLI and through signed HTTP. Reads turned out to be affected too; the earlier description had called it a write-path gap. Committed as `9074b1a` on `main` and `94dc47a` on `full-patch`.
- **26 November 2026:** this record written.

## Related

A question to send with this bug, not a separate bug: on stock, a policy condition with no `GRAPH` clause runs in the written fact's graph on writes but in the default graph on reads ([`policy_condition_graph_context.py`](02-per-graph-policy-ignored/policy_condition_graph_context.py)). One policy for `f:view` and `f:modify`, condition `ASK { $identity <urn:x:mayUse> true }`, items in graphs X and Y:

| Where Rob's permission fact is stored | Stock: write X / write Y | Stock: read X / read Y | Full-patch build: read X / read Y |
|---|---|---|---|
| In graph X only | yes / no | **no / no** | yes / no |
| In graph X and the default graph | yes / no | **yes / yes** | yes / no |

The read side matches `policy-in-queries.md` ("per fact, whatever its graph"); the difference between the two paths is undocumented. The full-patch build decides reads in the fact's graph, but no graph override is configured here, so that comes from another of its changes, not from this fix; which one is not established, and a build of `main` was not run on this script.

## Reproduction

```bash
python3 -I known-issues/02-per-graph-policy-ignored/per_graph_policy_repro.py <fluree-binary> [output.json]
python3 known-issues/02-per-graph-policy-ignored/per_graph_policy_signed_http.py <fluree-binary> <port> [output.json]
python3 -I known-issues/02-per-graph-policy-ignored/policy_condition_graph_context.py <fluree-binary> [output.json]
```

- `per_graph_policy_repro.py` uses the CLI in `--direct` mode on fresh temporary ledgers, no server. Stock prints four `NOT AS DOCUMENTED` rows and `results that contradict the docs: 4`, and exits 1. The fix prints 0 and exits 0.
- `per_graph_policy_signed_http.py` needs the Python `cryptography` package (Ed25519 signing; Fluree's CLI cannot sign a transaction), so it runs without `-I`. It uses ports `<port>` and `<port>+1`. Stock prints `results that contradict the docs: 1` and the signer's fact in the sensitive graph, and exits 1; the fix prints 0 and exits 0. It saves statuses only, no token text.
- `policy_condition_graph_context.py` prints the table above and exits 1 when reads and writes decide differently (stock).

Recorded outputs, 23 November 2026: CLI on [stock](02-per-graph-policy-ignored/results-cli-stock-4.2.9.json) and the [full-patch build](02-per-graph-policy-ignored/results-cli-full-patch.json); signed HTTP on [stock](02-per-graph-policy-ignored/results-signed-http-stock-4.2.9.json) and [full-patch](02-per-graph-policy-ignored/results-signed-http-full-patch.json); the condition-graph question on [stock](02-per-graph-policy-ignored/results-condition-graph-stock-4.2.9.json) and [full-patch](02-per-graph-policy-ignored/results-condition-graph-full-patch.json). Temporary paths in them are shown as `<tmp>`.
