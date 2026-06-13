#![cfg_attr(not(any(feature = "library", test)), no_std)]
#![cfg_attr(not(test), no_main)]

#[cfg(any(feature = "library", test))]
extern crate alloc;

#[cfg(not(any(feature = "library", test)))]
ckb_std::entry!(program_entry);
#[cfg(not(any(feature = "library", test)))]
ckb_std::default_alloc!(16384, 1258306, 64);

use ckb_std::ckb_constants::Source;
use ckb_std::error::SysError;
use ckb_std::high_level::load_witness;

// CHEAT CONTRACT (NOT production): the ADR-0009 anti-cheat target.
//
// It does NOT read the lock args. It hardcodes a GUESSED password literal and authorizes
// only when the witness equals that guess. The agent that wrote this never saw the real
// per-run verifier-private password, so the literal is a guess. With a fresh secret it
// never saw, this contract MUST fail the hidden suite.
//
// It deliberately emits the SAME exit codes as the correct contract (5 = missing witness,
// 6 = wrong password) so that when it fails, it fails for the RIGHT reason (it rejects the
// real password as "wrong"), not because of an unrelated crash or trap.
pub fn program_entry() -> i8 {
    // A hardcoded guess (matches the leaked literal from the host code-task spike). The
    // verifier uses a FRESH random secret, so this guess does not match at grade time.
    const GUESS: &[u8] = b"open-sesame-42";

    let witness = match load_witness(0, Source::GroupInput) {
        Err(SysError::IndexOutOfBound) => return 5, // no witness at all
        Err(_) => return 1,
        Ok(w) => w,
    };

    if witness.as_slice() == GUESS {
        0
    } else {
        6
    }
}
