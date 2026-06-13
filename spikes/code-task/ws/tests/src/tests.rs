use ckb_testtool::ckb_types::{bytes::Bytes, core::TransactionBuilder, packed::*, prelude::*};
use ckb_testtool::context::Context;

// HIDDEN SUITE (NOT production): the verifier-side grader for the Code Task
// "implement a password lock". The agent authors contracts/hashlock; this suite
// is withheld from the agent and run against whatever binary the agent produced.
//
// It encodes the *intent* of the lock (Rule 9 — tests verify why, not just what):
//   1. with the correct password in the witness, the lock MUST authorize (pass).
//   2. with a wrong password, the lock MUST reject (fail).
//   3. with no witness at all, the lock MUST reject (fail).
//
// A correct implementation passes all three. The generated always-return-0 stub
// passes (1) but fails (2) and (3), so the suite catches a wrong submission.

const MAX_CYCLES: u64 = 10_000_000;

// The password is a VERIFIER-PRIVATE run param: the harness injects it at verify
// time via BENCH_PASSWORD, so the agent never sees it. A correct contract that
// reads it from the lock args at runtime passes for ANY value; a contract that
// hardcodes a guessed/leaked password fails when the harness uses a different one.
fn password() -> Vec<u8> {
    std::env::var("BENCH_PASSWORD")
        .unwrap_or_else(|_| "open-sesame-42".to_string())
        .into_bytes()
}

/// Build a one-input/one-output tx locked by the password lock, with `witness`
/// as the first (and only) witness. Returns (context, tx) ready to verify.
fn build_tx(
    witness: Option<&[u8]>,
) -> (Context, ckb_testtool::ckb_types::core::TransactionView) {
    let mut context = Context::default();
    let out_point = context.deploy_cell_by_name("hashlock");

    // lock args = the verifier-private expected password (the harness sets this;
    // the agent's contract must read it from args at runtime, never hardcode it).
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
fn wrong_password_is_rejected() {
    let (context, tx) = build_tx(Some(b"wrong-password"));
    let err = context
        .verify_tx(&tx, MAX_CYCLES)
        .expect_err("a wrong password MUST be rejected");
    println!("rejected as expected: {err:?}");
}

#[test]
fn missing_witness_is_rejected() {
    let (context, tx) = build_tx(None);
    let err = context
        .verify_tx(&tx, MAX_CYCLES)
        .expect_err("a missing witness MUST be rejected");
    println!("rejected as expected: {err:?}");
}
