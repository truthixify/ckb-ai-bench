use ckb_testtool::builtin::ALWAYS_SUCCESS;
use ckb_testtool::ckb_hash::blake2b_256;
use ckb_testtool::ckb_types::{bytes::Bytes, core::TransactionBuilder, packed::*, prelude::*};
use ckb_testtool::context::Context;

const MAX_CYCLES: u64 = 10_000_000;

#[derive(Clone, Copy)]
enum WitnessMode {
    Correct,
    Missing,
    Wrong,
}

struct Case {
    args_len: usize,
    inputs: Vec<Bytes>,
    outputs: Vec<Bytes>,
    unrelated: bool,
    witness: WitnessMode,
}

impl Default for Case {
    fn default() -> Self {
        Self {
            args_len: 32,
            inputs: vec![amount(7)],
            outputs: vec![amount(7)],
            unrelated: true,
            witness: WitnessMode::Correct,
        }
    }
}

fn challenge() -> [u8; 32] {
    let value = std::env::var("CKBBENCH_CHALLENGE")
        .expect("CKBBENCH_CHALLENGE must be present");
    assert!(!value.is_empty(), "CKBBENCH_CHALLENGE must not be empty");
    blake2b_256(value.as_bytes())
}

fn amount(value: u64) -> Bytes {
    value.to_le_bytes().to_vec().into()
}

fn assert_rejected_with<E: std::fmt::Debug>(result: Result<u64, E>, code: i8) {
    let error = result.expect_err("transaction must be rejected");
    let message = format!("{error:?}");
    assert!(
        message.contains(&format!("error code {code} ")),
        "expected script exit code {code}, got {message}"
    );
}

fn build(case: Case) -> (Context, ckb_testtool::ckb_types::core::TransactionView) {
    let mut context = Context::default();
    let always = context.deploy_cell(ALWAYS_SUCCESS.clone());
    let regular = context.build_script(&always, Bytes::new()).expect("regular lock");
    let unrelated_type = context
        .build_script(&always, Bytes::from_static(b"unrelated"))
        .expect("unrelated type");
    let binary = context.deploy_cell_by_name("grouped-cell");
    let mut args = challenge().to_vec();
    args.truncate(case.args_len);
    let target = context.build_script(&binary, Bytes::from(args.clone())).expect("target type");

    let mut inputs = Vec::new();
    let mut witnesses = Vec::new();
    let funding = CellOutput::new_builder().capacity(1000).lock(regular.clone()).build();
    inputs.push(CellInput::new_builder().previous_output(
        context.create_cell(funding, Bytes::from_static(b"not-group-data")),
    ).build());
    witnesses.push(Bytes::from_static(b"global-decoy").pack());

    for (index, data) in case.inputs.into_iter().enumerate() {
        let cell = CellOutput::new_builder()
            .capacity(1000)
            .lock(regular.clone())
            .type_(Some(target.clone()).pack())
            .build();
        inputs.push(CellInput::new_builder().previous_output(context.create_cell(cell, data)).build());
        let witness = if index == 0 {
            match case.witness {
                WitnessMode::Correct => Bytes::from(args.clone()),
                WitnessMode::Wrong => Bytes::from_static(b"wrong-group-witness"),
                WitnessMode::Missing => Bytes::new(),
            }
        } else {
            Bytes::from_static(b"later-group-witness")
        };
        witnesses.push(witness.pack());
        if case.unrelated && index == 0 {
            let cell = CellOutput::new_builder()
                .capacity(1000)
                .lock(regular.clone())
                .type_(Some(unrelated_type.clone()).pack())
                .build();
            inputs.push(CellInput::new_builder().previous_output(
                context.create_cell(cell, Bytes::from_static(b"unrelated-input")),
            ).build());
            witnesses.push(Bytes::from_static(b"unrelated-witness").pack());
        }
    }

    let mut outputs = vec![CellOutput::new_builder().capacity(500).lock(regular.clone()).build()];
    let mut outputs_data = vec![Bytes::from_static(b"not-group-output")];
    for (index, data) in case.outputs.into_iter().enumerate() {
        outputs.push(
            CellOutput::new_builder()
                .capacity(500)
                .lock(regular.clone())
                .type_(Some(target.clone()).pack())
                .build(),
        );
        outputs_data.push(data);
        if case.unrelated && index == 0 {
            outputs.push(
                CellOutput::new_builder()
                    .capacity(500)
                    .lock(regular.clone())
                    .type_(Some(unrelated_type.clone()).pack())
                    .build(),
            );
            outputs_data.push(Bytes::from_static(b"unrelated-output"));
        }
    }
    let tx = TransactionBuilder::default()
        .inputs(inputs)
        .outputs(outputs)
        .outputs_data(outputs_data.pack())
        .witnesses(witnesses)
        .build();
    let tx = context.complete_tx(tx);
    (context, tx)
}

#[test]
fn accepts_non_contiguous_group_and_later_members() {
    let case = Case {
        inputs: vec![amount(2), amount(5)],
        outputs: vec![amount(3), amount(4)],
        ..Default::default()
    };
    let (context, tx) = build(case);
    context.verify_tx(&tx, MAX_CYCLES).expect("valid grouped transition");
}

#[test]
fn unrelated_cells_do_not_enter_group_totals() {
    let (context, tx) = build(Case::default());
    context.verify_tx(&tx, MAX_CYCLES).expect("unrelated cells must be ignored");
}

#[test]
fn malformed_group_data_is_rejected() {
    for case in [
        Case { inputs: vec![Bytes::from_static(b"short")], ..Default::default() },
        Case { outputs: vec![Bytes::from_static(b"too-long-data")], ..Default::default() },
    ] {
        let (context, tx) = build(case);
        assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), 5);
    }
}

#[test]
fn missing_or_wrong_group_witness_is_rejected() {
    for mode in [WitnessMode::Missing, WitnessMode::Wrong] {
        let (context, tx) = build(Case { witness: mode, ..Default::default() });
        assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), 8);
    }
}

#[test]
fn unequal_group_totals_are_rejected() {
    let (context, tx) = build(Case {
        inputs: vec![amount(2), amount(5)],
        outputs: vec![amount(2), amount(4)],
        ..Default::default()
    });
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), 7);
}

#[test]
fn group_sum_overflow_is_rejected() {
    let (context, tx) = build(Case {
        inputs: vec![amount(u64::MAX), amount(1)],
        outputs: vec![amount(0)],
        ..Default::default()
    });
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), 6);
}

#[test]
fn malformed_script_args_are_rejected() {
    let (context, tx) = build(Case { args_len: 31, ..Default::default() });
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), 4);
}
