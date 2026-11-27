//! Reproduction for engine bug 16: what a signed write made through the Rust
//! API's `credential_transact` records as provenance, before and after an index
//! build.
//!
//! Run: cargo test -p fluree-db-api --features credential --test bug16_author_after_index -- --nocapture
#![cfg(feature = "credential")]

mod support;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use ed25519_dalek::{Signer, SigningKey};
use fluree_db_api::{credential, FlureeBuilder};
use serde_json::{json, Value as JsonValue};
use support::{genesis_ledger, rebuild_and_publish_index, MemoryFluree};

const LEDGER: &str = "bug16/author:main";

fn create_jws(payload: &str, signing_key: &SigningKey) -> String {
    let pubkey = signing_key.verifying_key().to_bytes();
    let header = json!({
        "alg": "EdDSA",
        "jwk": { "kty": "OKP", "crv": "Ed25519", "x": URL_SAFE_NO_PAD.encode(pubkey) }
    });
    let signing_input = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(header.to_string().as_bytes()),
        URL_SAFE_NO_PAD.encode(payload.as_bytes())
    );
    let sig = signing_key.sign(signing_input.as_bytes());
    format!("{signing_input}.{}", URL_SAFE_NO_PAD.encode(sig.to_bytes()))
}

/// Every `f:identity` and `f:author` in the ledger's txn-meta graph, by `t`.
async fn provenance(fluree: &MemoryFluree) -> Vec<String> {
    let query = json!({
        "@context": { "f": "https://ns.flur.ee/db#" },
        "from": format!("{LEDGER}#txn-meta"),
        "select": ["?t", "?p", "?o"],
        "where": { "@id": "?c", "f:t": "?t", "?p": "?o" }
    });
    let results = fluree.query_connection(&query).await.expect("query txn-meta");
    let ledger = fluree.ledger(LEDGER).await.expect("load ledger");
    let rows = results.to_jsonld(&ledger.snapshot).expect("to_jsonld");
    let mut out: Vec<String> = rows
        .as_array()
        .expect("rows")
        .iter()
        .filter_map(|row| {
            let row = row.as_array()?;
            let p = row.get(1)?.as_str()?;
            (p.ends_with("identity") || p.ends_with("author"))
                .then(|| format!("t={} {} {}", row[0], p, row[2]))
        })
        .collect();
    out.sort();
    out
}

#[tokio::test]
async fn bug16_signed_write_provenance_before_and_after_index() {
    let fluree = FlureeBuilder::memory().build_memory();
    let signing_key = SigningKey::from_bytes(&[7u8; 32]);
    let did = fluree_db_credential::did_from_pubkey(&signing_key.verifying_key().to_bytes());
    println!("signer: {did}");

    // A signed identity with no policy is denied, so the signer gets an allow-all policy first.
    let setup = json!({
        "@context": { "f": "https://ns.flur.ee/db#", "ex": "http://example.org/" },
        "@graph": [
            { "@id": did, "f:policyClass": [{ "@id": "ex:AllowAll" }] },
            { "@id": "ex:allowAll", "@type": ["f:AccessPolicy", "ex:AllowAll"],
              "f:action": [{ "@id": "f:view" }, { "@id": "f:modify" }], "f:allow": true }
        ]
    });
    let mut ledger = fluree
        .insert(genesis_ledger(&fluree, LEDGER), &setup)
        .await
        .expect("setup")
        .ledger;

    // t=2: a signed update with no author claim.
    let plain: JsonValue = json!({
        "@context": { "ex": "http://example.org/" },
        "insert": { "@id": "ex:alice", "ex:name": "Alice" }
    });
    ledger = fluree
        .credential_transact(ledger, credential::Input::Jws(&create_jws(&plain.to_string(), &signing_key)))
        .await
        .expect("signed update without a claim")
        .ledger;

    // t=3: a signed update that also carries a user author claim.
    let claimed: JsonValue = json!({
        "@context": { "ex": "http://example.org/", "f": "https://ns.flur.ee/db#" },
        "insert": { "@id": "ex:bob", "ex:name": "Bob" },
        "f:author": "alice-the-claimant"
    });
    match fluree
        .credential_transact(ledger.clone(), credential::Input::Jws(&create_jws(&claimed.to_string(), &signing_key)))
        .await
    {
        Ok(result) => {
            ledger = result.ledger;
            println!("signed update with an f:author claim: committed at t={}", ledger.t());
        }
        Err(e) => println!("signed update with an f:author claim: refused: {e}"),
    }
    drop(ledger);

    println!("before index build:");
    for line in provenance(&fluree).await {
        println!("    {line}");
    }
    rebuild_and_publish_index(&fluree, LEDGER).await;
    fluree.disconnect_ledger(LEDGER).await;
    println!("after index build:");
    for line in provenance(&fluree).await {
        println!("    {line}");
    }
}
