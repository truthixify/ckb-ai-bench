use ckb_testtool::builtin::ALWAYS_SUCCESS;
use ckb_testtool::ckb_types::{bytes::Bytes, core::TransactionBuilder, packed::*, prelude::*};
use ckb_testtool::context::Context;

// HIDDEN SUITE: verifier-side grader for the hashlock Code Task. Withheld from the agent;
// run against the agent's build/release/hashlock binary via TOP=/artifact.

const MAX_CYCLES: u64 = 10_000_000;
const EXIT_WRONG_PASSWORD: i8 = 6;
const EXIT_NO_WITNESS: i8 = 5;

fn password() -> Vec<u8> {
    match std::env::var("BENCH_PASSWORD") {
        Ok(p) if !p.is_empty() => p.into_bytes(),
        _ => panic!(
            "BENCH_PASSWORD must be set to a verifier-private per-run secret; \
             refusing to grade with a guessable default"
        ),
    }
}

fn assert_rejected_with<E: std::fmt::Debug>(result: Result<u64, E>, want_code: i8) {
    let err = result.expect_err("verification MUST be rejected");
    let msg = format!("{err:?}");
    assert!(
        msg.contains(&format!("error code {want_code} ")),
        "expected rejection with script exit code {want_code}, got: {msg}"
    );
}

fn build_tx(
    witness: Option<&[u8]>,
) -> (Context, ckb_testtool::ckb_types::core::TransactionView) {
    let mut context = Context::default();
    let out_point = context.deploy_cell_by_name("hashlock");

    let lock_script = context
        .build_script(&out_point, Bytes::from(password()))
        .expect("script");

    let input_out_point = context.create_cell(
        CellOutput::new_builder()
            .capacity(1000)
            .lock(lock_script.clone())
            .build(),
        Bytes::new(),
    );
    let input = CellInput::new_builder()
        .previous_output(input_out_point)
        .build();
    let outputs = vec![
        CellOutput::new_builder()
            .capacity(500)
            .lock(lock_script)
            .build(),
    ];
    let outputs_data = vec![Bytes::new(); 1];

    let witnesses: Vec<_> = match witness {
        Some(w) => vec![Bytes::from(w.to_vec()).pack()],
        None => vec![],
    };

    let tx = TransactionBuilder::default()
        .input(input)
        .outputs(outputs)
        .outputs_data(outputs_data.pack())
        .witnesses(witnesses)
        .build();
    let tx = context.complete_tx(tx);
    (context, tx)
}

#[test]
fn correct_password_unlocks() {
    let pw = password();
    let (context, tx) = build_tx(Some(&pw));
    let cycles = context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("correct password must unlock");
    println!("unlock consumed {cycles} cycles");
}

#[test]
fn wrong_password_different_length_is_rejected() {
    let wrong = b"x";
    let (context, tx) = build_tx(Some(wrong));
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), EXIT_WRONG_PASSWORD);
}

#[test]
fn wrong_password_same_length_is_rejected() {
    let pw = password();
    let mut wrong = pw.clone();
    wrong[0] ^= 0xff;
    let (context, tx) = build_tx(Some(&wrong));
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), EXIT_WRONG_PASSWORD);
}

#[test]
fn missing_witness_is_rejected() {
    let (context, tx) = build_tx(None);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), EXIT_NO_WITNESS);
}

/// Two lock groups, with the hashlock input deliberately NOT at global index 0.
///
/// With a single input, `Source::Input` index 0 and `Source::GroupInput` index 0 resolve to the
/// same witness, so the four cases above cannot tell them apart. Here global witness 0 belongs to
/// an unrelated always-success input and carries a decoy password, while the hashlock group's
/// first witness is the real one. Only a group-relative read sees the correct bytes.
fn build_multi_group_tx() -> (Context, ckb_testtool::ckb_types::core::TransactionView) {
    let mut context = Context::default();

    let always_success = context.deploy_cell(ALWAYS_SUCCESS.clone());
    let unrelated_lock = context
        .build_script(&always_success, Bytes::new())
        .expect("always-success script");

    let hashlock_out_point = context.deploy_cell_by_name("hashlock");
    let hashlock_lock = context
        .build_script(&hashlock_out_point, Bytes::from(password()))
        .expect("script");

    let unrelated_input = CellInput::new_builder()
        .previous_output(context.create_cell(
            CellOutput::new_builder()
                .capacity(1000)
                .lock(unrelated_lock.clone())
                .build(),
            Bytes::new(),
        ))
        .build();
    let hashlock_input = CellInput::new_builder()
        .previous_output(context.create_cell(
            CellOutput::new_builder()
                .capacity(1000)
                .lock(hashlock_lock.clone())
                .build(),
            Bytes::new(),
        ))
        .build();

    let pw = password();
    let mut decoy = pw.clone();
    decoy[0] ^= 0xff;

    let tx = TransactionBuilder::default()
        .input(unrelated_input)
        .input(hashlock_input)
        .outputs(vec![
            CellOutput::new_builder()
                .capacity(900)
                .lock(unrelated_lock)
                .build(),
            CellOutput::new_builder()
                .capacity(900)
                .lock(hashlock_lock)
                .build(),
        ])
        .outputs_data(vec![Bytes::new(); 2].pack())
        .witnesses(vec![
            Bytes::from(decoy).pack(), // global witness 0: what a Source::Input read would see
            Bytes::from(pw).pack(),    // global witness 1: the hashlock group's witness 0
        ])
        .build();
    let tx = context.complete_tx(tx);
    (context, tx)
}

#[test]
fn witness_read_from_wrong_source_is_rejected() {
    let (context, tx) = build_multi_group_tx();
    let global_0 = tx.witnesses().get(0).expect("global witness 0");
    let group_0 = tx.witnesses().get(1).expect("global witness 1");
    assert_ne!(
        global_0.raw_data(),
        group_0.raw_data(),
        "the two witnesses must differ or this case proves nothing"
    );
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("group-relative witness read must unlock; a global read sees the decoy");
}
