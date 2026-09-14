.PHONY: build

build:
	mkdir -p build/release
	RUSTFLAGS="-C target-feature=+zba,+zbb,+zbc,+zbs -C passes=lower-atomic" TARGET_CC=clang TARGET_AR=llvm-ar cargo build --release --locked --offline --target riscv64imac-unknown-none-elf
	cp target/riscv64imac-unknown-none-elf/release/grouped-cell build/release/grouped-cell
	llvm-strip build/release/grouped-cell
