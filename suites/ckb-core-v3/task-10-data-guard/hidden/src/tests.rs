use ckb_testtool::builtin::ALWAYS_SUCCESS;
use ckb_testtool::ckb_hash::blake2b_256;
use ckb_testtool::ckb_types::{bytes::Bytes, core::TransactionBuilder, packed::*, prelude::*};
use ckb_testtool::context::Context;

const MAX_CYCLES: u64 = 10_000_000;
const ERROR_ARGS: i8 = 4;
const ERROR_INPUT_SHAPE: i8 = 5;
const ERROR_OUTPUT_SHAPE: i8 = 6;
const ERROR_DATA_HASH: i8 = 7;

fn challenge() -> Vec<u8> {
    match std::env::var("CKBBENCH_CHALLENGE") {
        Ok(value) if !value.is_empty() => value.into_bytes(),
        _ => panic!("CKBBENCH_CHALLENGE must be a non-empty verifier-private value"),
    }
}

fn payload(label: &[u8]) -> Bytes {
    let mut value = challenge();
    value.extend_from_slice(label);
    value.into()
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
    target_inputs: &[Bytes],
    target_outputs: &[Bytes],
    unrelated_group: bool,
) -> (Context, ckb_testtool::ckb_types::core::TransactionView) {
    let mut context = Context::default();
    let always_success = context.deploy_cell(ALWAYS_SUCCESS.clone());
    let lock = context
        .build_script(&always_success, Bytes::from(challenge()))
        .expect("lock");
    let data_guard = context.deploy_cell_by_name("data-guard");
    let target_type = context
        .build_script(&data_guard, Bytes::from(args))
        .expect("target type");
    let unrelated_type = context
        .build_script(&always_success, Bytes::from_static(b"unrelated-type"))
        .expect("unrelated type");

    let mut inputs = Vec::new();
    let funding_data = payload(b"funding");
    let funding = CellOutput::new_builder()
        .capacity(1000)
        .lock(lock.clone())
        .type_(
            if unrelated_group {
                Some(unrelated_type.clone())
            } else {
                None
            }
            .pack(),
        )
        .build();
    inputs.push(
        CellInput::new_builder()
            .previous_output(context.create_cell(funding, funding_data.clone()))
            .build(),
    );
    for data in target_inputs {
        let cell = CellOutput::new_builder()
            .capacity(1000)
            .lock(lock.clone())
            .type_(Some(target_type.clone()).pack())
            .build();
        inputs.push(
            CellInput::new_builder()
                .previous_output(context.create_cell(cell, data.clone()))
                .build(),
        );
    }

    let mut outputs = Vec::new();
    let mut outputs_data = Vec::new();
    outputs.push(
        CellOutput::new_builder()
            .capacity(500)
            .lock(lock.clone())
            .type_(
                if unrelated_group {
                    Some(unrelated_type)
                } else {
                    None
                }
                .pack(),
            )
            .build(),
    );
    outputs_data.push(funding_data);
    for data in target_outputs {
        outputs.push(
            CellOutput::new_builder()
                .capacity(500)
                .lock(lock.clone())
                .type_(Some(target_type.clone()).pack())
                .build(),
        );
        outputs_data.push(data.clone());
    }

    let tx = TransactionBuilder::default()
        .inputs(inputs)
        .outputs(outputs)
        .outputs_data(outputs_data.pack())
        .build();
    let tx = context.complete_tx(tx);
    (context, tx)
}

fn expected_case() -> (Bytes, Vec<u8>) {
    let data = payload(b"expected");
    (data.clone(), blake2b_256(&data).to_vec())
}

#[test]
fn creation_and_single_cell_update_pass() {
    let (data, args) = expected_case();
    let (context, tx) = build_tx(args.clone(), &[], &[data.clone()], false);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("creation must pass");

    let (context, tx) = build_tx(args, &[data.clone()], &[data], false);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("one-to-one update must pass");
}

#[test]
fn unrelated_global_type_group_is_ignored() {
    let (data, args) = expected_case();
    let (context, tx) = build_tx(args, &[data.clone()], &[data], true);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("unrelated type group must not affect the data guard");
}

#[test]
fn mismatched_input_data_is_rejected() {
    let (data, args) = expected_case();
    let wrong = payload(b"wrong-input");
    let (context, tx) = build_tx(args, &[wrong], &[data], false);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_DATA_HASH);
}

#[test]
fn mismatched_output_data_is_rejected() {
    let (data, args) = expected_case();
    let wrong = payload(b"wrong-output");
    let (context, tx) = build_tx(args, &[data], &[wrong], false);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_DATA_HASH);
}

#[test]
fn duplicate_group_inputs_are_rejected() {
    let (data, args) = expected_case();
    let (context, tx) = build_tx(args, &[data.clone(), data.clone()], &[data], false);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_INPUT_SHAPE);
}

#[test]
fn missing_or_duplicate_group_outputs_are_rejected() {
    let (data, args) = expected_case();
    let (context, tx) = build_tx(args.clone(), &[data.clone()], &[], false);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_OUTPUT_SHAPE);

    let (context, tx) = build_tx(args, &[], &[data.clone(), data], false);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_OUTPUT_SHAPE);
}

#[test]
fn malformed_args_are_rejected() {
    let (data, _) = expected_case();
    let (context, tx) = build_tx(vec![0u8; 31], &[], &[data], false);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_ARGS);
}
