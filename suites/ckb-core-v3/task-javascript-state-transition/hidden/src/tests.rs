use std::process::Command;

fn verify(case_name: &str) {
    let verifier = format!("{}/verify.mjs", env!("CARGO_MANIFEST_DIR"));
    let status = Command::new("node")
        .arg(verifier)
        .arg(case_name)
        .status()
        .expect("JavaScript verifier must start");
    assert!(status.success(), "case {case_name} failed");
}

#[test]
fn increments_one_state() { verify("increments-one-state"); }

#[test]
fn increments_multiple_group_cells() { verify("increments-multiple-group-cells"); }

#[test]
fn ignores_unrelated_cells() { verify("ignores-unrelated-cells"); }

#[test]
fn rejects_unchanged_state() { verify("rejects-unchanged-state"); }

#[test]
fn rejects_skipped_state() { verify("rejects-skipped-state"); }

#[test]
fn rejects_invalid_later_state() { verify("rejects-invalid-later-state"); }

#[test]
fn rejects_malformed_input_data() { verify("rejects-malformed-input-data"); }

#[test]
fn rejects_malformed_output_data() { verify("rejects-malformed-output-data"); }

#[test]
fn rejects_missing_group_output() { verify("rejects-missing-group-output"); }

#[test]
fn rejects_extra_group_output() { verify("rejects-extra-group-output"); }

#[test]
fn rejects_overflow() { verify("rejects-overflow"); }
