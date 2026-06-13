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