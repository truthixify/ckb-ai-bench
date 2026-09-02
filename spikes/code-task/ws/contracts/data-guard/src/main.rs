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
use ckb_std::high_level::{load_cell_data_hash, load_script};

const ERROR_SYSCALL: i8 = 1;
const ERROR_ARGS: i8 = 4;
const ERROR_INPUT_SHAPE: i8 = 5;
const ERROR_OUTPUT_SHAPE: i8 = 6;
const ERROR_DATA_HASH: i8 = 7;

fn validate_group(source: Source, expected: &[u8; 32], maximum: usize) -> Result<usize, i8> {
    let mut count = 0;
    loop {
        let hash = match load_cell_data_hash(count, source) {
            Ok(hash) => hash,
            Err(SysError::IndexOutOfBound) => break,
            Err(_) => return Err(ERROR_SYSCALL),
        };
        if hash != *expected {
            return Err(ERROR_DATA_HASH);
        }
        count += 1;
        if cfg!(feature = "mutant-shape-blind") {
            break;
        }
        if count > maximum {
            break;
        }
    }
    Ok(count)
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
    let expected: [u8; 32] = match args.as_ref().try_into() {
        Ok(value) => value,
        Err(_) => return ERROR_ARGS,
    };
    let input_source = if cfg!(feature = "mutant-global-source") {
        Source::Input
    } else {
        Source::GroupInput
    };
    let output_source = if cfg!(feature = "mutant-global-source") {
        Source::Output
    } else {
        Source::GroupOutput
    };

    if !cfg!(feature = "mutant-output-only") {
        match validate_group(input_source, &expected, 1) {
            Ok(count) if count <= 1 => {}
            Ok(_) => return ERROR_INPUT_SHAPE,
            Err(code) => return code,
        }
    }
    match validate_group(output_source, &expected, 1) {
        Ok(1) => 0,
        Ok(_) => ERROR_OUTPUT_SHAPE,
        Err(code) => code,
    }
}
