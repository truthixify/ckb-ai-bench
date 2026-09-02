use ckb_testtool::builtin::ALWAYS_SUCCESS;
use ckb_testtool::ckb_hash::blake2b_256;
use ckb_testtool::ckb_types::{bytes::Bytes, core::TransactionBuilder, packed::*, prelude::*};
use ckb_testtool::context::Context;

const MAX_CYCLES: u64 = 10_000_000;
const ERROR_ARGS: i8 = 4;
const ERROR_DATA: i8 = 5;
const ERROR_OVERFLOW: i8 = 6;
const ERROR_AMOUNT: i8 = 7;

fn challenge() -> Vec<u8> {
    match std::env::var("CKBBENCH_CHALLENGE") {
        Ok(value) if !value.is_empty() => value.into_bytes(),
        _ => panic!("CKBBENCH_CHALLENGE must be a non-empty verifier-private value"),
    }
}

fn amount(value: u128) -> Bytes {
    value.to_le_bytes().to_vec().into()
}

fn amount_with_suffix(value: u128) -> Bytes {
    let mut data = value.to_le_bytes().to_vec();
    data.extend_from_slice(&challenge());
    data.into()
}

fn assert_rejected_with<E: std::fmt::Debug>(result: Result<u64, E>, want_code: i8) {
    let error = result.expect_err("verification must be rejected");
    let message = format!("{error:?}");
    assert!(
        message.contains(&format!("error code {want_code} ")),
        "expected script exit code {want_code}, got: {message}"
    );
}

struct TransactionCase {
    args_override: Option<Vec<u8>>,
    target_inputs: Vec<Bytes>,
    target_outputs: Vec<Bytes>,
    owner_input_after_funding: bool,
    owner_output: bool,
    unrelated_type_group: bool,
}

impl Default for TransactionCase {
    fn default() -> Self {
        Self {
            args_override: None,
            target_inputs: vec![amount(100)],
            target_outputs: vec![amount(100)],
            owner_input_after_funding: false,
            owner_output: false,
            unrelated_type_group: false,
        }
    }
}

fn build_tx(case: TransactionCase) -> (Context, ckb_testtool::ckb_types::core::TransactionView) {
    let mut context = Context::default();
    let always_success = context.deploy_cell(ALWAYS_SUCCESS.clone());
    let regular_lock = context
        .build_script(&always_success, Bytes::from(challenge()))
        .expect("regular lock");
    let owner_lock = context
        .build_script(&always_success, Bytes::from_static(b"owner-lock"))
        .expect("owner lock");
    let alternate_owner = context
        .build_script(&always_success, Bytes::from_static(b"alternate-owner"))
        .expect("alternate owner");
    let owner_hash = owner_lock.calc_script_hash().raw_data();
    let alternate_owner_hash = alternate_owner.calc_script_hash().raw_data();
    let token_binary = context.deploy_cell_by_name("token-conservation");
    let args = case.args_override.unwrap_or_else(|| owner_hash.to_vec());
    let target_type = context
        .build_script(&token_binary, Bytes::from(args))
        .expect("target token type");
    let alternate_type = context
        .build_script(&token_binary, alternate_owner_hash)
        .expect("alternate token type");

    let mut inputs = Vec::new();
    let funding = CellOutput::new_builder()
        .capacity(1000)
        .lock(regular_lock.clone())
        .build();
    inputs.push(
        CellInput::new_builder()
            .previous_output(context.create_cell(funding, Bytes::new()))
            .build(),
    );
    if case.owner_input_after_funding {
        let owner_cell = CellOutput::new_builder()
            .capacity(1000)
            .lock(owner_lock.clone())
            .build();
        inputs.push(
            CellInput::new_builder()
                .previous_output(context.create_cell(owner_cell, Bytes::new()))
                .build(),
        );
    }
    for data in case.target_inputs {
        let cell = CellOutput::new_builder()
            .capacity(1000)
            .lock(regular_lock.clone())
            .type_(Some(target_type.clone()).pack())
            .build();
        inputs.push(
            CellInput::new_builder()
                .previous_output(context.create_cell(cell, data))
                .build(),
        );
    }
    if case.unrelated_type_group {
        let cell = CellOutput::new_builder()
            .capacity(1000)
            .lock(alternate_owner.clone())
            .type_(Some(alternate_type.clone()).pack())
            .build();
        inputs.push(
            CellInput::new_builder()
                .previous_output(context.create_cell(cell, amount(0)))
                .build(),
        );
    }

    let mut outputs = Vec::new();
    let mut outputs_data = Vec::new();
    outputs.push(
        CellOutput::new_builder()
            .capacity(500)
            .lock(regular_lock.clone())
            .build(),
    );
    outputs_data.push(Bytes::new());
    for data in case.target_outputs {
        outputs.push(
            CellOutput::new_builder()
                .capacity(500)
                .lock(if case.owner_output {
                    owner_lock.clone()
                } else {
                    regular_lock.clone()
                })
                .type_(Some(target_type.clone()).pack())
                .build(),
        );
        outputs_data.push(data);
    }
    if case.unrelated_type_group {
        outputs.push(
            CellOutput::new_builder()
                .capacity(500)
                .lock(regular_lock)
                .type_(Some(alternate_type).pack())
                .build(),
        );
        outputs_data.push(amount(200));
    }

    let tx = TransactionBuilder::default()
        .inputs(inputs)
        .outputs(outputs)
        .outputs_data(outputs_data.pack())
        .build();
    let tx = context.complete_tx(tx);
    (context, tx)
}

