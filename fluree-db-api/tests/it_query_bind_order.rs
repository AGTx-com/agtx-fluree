//! A pattern that reads a variable an earlier-written BIND fills in is planned
//! after that BIND.
//!
//! The shape, found in a read policy:
//!
//! ```sparql
//! SELECT ?person WHERE {
//!   OPTIONAL { GRAPH <urn:g:keys> { ?key <urn:x:did> "did:example:reader" ;
//!                                        <urn:x:account> ?keyAccount } }
//!   BIND(COALESCE(?keyAccount, <urn:u:target>) AS ?reader)
//!   GRAPH <urn:g:shared> { ?reader a <urn:x:User> ; <urn:x:person> ?person }
//! }
//! ```
//!
//! The GRAPH block shares no variable with the OPTIONAL, and the planner places
//! every source ahead of an OPTIONAL, so it used to run first: it read every
//! user, and the BIND then kept the one whose IRI matched, as a clobber check.
//! Work grew with the number of users although one row comes back.
//!
//! Work is measured as fuel, which charges the index leaflets and in-memory
//! rows a query reads. The ledgers are indexed in 64-row leaflets, so a scan's
//! fuel grows with what it reads. Every case runs on an unindexed ledger
//! (novelty statistics) and an indexed one.

use fluree_db_api::{Fluree, FlureeBuilder, IndexerConfig, ReindexOptions};
use serde_json::Value;
use std::collections::BTreeMap;

async fn create(fluree: &Fluree, id: &str, data: &str, indexed: bool) {
    fluree.create_ledger(id).await.expect("create");
    update(fluree, id, &format!("INSERT DATA {{ {data} }}"))
        .await
        .expect("seed");
    if indexed {
        let opts = ReindexOptions::default()
            .with_indexer_config(IndexerConfig::default().with_leaflet_rows(64));
        fluree.reindex(id, opts).await.expect("reindex");
    }
}

