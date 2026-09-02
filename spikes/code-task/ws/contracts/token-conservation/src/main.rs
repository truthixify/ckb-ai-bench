#![cfg_attr(not(any(feature = "library", test)), no_std)]
#![cfg_attr(not(test), no_main)]

#[cfg(not(any(feature = "library", test)))]
ckb_std::entry!(program_entry);
#[cfg(not(any(feature = "library", test)))]
ckb_std::default_alloc!(16384, 1258306, 64);

use ckb_std::ckb_constants::Source;
use ckb_std::ckb_types::bytes::Bytes;
use ckb_std::ckb_types::prelude::*;
use ckb_std::error::SysError;
use ckb_std::high_level::{load_cell_data, load_cell_lock_hash, load_script};

const ERROR_SYSCALL: i8 = 1;
const ERROR_ARGS: i8 = 4;
const ERROR_DATA: i8 = 5;
const ERROR_OVERFLOW: i8 = 6;
const ERROR_AMOUNT: i8 = 7;

fn owner_mode(owner: &[u8; 32]) -> Result<bool, i8> {
    let source = if cfg!(feature = "mutant-owner-output") {
        Source::Output
    } else {
        Source::Input
    };
    let mut index = 0;
    loop {
        match load_cell_lock_hash(index, source) {
            Ok(hash) if hash == *owner => return Ok(true),
            Ok(_) => index += 1,
            Err(SysError::IndexOutOfBound) => return Ok(false),
            Err(_) => return Err(ERROR_SYSCALL),
        }
    }
}

fn sum_amounts(group_source: Source) -> Result<u128, i8> {
    let source = if cfg!(feature = "mutant-global-source") {
        match group_source {
            Source::GroupInput => Source::Input,
            Source::GroupOutput => Source::Output,
            _ => group_source,
        }
    } else {
        group_source
    };
    let mut total = 0u128;
    let mut index = 0;
    loop {
        let data = match load_cell_data(index, source) {
            Ok(data) => data,
            Err(SysError::IndexOutOfBound) => break,
            Err(_) => return Err(ERROR_SYSCALL),
        };
        let raw: [u8; 16] = match data.get(..16).and_then(|value| value.try_into().ok()) {
            Some(value) => value,
            None => return Err(ERROR_DATA),
        };
        let amount = u128::from_le_bytes(raw);
        total = if cfg!(feature = "mutant-wrapping-sum") {
            total.wrapping_add(amount)
        } else {
            total.checked_add(amount).ok_or(ERROR_OVERFLOW)?
        };
        index += 1;
        if cfg!(feature = "mutant-first-only") {
            break;
        }
    }
    Ok(total)
}

pub fn program_entry() -> i8 {
    let script = match load_script() {
        Ok(script) => script,
        Err(_) => return ERROR_SYSCALL,
    };
    let args: Bytes = script.args().unpack();
    let owner: [u8; 32] = match args.as_ref().try_into() {
        Ok(value) => value,
        Err(_) => return ERROR_ARGS,
    };
    match owner_mode(&owner) {
        Ok(true) => return 0,
        Ok(false) => {}
        Err(code) => return code,
    }

    let inputs = match sum_amounts(Source::GroupInput) {
        Ok(value) => value,
        Err(code) => return code,
    };
    let outputs = match sum_amounts(Source::GroupOutput) {
        Ok(value) => value,
        Err(code) => return code,
    };
    let accepted = if cfg!(feature = "mutant-equal-only") {
        inputs == outputs
    } else {
        inputs >= outputs
    };
    if accepted { 0 } else { ERROR_AMOUNT }
}
