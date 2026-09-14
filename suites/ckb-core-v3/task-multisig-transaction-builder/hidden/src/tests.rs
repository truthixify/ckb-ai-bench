use ckb_system_scripts::BUNDLED_CELL;
use ckb_testtool::builtin::ALWAYS_SUCCESS;
use ckb_testtool::ckb_jsonrpc_types::{CellOutput as JsonCellOutput, OutPoint as JsonOutPoint};
use ckb_testtool::ckb_types::{
    bytes::Bytes,
    core::{Capacity, TransactionView},
    packed::{self, WitnessArgs},
    prelude::*,
};
use ckb_testtool::context::Context;
use serde_json::Value;
use std::fs;

const MAX_CYCLES: u64 = 20_000_000;

fn decode_hex(value: &Value) -> Vec<u8> {
    let value = value.as_str().expect("hex string");
    let digits = value.strip_prefix("0x").expect("0x prefix");
    assert!(digits.len().is_multiple_of(2), "even hex length");
    (0..digits.len())
        .step_by(2)
        .map(|offset| u8::from_str_radix(&digits[offset..offset + 2], 16).expect("hex byte"))
        .collect()
}

fn encode_hex(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(2 + bytes.len() * 2);
    output.push_str("0x");
    for byte in bytes {
        use std::fmt::Write;
        write!(&mut output, "{byte:02x}").expect("hex output");
    }
    output
}

fn optional_bytes(value: &Value) -> Option<Bytes> {
    (!value.is_null()).then(|| Bytes::from(decode_hex(value)))
}

fn witness_args(value: &Value, lock: Option<Bytes>) -> WitnessArgs {
    let lock = lock.or_else(|| optional_bytes(&value["lock"]));
    WitnessArgs::new_builder()
        .lock(lock.pack())
        .input_type(optional_bytes(&value["input_type"]).pack())
        .output_type(optional_bytes(&value["output_type"]).pack())
        .build()
}

fn out_point(value: Value) -> packed::OutPoint {
    let json: JsonOutPoint = serde_json::from_value(value).expect("valid out point");
    json.into()
}

fn cell_output(value: Value) -> packed::CellOutput {
    let json: JsonCellOutput = serde_json::from_value(value).expect("valid cell output");
    json.into()
}

fn install_binary(context: &mut Context, out_point_value: Value, data: Bytes) {
    let output = packed::CellOutput::new_builder()
        .capacity(Capacity::shannons(100_000_000_000).pack())
        .build();
    context.create_cell_with_out_point(out_point(out_point_value), output, data);
}

fn transaction(case: &Value) -> (Context, TransactionView) {
    let input = &case["input"];
    let signing = &input["signing"];
    let oracle = &case["oracle"];
    let first = signing["group_indices"][0]
        .as_u64()
        .expect("first group index") as usize;
    let lock = Bytes::from(decode_hex(&case["result"]["witness_lock"]));
    let witnesses = signing["witnesses"]
        .as_array()
        .expect("witnesses")
        .iter()
        .enumerate()
        .map(|(index, witness)| {
            let replacement = (index == first).then(|| lock.clone());
            Value::String(encode_hex(witness_args(witness, replacement).as_bytes().as_ref()))
        })
        .collect();
    let mut raw = signing["raw_transaction"].clone();
    raw["witnesses"] = Value::Array(witnesses);
    let json: ckb_testtool::ckb_jsonrpc_types::Transaction =
        serde_json::from_value(raw).expect("valid transaction");
    let packed: packed::Transaction = json.into();

    let deps = signing["raw_transaction"]["cell_deps"]
        .as_array()
        .expect("cell deps");
    assert_eq!(deps.len(), 3, "fixture dependency count");
    let mut context = Context::default();
    install_binary(
        &mut context,
        deps[0]["out_point"].clone(),
        BUNDLED_CELL
            .get("specs/cells/secp256k1_blake160_multisig_all")
            .expect("multisig binary")
            .to_vec()
            .into(),
    );
    install_binary(
        &mut context,
        deps[1]["out_point"].clone(),
        BUNDLED_CELL
            .get("specs/cells/secp256k1_data")
            .expect("secp data")
            .to_vec()
            .into(),
    );
    install_binary(
        &mut context,
        deps[2]["out_point"].clone(),
        ALWAYS_SUCCESS.clone(),
    );
    for cell in oracle["input_cells"].as_array().expect("input cells") {
        context.create_cell_with_out_point(
            out_point(cell["out_point"].clone()),
            cell_output(cell["output"].clone()),
            Bytes::from(decode_hex(&cell["data"])),
        );
    }
    (context, packed.into_view())
}

#[test]
fn returned_witnesses_unlock_real_multisig_groups() {
    let cases_path = std::env::var("CKBBENCH_PROJECT_CASES")
        .unwrap_or_else(|_| "/artifact/cases.json".to_owned());
    let document: Value = serde_json::from_str(
        &fs::read_to_string(cases_path).expect("candidate cases"),
    )
    .expect("valid candidate cases");
    let cases = document["cases"].as_array().expect("cases array");
    assert!(!cases.is_empty(), "at least one case is required");

    for case in cases {
        let (context, tx) = transaction(case);
        context
            .verify_tx(&tx, MAX_CYCLES)
            .expect("candidate witness must satisfy the bundled multisig system lock");
    }
}