#[test]
fn equal_transfer_split_merge_and_burn_pass() {
    let (context, tx) = build_tx(TransactionCase::default());
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("equal transfer must pass");

    let split_merge = TransactionCase {
        target_inputs: vec![amount(60), amount(40)],
        target_outputs: vec![amount(30), amount(70)],
        ..Default::default()
    };
    let (context, tx) = build_tx(split_merge);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("split and merge must count every grouped cell");

    let burn = TransactionCase {
        target_inputs: vec![amount(100)],
        target_outputs: vec![amount(80)],
        ..Default::default()
    };
    let (context, tx) = build_tx(burn);
    context.verify_tx(&tx, MAX_CYCLES).expect("burn must pass");
}

#[test]
fn data_after_the_amount_is_allowed() {
    let case = TransactionCase {
        target_inputs: vec![amount_with_suffix(100)],
        target_outputs: vec![amount_with_suffix(100)],
        ..Default::default()
    };
    let (context, tx) = build_tx(case);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("only the first sixteen bytes encode the amount");
}

#[test]
fn unauthorized_mint_is_rejected() {
    let case = TransactionCase {
        target_inputs: vec![],
        target_outputs: vec![amount(1)],
        ..Default::default()
    };
    let (context, tx) = build_tx(case);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_AMOUNT);
}

#[test]
fn owner_input_beyond_global_index_zero_allows_mint() {
    let case = TransactionCase {
        target_inputs: vec![],
        target_outputs: vec![amount(500)],
        owner_input_after_funding: true,
        ..Default::default()
    };
    let (context, tx) = build_tx(case);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("any owner-locked input must activate owner mode");
}

#[test]
fn owner_lock_on_an_output_does_not_authorize_mint() {
    let case = TransactionCase {
        target_inputs: vec![],
        target_outputs: vec![amount(500)],
        owner_output: true,
        ..Default::default()
    };
    let (context, tx) = build_tx(case);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_AMOUNT);
}

#[test]
fn unrelated_type_group_is_not_counted() {
    let case = TransactionCase {
        unrelated_type_group: true,
        ..Default::default()
    };
    let (context, tx) = build_tx(case);
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("each token type must count only its own group");
}

#[test]
fn short_input_or_output_data_is_rejected() {
    let input_case = TransactionCase {
        target_inputs: vec![Bytes::from(vec![0u8; 15])],
        ..Default::default()
    };
    let (context, tx) = build_tx(input_case);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_DATA);

    let output_case = TransactionCase {
        target_outputs: vec![Bytes::from(vec![0u8; 15])],
        ..Default::default()
    };
    let (context, tx) = build_tx(output_case);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_DATA);
}

#[test]
fn input_and_output_overflow_are_rejected() {
    let input_case = TransactionCase {
        target_inputs: vec![amount(u128::MAX), amount(1)],
        target_outputs: vec![amount(0)],
        ..Default::default()
    };
    let (context, tx) = build_tx(input_case);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_OVERFLOW);

    let output_case = TransactionCase {
        target_inputs: vec![amount(0)],
        target_outputs: vec![amount(u128::MAX), amount(1)],
        ..Default::default()
    };
    let (context, tx) = build_tx(output_case);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_OVERFLOW);
}

#[test]
fn malformed_args_are_rejected() {
    let case = TransactionCase {
        args_override: Some(vec![blake2b_256(challenge())[0]; 31]),
        ..Default::default()
    };
    let (context, tx) = build_tx(case);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), ERROR_ARGS);
}
