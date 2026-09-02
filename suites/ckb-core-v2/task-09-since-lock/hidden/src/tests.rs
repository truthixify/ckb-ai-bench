use ckb_testtool::builtin::ALWAYS_SUCCESS;
use ckb_testtool::ckb_hash::blake2b_256;
use ckb_testtool::ckb_types::{bytes::Bytes, core::TransactionBuilder, packed::*, prelude::*};
use ckb_testtool::context::Context;

const MAX_CYCLES: u64 = 10_000_000;
const ERROR_ARGS: i8 = 4;
const ERROR_SINCE: i8 = 5;
const RELATIVE_BLOCK: u64 = 1 << 63;
const RELATIVE_EPOCH: u64 = (1 << 63) | (1 << 61);
const RELATIVE_TIMESTAMP: u64 = (1 << 63) | (1 << 62);

fn challenge() -> Vec<u8> {
    match std::env::var("CKBBENCH_CHALLENGE") {
        Ok(value) if !value.is_empty() => value.into_bytes(),
        _ => panic!("CKBBENCH_CHALLENGE must be a non-empty verifier-private value"),
    }
}

fn threshold_number() -> u64 {
    20 + u64::from(blake2b_256(challenge())[0] % 20)
}

fn threshold() -> u64 {
    RELATIVE_BLOCK | threshold_number()
}

fn assert_rejected_with<E: std::fmt::Debug>(result: Result<u64, E>, want_code: i8) {
    let error = result.expect_err("verification must be rejected");
    let message = format!("{error:?}");
    assert!(
        message.contains(&format!("error code {want_code} ")),
        "expected script exit code {want_code}, got: {message}"
    );
}

fn build_tx(
    args: Vec<u8>,
    grouped_since: &[u64],
    unrelated_first: Option<u64>,
) -> (Context, ckb_testtool::ckb_types::core::TransactionView) {
    let mut context = Context::default();
    let always_success = context.deploy_cell(ALWAYS_SUCCESS.clone());
    let unrelated_lock = context
        .build_script(&always_success, Bytes::from(challenge()))
        .expect("unrelated lock");
    let since_binary = context.deploy_cell_by_name("since-lock");
    let since_lock = context
        .build_script(&since_binary, Bytes::from(args))
        .expect("since lock");

    let mut inputs = Vec::new();
    if let Some(value) = unrelated_first {
        let out_point = context.create_cell(
            CellOutput::new_builder()
                .capacity(1000)
                .lock(unrelated_lock.clone())
                .build(),
            Bytes::new(),
        );
        inputs.push(
            CellInput::new_builder()
                .previous_output(out_point)
                .since(value)
                .build(),
        );
    }
    for value in grouped_since {
        let out_point = context.create_cell(
            CellOutput::new_builder()
                .capacity(1000)
                .lock(since_lock.clone())
                .build(),
            Bytes::new(),
        );
        inputs.push(
            CellInput::new_builder()
                .previous_output(out_point)
                .since(*value)
                .build(),
        );
    }
    let outputs = vec![
        CellOutput::new_builder()
            .capacity(500)
            .lock(unrelated_lock)
            .build(),
    ];
    let tx = TransactionBuilder::default()
        .inputs(inputs)
        .outputs(outputs)
        .outputs_data(vec![Bytes::new()].pack())
        .build();
    (context.clone(), context.complete_tx(tx))
}

#[test]
fn exact_threshold_passes() {
    let (context, tx) = build_tx(threshold().to_le_bytes().to_vec(), &[threshold()], None);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("exact threshold must pass");
}

#[test]
fn larger_compatible_value_passes() {
    let actual = RELATIVE_BLOCK | (threshold_number() + 50);
    let (context, tx) = build_tx(threshold().to_le_bytes().to_vec(), &[actual], None);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("larger compatible since must pass");
}

#[test]
fn every_group_input_is_checked() {
    let high = RELATIVE_BLOCK | (threshold_number() + 1);
    let low = RELATIVE_BLOCK | (threshold_number() - 1);
    let (context, tx) = build_tx(threshold().to_le_bytes().to_vec(), &[high, low], None);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_SINCE);
}

#[test]
fn unrelated_global_input_is_ignored() {
    let decoy = RELATIVE_BLOCK | (threshold_number() - 1);
    let (context, tx) = build_tx(
        threshold().to_le_bytes().to_vec(),
        &[threshold()],
        Some(decoy),
    );
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("only group-relative inputs are governed");
}

#[test]
fn lower_value_is_rejected() {
    let low = RELATIVE_BLOCK | (threshold_number() - 1);
    let (context, tx) = build_tx(threshold().to_le_bytes().to_vec(), &[low], None);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_SINCE);
}

#[test]
fn absolute_value_is_rejected() {
    let (context, tx) = build_tx(
        threshold().to_le_bytes().to_vec(),
        &[threshold_number()],
        None,
    );
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_SINCE);
}

#[test]
fn incompatible_metric_is_rejected_even_when_raw_value_is_larger() {
    let actual = RELATIVE_TIMESTAMP | (threshold_number() + 1000);
    let (context, tx) = build_tx(threshold().to_le_bytes().to_vec(), &[actual], None);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_SINCE);
}

#[test]
fn invalid_metric_flags_are_rejected() {
    let actual = RELATIVE_EPOCH | RELATIVE_TIMESTAMP | threshold_number();
    let (context, tx) = build_tx(threshold().to_le_bytes().to_vec(), &[actual], None);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_SINCE);
}

#[test]
fn malformed_or_absolute_threshold_is_rejected() {
    let (context, tx) = build_tx(vec![0u8; 7], &[threshold()], None);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_ARGS);

    let (context, tx) = build_tx(
        threshold_number().to_le_bytes().to_vec(),
        &[threshold()],
        None,
    );
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_ARGS);
}
