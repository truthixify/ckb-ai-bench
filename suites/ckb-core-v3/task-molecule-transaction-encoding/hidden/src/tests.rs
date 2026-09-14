use ckb_jsonrpc_types::Transaction;
use ckb_types::prelude::{Entity, IntoTransactionView};
use serde_json::Value;
use std::fs;

fn hex(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(2 + bytes.len() * 2);
    output.push_str("0x");
    for byte in bytes {
        use std::fmt::Write;
        write!(&mut output, "{byte:02x}").expect("hex output");
    }
    output
}

#[test]
fn candidate_serialization_matches_ckb_types() {
    let cases_path = std::env::var("CKBBENCH_PROJECT_CASES")
        .unwrap_or_else(|_| "/artifact/cases.json".to_owned());
    let document: Value = serde_json::from_str(
        &fs::read_to_string(cases_path).expect("candidate cases"),
    )
    .expect("valid candidate cases");
    let cases = document["cases"].as_array().expect("cases array");
    assert!(!cases.is_empty(), "at least one case is required");

    for case in cases {
        let mut transaction = case["input"]["raw_transaction"].clone();
        transaction["witnesses"] = Value::Array(Vec::new());
        let json: Transaction = serde_json::from_value(transaction).expect("valid RPC transaction");
        let packed: ckb_types::packed::Transaction = json.into();
        let view = packed.into_view();
        let result = &case["result"];
        assert_eq!(
            result["serialized"].as_str().expect("serialized bytes"),
            hex(view.data().raw().as_bytes().as_ref()),
            "candidate emitted non-canonical RawTransaction bytes"
        );
        assert_eq!(
            result["hash"].as_str().expect("transaction hash"),
            hex(view.hash().as_slice()),
            "candidate transaction hash differs from ckb-types"
        );
    }
}
