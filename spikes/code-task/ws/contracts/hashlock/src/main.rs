#![cfg_attr(not(any(feature = "library", test)), no_std)]
#![cfg_attr(not(test), no_main)]

#[cfg(any(feature = "library", test))]
extern crate alloc;

#[cfg(not(any(feature = "library", test)))]
ckb_std::entry!(program_entry);
#[cfg(not(any(feature = "library", test)))]
ckb_std::default_alloc!(16384, 1258306, 64);

use ckb_std::ckb_constants::Source;
use ckb_std::ckb_types::bytes::Bytes;
use ckb_std::ckb_types::prelude::*;
use ckb_std::error::SysError;
use ckb_std::high_level::{load_script, load_witness};

// SPIKE CONTRACT (NOT production): a "password lock".
//
// The lock's script args carry a secret password. The cell unlocks only if the
// first witness in the script group byte-equals that password. This is a real,
// objectively-defined rule with exactly one correct behavior, so a hidden test
// suite can distinguish a correct implementation from a wrong one.
//
// Exit codes:
//   0  -> authorized (witness == args)
//   5  -> no witness provided
//   6  -> witness does not match the password
pub fn program_entry() -> i8 {
    let script = match load_script() {
        Ok(s) => s,
        Err(_) => return 1,
    };
    let args: Bytes = script.args().unpack();

    let witness = match load_witness(0, Source::GroupInput) {
        Err(SysError::IndexOutOfBound) => return 5, // no witness at all
        Err(_) => return 1,
        Ok(w) => w,
    };

    if witness.as_slice() == args.as_ref() {
        0
    } else {
        6
    }
}
