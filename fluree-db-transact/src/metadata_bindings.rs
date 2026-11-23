//! Trusted deployment configuration, never a transaction option.
use serde::{Deserialize, Deserializer, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Default, Serialize)]
#[serde(transparent)]
pub struct TransactionMetadataBindings(BTreeMap<String, String>);

impl TransactionMetadataBindings {
    pub fn new(entries: BTreeMap<String, String>) -> Result<Self, String> {
        if entries.len() > 32 {
            return Err("at most 32 transaction metadata bindings are allowed".into());
        }
        let mut predicates = BTreeSet::new();
        for (variable, predicate) in &entries {
            let suffix = variable.strip_prefix("txn").unwrap_or("");
            if !suffix
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_uppercase)
                || !suffix
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'_')
                || variable == "txnMetadataGraph"
                || variable.len() > 64
            {
                return Err(format!(
                    "invalid or intrinsic transaction variable: {variable}"
                ));
            }
            fluree_db_core::GraphIri::parse(predicate)
                .map_err(|_| format!("metadata predicate must be an absolute IRI: {predicate}"))?;
            if !predicates.insert(predicate) {
                return Err(format!("duplicate metadata predicate: {predicate}"));
            }
        }
        Ok(Self(entries))
    }

    pub fn iter(&self) -> impl Iterator<Item = (&String, &String)> {
        self.0.iter()
    }
}

/// Reserve the whole transaction-context namespace, including removed bindings.
pub fn reserved_transaction_variable(name: &str) -> bool {
    name.trim_start_matches(['?', '$'])
        .strip_prefix("txn")
        .and_then(|s| s.as_bytes().first())
        .is_some_and(u8::is_ascii_uppercase)
}

impl<'de> Deserialize<'de> for TransactionMetadataBindings {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Entries;
        impl<'de> serde::de::Visitor<'de> for Entries {
            type Value = TransactionMetadataBindings;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a unique map of transaction variables to absolute predicate IRIs")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> Result<Self::Value, A::Error> {
                let mut entries = BTreeMap::new();
                while let Some((key, value)) = map.next_entry::<String, String>()? {
                    if entries.insert(key.clone(), value).is_some() {
                        return Err(serde::de::Error::custom(format!(
                            "duplicate transaction variable: {key}"
                        )));
                    }
                    if entries.len() > 32 {
                        return Err(serde::de::Error::custom(
                            "too many transaction metadata bindings",
                        ));
                    }
                }
                TransactionMetadataBindings::new(entries).map_err(serde::de::Error::custom)
            }
        }
        deserializer.deserialize_map(Entries)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn metadata_binding_configuration_is_bounded_and_unambiguous() {
        for invalid in [
            r#"{"identity":"urn:example:actor"}"#,
            r#"{"txnMetadataGraph":"urn:example:actor"}"#,
            r#"{"?txnActor":"urn:example:actor"}"#,
            r#"{"txnActor":"relative"}"#,
            r#"{"txnActor":"urn:example:actor","txnActor":"urn:example:other"}"#,
            r#"{"txnActor":"urn:example:actor","txnOther":"urn:example:actor"}"#,
        ] {
            assert!(
                serde_json::from_str::<TransactionMetadataBindings>(invalid).is_err(),
                "{invalid}"
            );
        }
        let valid: TransactionMetadataBindings =
            serde_json::from_str(r#"{"txnActor":"urn:example:actor"}"#).unwrap();
        assert_eq!(valid.iter().count(), 1);
        for alias in ["txnRemoved", "?txnRemoved", "$txnRemoved", "?$txnRemoved"] {
            assert!(reserved_transaction_variable(alias));
        }
        assert!(!reserved_transaction_variable("?unrelated"));
    }
}
