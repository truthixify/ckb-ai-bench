use ckb_testtool::builtin::ALWAYS_SUCCESS;
use ckb_testtool::ckb_jsonrpc_types::{CellDep as JsonCellDep, OutPoint as JsonOutPoint};
use ckb_testtool::ckb_types::{
    bytes::Bytes,
    core::{Capacity, ScriptHashType, TransactionBuilder},
    packed::{self, CellInput, CellOutput, OutPointVec, Script},
    prelude::*,
};
use ckb_testtool::context::Context;
use serde_json::Value;
use std::fs;

const MAX_CYCLES: u64 = 10_000_000;

fn out_point(value: Value) -> packed::OutPoint {
    let json: JsonOutPoint = serde_json::from_value(value).expect("valid out point");
    json.into()
}

fn cell_dep(value: Value) -> packed::CellDep {
    let json: JsonCellDep = serde_json::from_value(value).expect("valid cell dep");
    json.into()
}

fn dep_output() -> CellOutput {
    CellOutput::new_builder()
        .capacity(Capacity::shannons(100_000_000_000).pack())
        .build()
}

fn install_selected_deps(context: &mut Context, case: &Value) -> Vec<packed::CellDep> {
    let selected_values = case["result"]["cell_deps"]
        .as_array()
        .expect("selected dependencies");
    let selected: Vec<_> = selected_values
        .iter()
        .cloned()
        .map(cell_dep)
        .collect();
    assert!(!selected.is_empty(), "at least one dependency is required");
    let selected_points: Vec<_> = selected.iter().map(|dep| dep.out_point()).collect();

    for (ordinal, (value, dep)) in selected_values.iter().zip(selected.iter()).enumerate() {
        let candidate = dep.out_point();
        match value["dep_type"].as_str().expect("dependency type") {
            "code" => context.create_cell_with_out_point(
                candidate,
                dep_output(),
                ALWAYS_SUCCESS.clone(),
            ),
            "dep_group" => {
                let mut member_value = case["oracle"]["always_success_member"].clone();
                let mut member_index = ordinal + 1;
                let member = loop {
                    member_value["index"] = Value::String(format!("0x{member_index:x}"));
                    let candidate_member = out_point(member_value.clone());
                    if !selected_points.iter().any(|point| point == &candidate_member) {
                        break candidate_member;
                    }
                    member_index += 1;
                };
                context.create_cell_with_out_point(member.clone(), dep_output(), ALWAYS_SUCCESS.clone());
                let group = OutPointVec::new_builder().push(member).build();
                context.create_cell_with_out_point(candidate, dep_output(), group.as_bytes());
            }
            _ => panic!("unsupported dependency type"),
        }
    }
    selected
}

fn verify_with_dependencies(context: &mut Context, deps: Vec<packed::CellDep>) {
    let lock = Script::new_builder()
        .code_hash(CellOutput::calc_data_hash(&ALWAYS_SUCCESS))
        .hash_type(ScriptHashType::Data)
        .build();
    let capacity = Capacity::shannons(100_000_000_000);
    let input_out_point = context.create_cell(
        CellOutput::new_builder()
            .capacity(capacity.pack())
            .lock(lock.clone())
            .build(),
        Bytes::new(),
    );
    let tx = TransactionBuilder::default()
        .input(CellInput::new_builder().previous_output(input_out_point).build())
        .outputs(vec![
            CellOutput::new_builder()
                .capacity(capacity.pack())
                .lock(lock)
                .build(),
        ])
        .outputs_data(vec![Bytes::new()].pack())
        .cell_deps(deps)
        .build();
    context
        .verify_tx(&tx, MAX_CYCLES)
        .expect("returned dependencies must execute the probe lock");
}

#[test]
fn every_returned_dependency_executes_a_probe_transaction() {
    let cases_path = std::env::var("CKBBENCH_PROJECT_CASES")
        .unwrap_or_else(|_| "/artifact/cases.json".to_owned());
    let document: Value = serde_json::from_str(
        &fs::read_to_string(cases_path).expect("candidate cases"),
    )
    .expect("valid candidate cases");
    let cases = document["cases"].as_array().expect("cases array");
    assert!(!cases.is_empty(), "at least one case is required");

    for case in cases {
        let mut context = Context::default();
        let deps = install_selected_deps(&mut context, case);
        for dep in &deps {
            verify_with_dependencies(&mut context, vec![dep.clone()]);
        }
        verify_with_dependencies(&mut context, deps);
    }
}