async fn update(fluree: &Fluree, id: &str, sparql: &str) -> Result<(), String> {
    fluree
        .graph(id)
        .transact()
        .sparql_update(sparql)
        .commit()
        .await
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// The sorted rows of `sparql` on `id`, each `{var: value}` with its variables
/// in name order, and the fuel it used.
async fn select(fluree: &Fluree, id: &str, sparql: &str) -> (Vec<String>, f64) {
    let db = fluree.db(id).await.expect("view");
    let response = db
        .query(fluree)
        .sparql(sparql)
        .execute_tracked()
        .await
        .unwrap_or_else(|e| panic!("{sparql}: {e:?}"));
    let bindings = response.result["results"]["bindings"]
        .as_array()
        .unwrap_or_else(|| panic!("{sparql}: no bindings in {}", response.result));
    let mut rows: Vec<String> = bindings
        .iter()
        .map(|b| {
            let row: BTreeMap<&String, &Value> = b
                .as_object()
                .expect("binding")
                .iter()
                .map(|(var, term)| (var, &term["value"]))
                .collect();
            serde_json::to_string(&row).expect("row")
        })
        .collect();
    rows.sort();
    (rows, response.fuel.expect("fuel"))
}

/// One expected row, in [`select`]'s form.
fn row(pairs: &[(&str, &str)]) -> String {
    let row: BTreeMap<&str, &str> = pairs.iter().copied().collect();
    serde_json::to_string(&row).expect("row")
}

fn sorted(mut rows: Vec<String>) -> Vec<String> {
    rows.sort();
    rows
}

const KEY_LOOKUP: &str = "OPTIONAL { GRAPH <urn:g:keys> { \
     ?key <urn:x:did> \"did:example:reader\" ; <urn:x:account> ?keyAccount } }";

fn users(n: usize) -> String {
    let users: String = (0..n)
        .map(|i| format!("<urn:u:{i:06}> a <urn:x:User> ; <urn:x:person> <urn:p:{i:06}> . "))
        .collect();
    format!(
        "GRAPH <urn:g:shared> {{ {users} }} \
         GRAPH <urn:g:keys> {{ <urn:k:none> <urn:x:did> \"did:example:other\" . }}"
    )
}

/// The same lookup in JSON-LD, which lowers to the same patterns.
fn jsonld_lookup(target: &str) -> Value {
    serde_json::json!({
        "select": ["?person"],
        "where": [
            ["optional", ["graph", "urn:g:keys", {
                "@id": "?key",
                "urn:x:did": "did:example:reader",
                "urn:x:account": "?keyAccount"
            }]],
            ["bind", "?reader", format!("(coalesce ?keyAccount (iri \"urn:u:{target}\"))")],
            ["graph", "urn:g:shared", {
                "@id": "?reader",
                "@type": "urn:x:User",
                "urn:x:person": "?person"
            }]
        ]
    })
}

/// The JSON-LD answer's values, as strings, and the fuel it used.
async fn select_jsonld(fluree: &Fluree, id: &str, query: &Value) -> (Vec<String>, f64) {
    let db = fluree.db(id).await.expect("view");
    let response = db
        .query(fluree)
        .jsonld(query)
        .execute_tracked()
        .await
        .unwrap_or_else(|e| panic!("{query}: {e:?}"));
    let rows = response.result.as_array().expect("rows");
    let rows = rows.iter().map(Value::to_string).collect();
    (sorted(rows), response.fuel.expect("fuel"))
}

/// The known-issues query (`known-issues/11-lookup-before-bind/query_repro.py`)
/// at 1,000 and 10,000 users, with the target sorting last, in SPARQL and in
/// JSON-LD: the same rows and the same fuel at both sizes. Before the fix the
/// lookup read every user, so the fuel grew with the users.
#[tokio::test]
async fn a_lookup_after_the_bind_that_fills_in_its_key_does_the_same_work_at_any_size() {
    let fluree = FlureeBuilder::memory().build_memory();
    let mut failures = Vec::new();
    for indexed in [false, true] {
        let (mut sparql, mut jsonld) = (Vec::new(), Vec::new());
        for n in [1_000, 10_000] {
            let id = format!("it/bind-order-work-{n}-{indexed}:main");
            create(&fluree, &id, &users(n), indexed).await;
            let target = format!("{:06}", n - 1);
            let query = format!(
                "SELECT ?person WHERE {{ {KEY_LOOKUP} \
                 BIND(COALESCE(?keyAccount, <urn:u:{target}>) AS ?reader) \
                 GRAPH <urn:g:shared> {{ ?reader a <urn:x:User> ; <urn:x:person> ?person }} }}"
            );
            let (rows, used) = select(&fluree, &id, &query).await;
            assert_eq!(
                rows,
                [row(&[("person", &format!("urn:p:{target}"))])],
                "{n} users, indexed: {indexed}"
            );
            sparql.push(used);
            let (rows, used) = select_jsonld(&fluree, &id, &jsonld_lookup(&target)).await;
            assert_eq!(
                rows.len(),
                1,
                "JSON-LD, {n} users, indexed: {indexed}: {rows:?}"
            );
            assert!(
                rows[0].contains(&format!("urn:p:{target}")),
                "JSON-LD, {n} users, indexed: {indexed}: {rows:?}"
            );
            jsonld.push(used);
        }
        for (surface, fuel) in [("SPARQL", sparql), ("JSON-LD", jsonld)] {
            println!("{surface}, indexed: {indexed}: fuel {fuel:?} at 1,000 and 10,000 users");
            if (fuel[1] - fuel[0]).abs() >= 0.001 {
                failures.push(format!(
                    "{surface}, indexed: {indexed}: fuel {} at 1,000 users, {} at 10,000",
                    fuel[0], fuel[1]
                ));
            }
        }
    }
    assert!(failures.is_empty(), "{failures:#?}");
}

const FIXTURE: &str = "GRAPH <urn:g:shared> { \
       <urn:u:1> a <urn:x:User> ; <urn:x:person> <urn:p:1> . \
       <urn:u:2> a <urn:x:User> ; <urn:x:person> <urn:p:2> . \
       <urn:u:3> a <urn:x:User> ; <urn:x:person> <urn:p:3> . \
       <urn:u:4> a <urn:x:User> ; <urn:x:person> <urn:p:4> . \
       <urn:u:5> a <urn:x:User> ; <urn:x:person> <urn:p:5> . } \
     GRAPH <urn:g:keys> { \
       <urn:k:one> <urn:x:did> \"did:example:one\" ; <urn:x:account> <urn:u:3> . \
       <urn:k:num> <urn:x:did> \"did:example:num\" ; <urn:x:code> 42 . } \
     <urn:u:1> <urn:x:role> \"role-1\" . <urn:u:2> <urn:x:role> \"role-2\" . \
     <urn:u:3> <urn:x:role> \"role-3\" . <urn:u:4> <urn:x:role> \"role-4\" . \
     <urn:u:5> <urn:x:role> \"role-5\" . \
     <urn:u:2> <urn:x:blocked> true . \
     <urn:u:1> <urn:x:tag> \"t1\", \"t2\", \"t3\", \"t4\", \"t5\" . \
     <urn:d:1> <urn:x:owner> <urn:u:3> . \
     <urn:d:2> <urn:x:owner> <urn:u:1> . \
     <urn:k:n> <urn:x:num> 1 . \
     <urn:s:int> <urn:x:val> 1 . \
     <urn:s:long> <urn:x:val> \"1\"^^<http://www.w3.org/2001/XMLSchema#long> .";

/// `OPTIONAL` on the key whose DID is `did`.
fn lookup(did: &str, prop: &str, var: &str) -> String {
    format!(
        "OPTIONAL {{ GRAPH <urn:g:keys> {{ ?key <urn:x:did> \"{did}\" ; <urn:x:{prop}> ?{var} }} }}"
    )
}

const READER_BLOCK: &str =
    "GRAPH <urn:g:shared> { ?reader a <urn:x:User> ; <urn:x:person> ?person }";

/// The same lookup in the default graph, which the rule holds even when the
/// BIND may leave `?reader` unbound.
const READER_ROLE: &str = "?reader <urn:x:role> ?role";

/// Every user's (reader, role) row.
fn every_role() -> Vec<String> {
    sorted(
        (1..=5)
            .map(|i| {
                row(&[
                    ("reader", &format!("urn:u:{i}")),
                    ("role", &format!("role-{i}")),
                ])
            })
            .collect(),
    )
}

/// Every user's (reader, person) row: what a lookup with `?reader` unbound
/// returns.
fn every_user() -> Vec<String> {
    sorted(
        (1..=5)
            .map(|i| {
                row(&[
                    ("person", &format!("urn:p:{i}")),
                    ("reader", &format!("urn:u:{i}")),
                ])
            })
            .collect(),
    )
}

/// Each query returns the rows SPARQL gives it, which the plan before the fix
/// also gave: the OPTIONAL present and absent, a COALESCE that yields
/// unbound, a BIND whose expression errors (non-strict), a variable another
/// pattern also binds, nested groups, and a numeric object the BIND may yield.
///
/// Where the BIND leaves `?reader` unbound, the default-graph lookups are held
/// and scan free. The GRAPH lookups keep their place: a GRAPH block run after
/// a row that leaves one of its variables unbound drops its binding of it, so
/// held there they would lose `?reader`.
#[tokio::test]
async fn a_lookup_planned_after_the_bind_returns_the_same_rows() {
    let one = lookup("did:example:one", "account", "keyAccount");
    let none = lookup("did:example:none", "account", "keyAccount");
    let num = lookup("did:example:num", "code", "code");
    let cases: Vec<(&str, String, Vec<String>)> = vec![
        (
            "OPTIONAL present",
            format!(
                "SELECT ?person WHERE {{ {one} \
                 BIND(COALESCE(?keyAccount, <urn:u:5>) AS ?reader) {READER_BLOCK} }}"
            ),
            vec![row(&[("person", "urn:p:3")])],
        ),
        (
            "OPTIONAL absent",
            format!(
                "SELECT ?person WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount, <urn:u:5>) AS ?reader) {READER_BLOCK} }}"
            ),
            vec![row(&[("person", "urn:p:5")])],
        ),
        (
            "COALESCE yields unbound",
            format!(
                "SELECT ?reader ?person WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) {READER_BLOCK} }}"
            ),
            every_user(),
        ),
        (
            "BIND expression errors",
            format!(
                "SELECT ?reader ?person WHERE {{ {num} \
                 BIND(IRI(?code + \"x\") AS ?reader) {READER_BLOCK} }}"
            ),
            every_user(),
        ),
        (
            "COALESCE yields unbound, default-graph lookup",
            format!(
                "SELECT ?reader ?role WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) {READER_ROLE} }}"
            ),
            every_role(),
        ),
        (
            "BIND expression errors, default-graph lookup",
            format!(
                "SELECT ?reader ?role WHERE {{ {num} \
                 BIND(IRI(?code + \"x\") AS ?reader) {READER_ROLE} }}"
            ),
            every_role(),
        ),
        (
            "COALESCE yields unbound, UNION of default-graph lookups",
            format!(
                "SELECT ?reader ?role WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) \
                 {{ {READER_ROLE} }} UNION {{ ?reader <urn:x:missing> ?role }} }}"
            ),
            every_role(),
        ),
        (
            "variable another pattern binds, BIND may yield a literal",
            format!(
                "SELECT ?doc ?person WHERE {{ {one} \
                 BIND(COALESCE(?keyAccount, <urn:u:5>) AS ?reader) \
                 ?doc <urn:x:owner> ?reader . {READER_BLOCK} }}"
            ),
            vec![row(&[("doc", "urn:d:1"), ("person", "urn:p:3")])],
        ),
        (
            "variable another pattern binds, BIND yields an IRI",
            format!(
                "SELECT ?doc ?person WHERE {{ {none} \
                 BIND(IRI(COALESCE(STR(?keyAccount), \"urn:u:1\")) AS ?reader) \
                 ?doc <urn:x:owner> ?reader . {READER_BLOCK} }}"
            ),
            vec![row(&[("doc", "urn:d:2"), ("person", "urn:p:1")])],
        ),
        (
            "nested group",
            format!(
                "SELECT ?person WHERE {{ {{ {one} \
                 BIND(COALESCE(?keyAccount, <urn:u:5>) AS ?reader) {READER_BLOCK} }} }}"
            ),
            vec![row(&[("person", "urn:p:3")])],
        ),
        (
            "inside EXISTS",
            format!(
                "SELECT ?doc WHERE {{ ?doc <urn:x:owner> ?owner \
                 FILTER EXISTS {{ {one} BIND(COALESCE(?keyAccount, <urn:u:5>) AS ?reader) \
                 {READER_BLOCK} FILTER(?reader = ?owner) }} }}"
            ),
            vec![row(&[("doc", "urn:d:1")])],
        ),
        (
            "inside OPTIONAL",
            format!(
                "SELECT ?doc ?person WHERE {{ ?doc <urn:x:owner> ?owner \
                 OPTIONAL {{ {one} BIND(COALESCE(?keyAccount, <urn:u:5>) AS ?reader) \
                 {READER_BLOCK} FILTER(?reader = ?owner) }} }}"
            ),
            sorted(vec![
                row(&[("doc", "urn:d:1"), ("person", "urn:p:3")]),
                row(&[("doc", "urn:d:2")]),
            ]),
        ),
        (
            // Probed with the integer 1, the scan would also match
            // "1"^^xsd:long; the clobber check keeps only the same term. A
            // BIND that may yield a literal leaves an object lookup where it
            // was.
            "numeric object",
            "SELECT ?s WHERE { OPTIONAL { <urn:k:n> <urn:x:num> ?v } \
             BIND(?v AS ?x) ?s <urn:x:val> ?x }"
                .to_string(),
            vec![row(&[("s", "urn:s:int")])],
        ),
        // Step 1 review, C1: a BIND that may leave ?reader unbound, and
        // another pattern that reads ?reader. Today the lookup binds ?reader
        // first and the reader sees the lookup's value.
        (
            "C1: FILTER != after a lookup whose BIND may be unbound",
            format!(
                "SELECT ?reader ?role WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) {READER_ROLE} \
                 FILTER(?reader != <urn:u:1>) }}"
            ),
            every_role()[1..].to_vec(),
        ),
        (
            "C1: FILTER(BOUND) after a lookup whose BIND may be unbound",
            format!(
                "SELECT ?reader ?role WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) {READER_ROLE} \
                 FILTER(BOUND(?reader)) }}"
            ),
            every_role(),
        ),
        (
            "C1: a later BIND reading the variable",
            format!(
                "SELECT ?reader ?name WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) {READER_ROLE} \
                 BIND(STR(?reader) AS ?name) }}"
            ),
            sorted(
                (1..=5)
                    .map(|i| {
                        let user = format!("urn:u:{i}");
                        row(&[("name", &user), ("reader", &user)])
                    })
                    .collect(),
            ),
        ),
        (
            "C1: FILTER != after a lookup whose BIND errors",
            format!(
                "SELECT ?reader ?role WHERE {{ {num} \
                 BIND(IRI(?code + \"x\") AS ?reader) {READER_ROLE} \
                 FILTER(?reader != <urn:u:1>) }}"
            ),
            every_role()[1..].to_vec(),
        ),
        (
            "C1: FILTER != after a UNION lookup",
            format!(
                "SELECT ?reader ?role WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) \
                 {{ {READER_ROLE} }} UNION {{ ?reader <urn:x:missing> ?role }} \
                 FILTER(?reader != <urn:u:1>) }}"
            ),
            every_role()[1..].to_vec(),
        ),
        (
            "C1: inside EXISTS",
            format!(
                "SELECT ?doc WHERE {{ ?doc <urn:x:owner> ?owner \
                 FILTER EXISTS {{ {none} BIND(COALESCE(?keyAccount) AS ?reader) \
                 {READER_ROLE} FILTER(?reader = ?owner) }} }}"
            ),
            sorted(vec![row(&[("doc", "urn:d:1")]), row(&[("doc", "urn:d:2")])]),
        ),
        (
            "C1: NOT EXISTS written between the BIND and the lookup",
            format!(
                "SELECT ?reader ?role WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) \
                 FILTER NOT EXISTS {{ ?reader <urn:x:blocked> true }} {READER_ROLE} }}"
            ),
            every_role()
                .into_iter()
                .filter(|r| !r.contains("urn:u:2"))
                .collect(),
        ),
        // Review M2: a MINUS written between the BIND and the lookup. Today the
        // MINUS sees the lookup's ?reader and removes u:3. (SPARQL's algebra
        // would keep it, with ?reader unbound where the MINUS runs; the fix
        // does not change what today's plan returns.)
        (
            "MINUS between, BIND may be unbound",
            format!(
                "SELECT ?reader ?role WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount) AS ?reader) \
                 MINUS {{ ?reader <urn:x:role> \"role-3\" }} {READER_ROLE} }}"
            ),
            every_role()
                .into_iter()
                .filter(|r| !r.contains("urn:u:3"))
                .collect(),
        ),
        (
            "MINUS between reading the lookup's variable, BIND always binds",
            format!(
                "SELECT ?reader ?role WHERE {{ {none} \
                 BIND(COALESCE(?keyAccount, <urn:u:3>) AS ?reader) \
                 MINUS {{ <urn:u:3> <urn:x:role> ?role }} {READER_ROLE} }}"
            ),
            vec![],
        ),
    ];

    let fluree = FlureeBuilder::memory().build_memory();
    let mut failures = Vec::new();
    for indexed in [false, true] {
        let id = format!("it/bind-order-rows-{indexed}:main");
        create(&fluree, &id, FIXTURE, indexed).await;
        for (name, query, expected) in &cases {
            let (rows, _) = select(&fluree, &id, query).await;
            if &rows != expected {
                failures.push(format!(
                    "{name} (indexed: {indexed}): got {rows:?}, want {expected:?}"
                ));
            }
        }
    }
    assert!(failures.is_empty(), "{failures:#?}");
}

/// `LIMIT` without `ORDER BY`: SPARQL fixes only how many rows come back and
/// that each is a solution. Planned after the BIND, the lookup can return
/// different ones than before, so that is all this asserts.
#[tokio::test]
async fn a_limit_without_order_returns_that_many_solutions() {
    let none = lookup("did:example:none", "account", "keyAccount");
    let all = format!(
        "SELECT ?tag WHERE {{ {none} \
         BIND(COALESCE(?keyAccount, <urn:u:1>) AS ?reader) ?reader <urn:x:tag> ?tag }}"
    );
    let fluree = FlureeBuilder::memory().build_memory();
    for indexed in [false, true] {
        let id = format!("it/bind-order-limit-{indexed}:main");
        create(&fluree, &id, FIXTURE, indexed).await;
        let (every, _) = select(&fluree, &id, &all).await;
        assert_eq!(
            every,
            sorted((1..=5).map(|i| row(&[("tag", &format!("t{i}"))])).collect())
        );
        let (some, _) = select(&fluree, &id, &format!("{all} LIMIT 2")).await;
        assert_eq!(some.len(), 2, "{some:?}");
        assert!(some.iter().all(|r| every.contains(r)), "{some:?}");
        assert_ne!(some[0], some[1]);
    }
}

/// Strict BIND errors (an update's WHERE) are as they are today: such a
/// group is planned as before the hold. A BIND that may fail (here
/// `IRI(?code + "x")`) fails the update when the lookup finds a row and
/// writes nothing when it does not. A COALESCE skips its arguments' errors.
///
/// Re-review N2, both directions. A BIND written after the lookup that reads
/// the reader, `IF(?reader = <x>, ?reader + 1, 0)`, fails on the row where
/// `?reader` is `<x>`. Today the lookup runs first with `?reader` free, so
/// the BIND meets every user: with `<x>` = `<urn:u:2>` the update fails,
/// although the fallback reader is `<urn:u:1>`. With `<x>` the fallback
/// `<urn:x:nobody>`, which has no role, the BIND never meets it and the
/// update succeeds, writing nothing. Held, both would go the other way.
#[tokio::test]
async fn strict_updates_succeed_and_fail_as_today() {
    let fluree = FlureeBuilder::memory().build_memory();
    let id = "it/bind-order-strict:main";
    create(&fluree, id, FIXTURE, false).await;
    let num = lookup("did:example:num", "code", "code");
    let insert = |bind: &str, consumer: &str| {
        format!(
            "INSERT {{ GRAPH <urn:g:log> {{ <urn:x:log> <urn:x:saw> ?reader }} }} WHERE {{ {num} \
             BIND({bind} AS ?reader) {consumer} }}"
        )
    };
    let fails = "IRI(?code + \"x\")";
    let nobody = format!("{READER_ROLE} FILTER(?role = \"nobody\")");
    let err = update(&fluree, id, &insert(fails, READER_ROLE))
        .await
        .expect_err("the lookup finds rows, so the BIND runs and fails");
    assert!(err.contains(STRICT_BIND_ERROR), "{err}");
    update(&fluree, id, &insert(fails, &nobody))
        .await
        .expect("no row reaches the BIND");
    update(
        &fluree,
        id,
        &insert("COALESCE(IRI(?code + \"x\"), <urn:u:1>)", READER_ROLE),
    )
    .await
    .expect("a COALESCE skips its argument's error");
    let (saw, _) = select(
        &fluree,
        id,
        "SELECT ?reader WHERE { GRAPH <urn:g:log> { <urn:x:log> <urn:x:saw> ?reader } }",
    )
    .await;
    assert_eq!(saw, [row(&[("reader", "urn:u:1")])]);

    let none = lookup("did:example:none", "account", "keyAccount");
    let reads_reader = |fallback: &str, failing: &str| {
        format!(
            "INSERT {{ GRAPH <urn:g:log> {{ <urn:x:log> <urn:x:n> ?n }} }} WHERE {{ {none} \
             BIND(COALESCE(?keyAccount, <{fallback}>) AS ?reader) {READER_ROLE} \
             BIND(IF(?reader = <{failing}>, ?reader + 1, 0) AS ?n) }}"
        )
    };
    let err = update(&fluree, id, &reads_reader("urn:u:1", "urn:u:2"))
        .await
        .expect_err("today the BIND meets urn:u:2's row and fails");
    assert!(err.contains(STRICT_BIND_ERROR), "{err}");
    update(&fluree, id, &reads_reader("urn:x:nobody", "urn:x:nobody"))
        .await
        .expect("today the BIND never meets the fallback, which has no role");
    let (logged, _) = select(
        &fluree,
        id,
        "SELECT ?n WHERE { GRAPH <urn:g:log> { <urn:x:log> <urn:x:n> ?n } }",
    )
    .await;
    assert!(logged.is_empty(), "nothing written: {logged:?}");
}

/// Part of the strict BIND error's message.
const STRICT_BIND_ERROR: &str = "cannot perform arithmetic on these types";

/// Step 1 review, I1: a selective lookup that anchors another source. Held,
/// `?o <urn:x:label> ?l` would run first and scan every label; today it
/// probes the lookup's objects. The work stays flat in the number of labels.
#[tokio::test]
async fn a_lookup_that_anchors_another_source_does_the_same_work_at_any_size() {
    let fluree = FlureeBuilder::memory().build_memory();
    let query = "SELECT ?l WHERE { \
        OPTIONAL { ?key <urn:x:did> \"did:example:reader\" ; <urn:x:account> ?keyAccount } \
        BIND(COALESCE(?keyAccount, <urn:u:1>) AS ?r) \
        ?r <urn:x:rare> ?o . ?o <urn:x:label> ?l }";
    let mut failures = Vec::new();
    for indexed in [false, true] {
        let mut fuel = Vec::new();
        for n in [1_000, 10_000] {
            let id = format!("it/bind-order-anchor-{n}-{indexed}:main");
            let labels: String = (1..=n)
                .map(|i| format!("<urn:o:{i}> <urn:x:label> \"label-{i}\" . "))
                .collect();
            let data = format!(
                "{labels} <urn:u:1> <urn:x:rare> <urn:o:1> . \
                 <urn:u:2> <urn:x:rare> <urn:o:2> . <urn:u:3> <urn:x:rare> <urn:o:3> ."
            );
            create(&fluree, &id, &data, indexed).await;
            let (rows, used) = select(&fluree, &id, query).await;
            assert_eq!(rows, [row(&[("l", "label-1")])], "{n}, indexed: {indexed}");
            fuel.push(used);
        }
        println!("anchor, indexed: {indexed}: fuel {fuel:?} at 1,000 and 10,000 labels");
        if (fuel[1] - fuel[0]).abs() >= 0.001 {
            failures.push(format!(
                "indexed: {indexed}: fuel {} at 1,000 labels, {} at 10,000",
                fuel[0], fuel[1]
            ));
        }
    }
    assert!(failures.is_empty(), "{failures:#?}");
}

/// Cost guard. Holding a lookup back is slower when the BIND's input is large
/// and the lookup finds nothing: placed first, the lookup returns no row and
/// the input is never read. Statistics that price the lookup at one row or
/// none keep it first, so the work stays flat in the size of the input. A
/// GRAPH block is priced from its body here (review M1), and a UNION by its
/// costliest branch (re-review N1). Each BIND binds on every row, so only the
/// guard keeps the lookup first.
#[tokio::test]
async fn a_lookup_statistics_price_as_empty_stays_ahead_of_a_large_bind_input() {
    let fluree = FlureeBuilder::memory().build_memory();
    let cases = [
        (
            "triple",
            "SELECT ?o WHERE { ?item <urn:x:code> ?id \
             BIND(COALESCE(IRI(CONCAT(\"urn:u:\", ?id)), <urn:u:0>) AS ?x) ?x <urn:x:rare> ?o }",
        ),
        (
            "UNION",
            "SELECT ?o WHERE { ?item <urn:x:code> ?id \
             BIND(COALESCE(IRI(CONCAT(\"urn:u:\", ?id)), <urn:u:0>) AS ?x) \
             { ?x <urn:x:rare> ?o } UNION { ?x <urn:x:rarer> ?o } }",
        ),
        (
            "GRAPH block",
            "SELECT ?o WHERE { OPTIONAL { ?item <urn:x:code> ?id } \
             BIND(COALESCE(IRI(CONCAT(\"urn:u:\", ?id)), <urn:u:1>) AS ?x) \
             GRAPH <urn:g:g> { ?x <urn:x:rare> ?o } }",
        ),
    ];
    let mut failures = Vec::new();
    for indexed in [false, true] {
        let mut fuel: Vec<Vec<f64>> = vec![Vec::new(); cases.len()];
        for n in [1_000, 10_000] {
            let id = format!("it/bind-order-cost-{n}-{indexed}:main");
            let items: String = (0..n)
                .map(|i| format!("<urn:i:{i:06}> <urn:x:code> \"{i:06}\" . "))
                .collect();
            create(
                &fluree,
                &id,
                &format!("{items} <urn:x:a> <urn:x:b> <urn:x:c> . GRAPH <urn:g:g> {{ <urn:x:a> <urn:x:b> <urn:x:c> . }}"),
                indexed,
            )
            .await;
            for (k, (name, query)) in cases.iter().enumerate() {
                let (rows, used) = select(&fluree, &id, query).await;
                assert!(rows.is_empty(), "{name}: {rows:?}");
                fuel[k].push(used);
            }
        }
        for (k, (name, _)) in cases.iter().enumerate() {
            if (fuel[k][1] - fuel[k][0]).abs() >= 0.001 {
                failures.push(format!(
                    "{name}, indexed: {indexed}: fuel {} at 1,000 items, {} at 10,000",
                    fuel[k][0], fuel[k][1]
                ));
            }
        }
    }
    assert!(failures.is_empty(), "{failures:#?}");
}

/// The re-review's I1b query, and the same join through an IRI built from
/// the lookup's value. A source joined to the lookup through a BIND that
/// reads the lookup's `?o` anchors it: held, `?doc <urn:x:label> ?name`
/// would run first and scan every label, so the work would grow with the
/// documents. Planned as before the hold, the work does not grow: at 1,000
/// documents the planner reads the labels (fuel 2.003 unindexed, 2.233
/// indexed), at 10,000 it probes them (1.006 and 1.106), and the IRI form
/// probes at both sizes.
const THROUGH_A_BIND: [(&str, &str, &str); 2] = [
    (
        "BIND(STR(?o))",
        "SELECT ?doc WHERE { \
         OPTIONAL { ?key <urn:x:did> \"did:example:reader\" ; <urn:x:account> ?keyAccount } \
         BIND(COALESCE(?keyAccount, <urn:u:1>) AS ?r) \
         ?r <urn:x:rare> ?o . BIND(STR(?o) AS ?name) ?doc <urn:x:label> ?name . }",
        "doc",
    ),
    (
        "BIND(IRI(…))",
        "SELECT ?t WHERE { \
         OPTIONAL { ?key <urn:x:did> \"did:example:reader\" ; <urn:x:account> ?keyAccount } \
         BIND(COALESCE(?keyAccount, <urn:u:1>) AS ?r) \
         ?r <urn:x:rare> ?o . \
         BIND(IRI(CONCAT(\"urn:doc:\", STRAFTER(STR(?o), \"urn:o:\"))) AS ?d) \
         ?d <urn:x:title> ?t . }",
        "t",
    ),
];

/// Three `rare` triples (`u:1` to `o:1`, `u:2` to `o:2`, `u:3` to `o:3`) and
/// `n` documents, each with a label and a title.
fn labelled_docs(n: usize) -> String {
    let docs: String = (1..=n)
        .map(|i| {
            format!("<urn:doc:{i}> <urn:x:label> \"urn:o:{i}\" ; <urn:x:title> \"title-{i}\" . ")
        })
        .collect();
    format!(
        "{docs} <urn:u:1> <urn:x:rare> <urn:o:1> . \
         <urn:u:2> <urn:x:rare> <urn:o:2> . <urn:u:3> <urn:x:rare> <urn:o:3> ."
    )
}

#[tokio::test]
async fn a_lookup_that_anchors_a_source_through_a_bind_does_no_more_work_at_any_size() {
    let fluree = FlureeBuilder::memory().build_memory();
    let mut failures = Vec::new();
    for indexed in [false, true] {
        let mut fuel: Vec<Vec<f64>> = vec![Vec::new(); THROUGH_A_BIND.len()];
        for n in [1_000, 10_000] {
            let id = format!("it/bind-order-through-{n}-{indexed}:main");
            create(&fluree, &id, &labelled_docs(n), indexed).await;
            for (k, (name, query, var)) in THROUGH_A_BIND.iter().enumerate() {
                let (rows, used) = select(&fluree, &id, query).await;
                let expected = if *var == "doc" {
                    "urn:doc:1"
                } else {
                    "title-1"
                };
                assert_eq!(
                    rows,
                    [row(&[(var, expected)])],
                    "{name}, {n}, indexed: {indexed}"
                );
                fuel[k].push(used);
            }
        }
        for (k, (name, _, _)) in THROUGH_A_BIND.iter().enumerate() {
            println!(
                "{name}, indexed: {indexed}: fuel {:?} at 1,000 and 10,000",
                fuel[k]
            );
            if fuel[k][1] - fuel[k][0] >= 0.001 {
                failures.push(format!(
                    "{name}, indexed: {indexed}: fuel {} at 1,000 documents, {} at 10,000",
                    fuel[k][0], fuel[k][1]
                ));
            }
        }
    }
    assert!(failures.is_empty(), "{failures:#?}");
}

/// The re-review's I1b answer case. `STRLEN(STR(<urn:o:1>))` is the integer
/// 7. Fifty documents have integer sizes 1 to 50, more rows than the three
/// `rare` triples, and `<urn:d:x>` has the size `"7"^^xsd:long`. Today the
/// lookup runs first, the BIND after it, and `?doc <urn:x:size> ?len` probes
/// with 7; a probe matches numbers across datatypes, so both `<urn:doc:7>`
/// and `<urn:d:x>` come back. Held, the size scan would run first and the
/// BIND would compare terms, dropping `<urn:d:x>`.
#[tokio::test]
async fn a_lookup_joined_through_a_numeric_bind_returns_todays_rows() {
    let fluree = FlureeBuilder::memory().build_memory();
    let query = "SELECT ?doc WHERE { \
        OPTIONAL { ?key <urn:x:did> \"did:example:reader\" ; <urn:x:account> ?keyAccount } \
        BIND(COALESCE(?keyAccount, <urn:u:1>) AS ?r) \
        ?r <urn:x:rare> ?o . BIND(STRLEN(STR(?o)) AS ?len) ?doc <urn:x:size> ?len . }";
    for indexed in [false, true] {
        let id = format!("it/bind-order-numeric-through-{indexed}:main");
        let sizes: String = (1..=50)
            .map(|i| format!("<urn:doc:{i}> <urn:x:size> {i} . "))
            .collect();
        let data = format!(
            "{sizes} <urn:d:x> <urn:x:size> \"7\"^^<http://www.w3.org/2001/XMLSchema#long> . \
             <urn:u:1> <urn:x:rare> <urn:o:1> . \
             <urn:u:2> <urn:x:rare> <urn:o:2> . <urn:u:3> <urn:x:rare> <urn:o:3> ."
        );
        create(&fluree, &id, &data, indexed).await;
        let (rows, _) = select(&fluree, &id, query).await;
        assert_eq!(
            rows,
            sorted(vec![
                row(&[("doc", "urn:d:x")]),
                row(&[("doc", "urn:doc:7")])
            ]),
            "indexed: {indexed}"
        );
    }
}
