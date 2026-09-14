#![no_std]
#![no_main]

ckb_std::entry!(program_entry);
ckb_std::default_alloc!(16384, 1258306, 64);

use ckb_std::ckb_constants::Source;
use ckb_std::error::SysError;
use ckb_std::high_level::{load_cell_data, load_script, load_witness};

fn sum(source: Source) -> Result<u64, i8> {
    let mut total = 0u64;
    let mut index = 0;
    loop {
        let data = match load_cell_data(index, source) {
            Ok(data) => data,
            Err(SysError::IndexOutOfBound) => break,
            Err(_) => return Err(1),
        };
        let bytes: [u8; 8] = data.as_slice().try_into().map_err(|_| 5)?;
        total = total.wrapping_add(u64::from_le_bytes(bytes));
        index += 1;
    }
    Ok(total)
}

fn program_entry() -> i8 {
    let script = match load_script() {
        Ok(value) => value,
        Err(_) => return 1,
    };
    let args = script.args().raw_data();
    if args.len() != 32 {
        return 4;
    }
    match load_witness(0, Source::Input) {
        Ok(value) if value.as_slice() == args.as_ref() => {}
        Ok(_) => return 8,
        Err(SysError::IndexOutOfBound) => return 8,
        Err(_) => return 1,
    }
    let inputs = match sum(Source::Input) {
        Ok(value) => value,
        Err(code) => return code,
    };
    let outputs = match sum(Source::Output) {
        Ok(value) => value,
        Err(code) => return code,
    };
    if inputs == outputs { 0 } else { 7 }
}
