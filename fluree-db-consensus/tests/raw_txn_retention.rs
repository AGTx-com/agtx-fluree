//! A signed envelope must survive the local committer's option clones.
use fluree_db_api::{CommitOpts, FlureeBuilder, GovernanceOptions, TxnOpts};
use fluree_db_consensus::{
    Committer, IdempotencyKey, LocalCommitter, TransactionBody, TransactionRequest,
};
use fluree_db_core::{commit::codec::read_commit, ContentStore as _};
use fluree_db_ledger::IndexConfig;
use serde_json::json;
use std::sync::Arc;

#[derive(Debug)]
struct FailedUpload;

#[async_trait::async_trait]
impl fluree_db_core::ContentStore for FailedUpload {
    async fn has(&self, _: &fluree_db_core::ContentId) -> fluree_db_core::Result<bool> {
        unreachable!()
    }
    async fn get(&self, _: &fluree_db_core::ContentId) -> fluree_db_core::Result<Vec<u8>> {
        unreachable!()
    }
    async fn put(
        &self,
        _: fluree_db_core::ContentKind,
        _: &[u8],
    ) -> fluree_db_core::Result<fluree_db_core::ContentId> {
        Err(fluree_db_core::Error::storage(
            "injected original-envelope upload failure",
        ))
    }
    async fn put_with_id(
        &self,
        _: &fluree_db_core::ContentId,
        _: &[u8],
    ) -> fluree_db_core::Result<()> {
        unreachable!()
    }
    async fn release(&self, _: &fluree_db_core::ContentId) -> fluree_db_core::Result<()> {
        unreachable!()
    }
    fn permits_plaintext_cache(&self) -> bool {
        false
    }
    fn is_remote(&self) -> bool {
        false
    }
}

#[tokio::test]
async fn failed_envelope_upload_does_not_advance_the_ledger() {
    let ledger = "raw-failure:main";
    let fluree = Arc::new(FlureeBuilder::memory().build_memory());
    fluree.create_ledger(ledger).await.unwrap();
    let handle = fluree.ledger_cached(ledger).await.unwrap();
    let before = handle.t().await;
    let committer = LocalCommitter::new(
        fluree.clone(),
        IndexConfig {
            reindex_min_bytes: 100_000,
            reindex_max_bytes: 1_000_000_000,
        },
    );
    let result = committer
        .transact(TransactionRequest {
            idempotency_key: Some(IdempotencyKey::new("failed-upload").unwrap()),
            ledger_id: ledger.into(),
            body: TransactionBody::JsonLdInsert(json!({"@id": "urn:test:a", "urn:test:value": 1})),
            txn_opts: TxnOpts::default(),
            commit_opts: CommitOpts::default()
                .with_raw_txn_spawned(Arc::new(FailedUpload), json!("envelope")),
            tracking: None,
            governance: GovernanceOptions::default(),
        })
        .await;
    assert!(result
        .unwrap_err()
        .to_string()
        .contains("injected original-envelope upload failure"));
    assert_eq!(handle.t().await, before);
}

#[tokio::test]
async fn local_commit_retains_original_envelope_with_or_without_raw_body_option() {
    for store_raw in [false, true] {
        let ledger = "raw-envelope:main";
        let fluree = Arc::new(FlureeBuilder::memory().build_memory());
        fluree.create_ledger(ledger).await.unwrap();
        let store = fluree.content_store(ledger);
        // This layer receives an already verified envelope. HTTP signature verification is tested separately.
        let envelope = json!("exact.header-and-payload.signature");
        let opts = CommitOpts::default().with_raw_txn_spawned(store.clone(), envelope.clone());
        let committer = LocalCommitter::new(
            fluree.clone(),
            IndexConfig {
                reindex_min_bytes: 100_000,
                reindex_max_bytes: 1_000_000_000,
            },
        );
        let result = committer
            .transact(TransactionRequest {
                idempotency_key: Some(IdempotencyKey::new("original-key").unwrap()),
                ledger_id: ledger.into(),
                body: TransactionBody::JsonLdInsert(
                    json!({"@id": "urn:test:a", "urn:test:value": 1}),
                ),
                txn_opts: TxnOpts::default().store_raw_txn(store_raw),
                commit_opts: opts,
                tracking: None,
                governance: GovernanceOptions::default(),
            })
            .await
            .unwrap();
        let bytes = store.get(&result.commit.commit_id).await.unwrap();
        let commit = read_commit(&bytes).unwrap();
        let txn = commit
            .txn
            .expect("published commit must reference the original envelope");
        let stored: serde_json::Value =
            serde_json::from_slice(&store.get(&txn).await.unwrap()).unwrap();
        assert_eq!(
            stored, envelope,
            "parsed body must never replace the signed envelope"
        );
    }
}

#[tokio::test]
async fn cached_handle_retains_an_attached_envelope() {
    let ledger = "raw-handle:main";
    let fluree = FlureeBuilder::memory().build_memory();
    fluree.create_ledger(ledger).await.unwrap();
    let handle = fluree.ledger_cached(ledger).await.unwrap();
    let store = fluree.content_store(ledger);
    let envelope = json!("exact.original.envelope");
    let result = fluree
        .stage(&handle)
        .insert(&json!({"@id": "urn:test:a", "urn:test:value": 1}))
        .commit_opts(CommitOpts::default().with_raw_txn_spawned(store.clone(), envelope.clone()))
        .execute()
        .await
        .unwrap();
    let bytes = store.get(&result.receipt.commit_id).await.unwrap();
    let txn = read_commit(&bytes)
        .unwrap()
        .txn
        .expect("cached-handle clone must retain envelope");
    let stored: serde_json::Value =
        serde_json::from_slice(&store.get(&txn).await.unwrap()).unwrap();
    assert_eq!(stored, envelope);
}
