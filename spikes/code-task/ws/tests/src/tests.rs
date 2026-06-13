use ckb_testtool::ckb_types::{bytes::Bytes, core::TransactionBuilder, packed::*, prelude::*};
use ckb_testtool::context::Context;

// HIDDEN SUITE (NOT production): the verifier-side grader for the Code Task
// "implement a password lock". The agent authors contracts/hashlock; this suite
// is withheld from the agent and run against whatever binary the agent produced.
//
// It encodes the *intent* of the lock (Rule 9 — tests verify why, not just what):
//   1. with the correct password in the witness, the lock MUST authorize (pass).
//   2. with a wrong password (DIFFERENT LENGTH and SAME LENGTH), the lock MUST
//      reject with exit code 6 — defeats both a hardcode cheat and a length-only
//      cheat that compares `witness.len() == args.len()` instead of the bytes.
//   3. with no witness at all, the lock MUST reject with exit code 5.
//
// A correct implementation passes all of these. The generated always-return-0 stub,
// a hardcoded-password cheat, and a length-only cheat all fail at least one.

const MAX_CYCLES: u64 = 10_000_000;
const EXIT_WRONG_PASSWORD: i8 = 6;
const EXIT_NO_WITNESS: i8 = 5;

// The password is a VERIFIER-PRIVATE run param: the harness injects it at verify
// time via BENCH_PASSWORD, so the agent never sees it. A correct contract that
// reads it from the lock args at runtime passes for ANY value; a contract that
// hardcodes a guessed/leaked password fails when the harness uses a different one.
// We FAIL LOUD if it is unset rather than silently defaulting, so a contract can
// never pass by hardcoding a known default (Rule 12).
fn password() -> Vec<u8> {
    match std::env::var("BENCH_PASSWORD") {
        Ok(p) if !p.is_empty() => p.into_bytes(),
        _ => panic!(
            "BENCH_PASSWORD must be set to a verifier-private per-run secret; \
             refusing to grade with a guessable default"
        ),
    }
}

/// Assert the verification failed specifically with the given script exit code,
/// not merely with *some* error. This is what forces the contract to implement the
/// documented semantics (5 = no witness, 6 = wrong password) rather than any reject.
/// ckb-script renders a script failure as "...ValidationFailure: see error code N
/// on page...", so we match the rendered exit code.
fn assert_rejected_with<E: std::fmt::Debug>(result: Result<u64, E>, want_code: i8) {
    let err = result.expect_err("verification MUST be rejected");
    let msg = format!("{err:?}");
    assert!(
        msg.contains(&format!("error code {want_code} ")),
        "expected rejection with script exit code {want_code}, got: {msg}"
    );
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
fn wrong_password_different_length_is_rejected() {
    // A witness that differs from the password in BOTH content and length.
    let wrong = b"x";
    let (context, tx) = build_tx(Some(wrong));
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), EXIT_WRONG_PASSWORD);
}

#[test]
fn wrong_password_same_length_is_rejected() {
    // A witness with the SAME length as the password but different bytes. This
    // defeats a length-only cheat (`witness.len() == args.len()` instead of bytes).
    let pw = password();
    let mut wrong = pw.clone();
    wrong[0] ^= 0xff; // flip a byte; same length, different content
    let (context, tx) = build_tx(Some(&wrong));
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), EXIT_WRONG_PASSWORD);
}

#[test]
fn missing_witness_is_rejected() {
    let (context, tx) = build_tx(None);
    assert_rejected_with(context.verify_tx(&tx, MAX_CYCLES), EXIT_NO_WITNESS);
}
