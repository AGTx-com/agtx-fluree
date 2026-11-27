# Features

This fork's features, on the `full-patch` branch, beside the code they describe. Each is generic: it names no application, vocabulary or deployment, and the feature records stay on this branch only. Engine bugs, fixed or not, are recorded on `main`: [engine bug records](https://github.com/AGTx-com/agtx-fluree/blob/main/known-issues/README.md).

**How the branches relate.** `main` is the stock Fluree DB v4.2.9 release plus one commit per bug fix. This branch is meant to be `main` plus the features. Today it is still the tree from before `main` was rebuilt as fixes only (23 November 2026): it shares the fixes for bugs 3 to 7 with `main`, carries its own versions of the fixes for bugs 1, 2 and 8, and lacks the fixes for bugs 9 and 11. The engine release rebuilds it as `main` plus the features, so that every fix comes from `main` once.

**How features are turned on.** New features land behind a startup switch that is off by default, so a build with every switch off behaves as stock. The nine features built before that convention have no switch: each is turned on by ledger configuration, server configuration or a request option, or does nothing unless a policy or query uses it; two (2 and 7) are always on.

## Built, on this branch

| # | Feature | What it adds | Turned on by | Commit |
|---|---|---|---|---|
| 1 | Local `f:schemaSource` for policy and SHACL class checks | A ledger whose reasoning takes its ontology from a local graph gets subclass expansion from it in policy `f:onClass` / `f:onProperty` and in SHACL class targeting; stock takes extra schema there only from another ledger | ledger configuration: reasoning with a local `f:schemaSource` | `7b98230` |
| 2 | SHACL rechecks an edge when its annotation is detached | Detaching an annotation, or removing its last body property or its `rdf:type`, revalidates the surviving edge; `sh:targetSubjectsOf rdf:reifies` selects annotated edges | always, when SHACL is on; stricter than stock | `40da799` |
| 3 | `f:identitySource` | Binds `?$identity` from a named graph other than the policy source, so rules in one graph can recognise users recorded in another | ledger configuration | `49df74b` |
| 4 | Transaction-metadata bindings | Values a write declares in its transaction metadata become policy variables named `txn<Name>` | server configuration: `transaction_metadata_bindings` | `78e7395` |
| 5 | `?$predicate` | Policy queries can see the property of the fact being judged (`$predicate` in SPARQL) | does nothing unless a policy uses it | `5f50e0b` |
| 6 | `#txn-meta` inside policy queries | `GRAPH <ledger#txn-meta>` resolves to the transaction-metadata graph, inside policy queries only | does nothing unless a policy uses it | `fd929bd` |
| 7 | Binary store and dictionaries for policy queries | Policy queries scan the binary index as ordinary queries do, for speed; decisions are unchanged | always | `d899397` |
| 8 | Named graphs in history ranges | A history range (`FROM <ledger@t:a> TO <ledger@t:b>`) accepts `FROM NAMED <ledger#graph>` graphs of the same ledger | does nothing unless a query names them | `50bc7d0` |
| 9 | `opts.expectedT` and `opts.requireChange` | A write fails if the ledger's `t` differs from the expected one, or, with `requireChange`, if it would change nothing | request options | `06fe759` |

Two planner tests come with them: `ef555b0` (`GRAPH ?g` filters and binds stay outside `GRAPH`) and `f1d88ab` (`EXISTS` is not pushed into a `GRAPH` block).

## Built, on the engine-release branches

These join this branch when the engine release rebuilds it. They are on the local `policy-reuse/engine-on-bug11` branch today; their commit hashes change in the rebuild, so they are named here by switch.

| # | Feature | What it adds | Turned on by |
|---|---|---|---|
| 10 | Policy work counters | Policy tracking (`{"meta": {"policy": true}}`, or the `fluree-track-policy` header) reports a `policy_work` block: evaluations, lowerings, plans, `UNION` branches, `EXISTS` runs, semijoin builds and rows | when a request asks for policy tracking |
| 11 | Short-circuit `OR` in policy conditions | A policy condition's `\|\|` of `EXISTS` tests stops at the first operand that holds, on reads; decisions are unchanged | `FLUREE_POLICY_OR_SHORT_CIRCUIT`, off by default |
| 12 | Seeded semijoin | A pattern-level `EXISTS` in a policy condition starts from the one outer row instead of building the set of every key | `FLUREE_POLICY_SEEDED_SEMIJOIN`, off by default |
| 13 | Fact-free reuse | A view-policy branch that reads none of the fact's variables runs once for each set of seed values and is replayed for the rest of the read | `FLUREE_POLICY_FACT_FREE`, off by default |
| 14 | Lower once | A read lowers each policy condition's SPARQL once and reuses it | `FLUREE_POLICY_LOWER_ONCE`, off by default |

The write-side counterparts are measurement spikes on scratch branches, not features yet.

## Planned

| # | Feature | Fixes | Status | Record |
|---|---|---|---|---|
| 15 | Pinned JSON-LD contexts: map a context URL to a pinned local copy (never fetched), and refuse URL contexts that aren't pinned; accepting `@graph` container definitions is an optional later part | A URL `@context` is read as a vocabulary prefix and never loaded, so a W3C Verifiable Credential 2.0 is stored as wrong facts with no warning (a documented deviation, recorded on `main`) | Planned, not started | [15-pinned-json-ld-contexts.md](15-pinned-json-ld-contexts.md) |
