#![cfg_attr(not(any(feature = "library", test)), no_std)]
#![cfg_attr(not(test), no_main)]

#[cfg(not(any(feature = "library", test)))]
ckb_std::entry!(program_entry);
#[cfg(not(any(feature = "library", test)))]
ckb_std::default_alloc!(16384, 1258306, 64);

use core::cmp::Ordering;

use ckb_std::ckb_constants::Source;
use ckb_std::ckb_types::bytes::Bytes;
use ckb_std::ckb_types::prelude::*;
use ckb_std::error::SysError;
use ckb_std::high_level::{load_input_since, load_script};
use ckb_std::since::Since;

const ERROR_SYSCALL: i8 = 1;
const ERROR_ARGS: i8 = 4;
const ERROR_SINCE: i8 = 5;

fn valid_relative(value: Since) -> bool {
    value.flags_is_valid() && value.is_relative() && value.extract_lock_value().is_some()
}

pub fn program_entry() -> i8 {
    if cfg!(feature = "mutant-accept-all") {
        return 0;
    }

    let script = match load_script() {
        Ok(script) => script,
        Err(_) => return ERROR_SYSCALL,
    };
    let args: Bytes = script.args().unpack();
    let raw: [u8; 8] = match args.as_ref().try_into() {
        Ok(raw) => raw,
        Err(_) => return ERROR_ARGS,
    };
    let threshold_raw = u64::from_le_bytes(raw);
    let threshold = Since::new(threshold_raw);
    if !valid_relative(threshold) {
        return ERROR_ARGS;
    }

    let source = if cfg!(feature = "mutant-global-source") {
        Source::Input
    } else {
        Source::GroupInput
    };
    let mut index = 0;
    loop {
        let actual_raw = match load_input_since(index, source) {
            Ok(value) => value,
            Err(SysError::IndexOutOfBound) => break,
            Err(_) => return ERROR_SYSCALL,
        };
        let actual = Since::new(actual_raw);
        if !valid_relative(actual) {
            return ERROR_SINCE;
        }
        let accepted = if cfg!(feature = "mutant-numeric-compare") {
            actual_raw >= threshold_raw
        } else {
            matches!(
                actual.partial_cmp(&threshold),
                Some(Ordering::Equal | Ordering::Greater)
            )
        };
        if !accepted {
            return ERROR_SINCE;
        }
        index += 1;
        if cfg!(feature = "mutant-first-only") {
            break;
        }
    }
    if index == 0 { ERROR_SINCE } else { 0 }
}
