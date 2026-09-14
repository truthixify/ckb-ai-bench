.PHONY: build

build:
	mkdir -p build/release
	"$(CKB_SDK_HOME)/node_modules/.bin/esbuild" state-transition.js --bundle --platform=neutral --minify --external:@ckb-js-std/bindings --target=es2022 --outfile=build/release/state-transition.bundle.js
	ckb-debugger --read-file build/release/state-transition.bundle.js --bin "$(CKB_SDK_HOME)/node_modules/ckb-testtool/src/unittest/defaultScript/ckb-js-vm" -- -c build/release/state-transition.bc
