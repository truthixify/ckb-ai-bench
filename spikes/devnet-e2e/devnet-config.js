// Spike (NOT production): CCC client config for the nervos/ckb --chain dev sidecar.
//
// CCC's ClientPublicTestnet accepts a custom RPC url and a custom `scripts` map.
// The devnet secp256k1_blake160 sighash system script has the SAME code_hash as
// testnet (it is a genesis system cell), but its cellDep out-point lives in the
// devnet genesis block, not testnet's deploy tx. We pin the devnet out-point
// (genesis tx[1] index 0, a depGroup) discovered from get_block_by_number(0x0).
//
// The funded genesis key is a PUBLIC dev key from dev.toml (safe to commit).

import { ClientPublicTestnet, KnownScript } from "@ckb-ccc/core";
import { TESTNET_SCRIPTS } from "@ckb-ccc/core/advanced";

export const DEVNET_RPC = "http://localhost:18120";

// PUBLIC dev key for lock args 0xc8328aab... (20B CKB at genesis). NOT a secret.
export const GENESIS_PRIVKEY =
  "0xd00c06bfd800d27397002dca6fb0993d5ba6399b4238b2f29ee9deb97593d2bc";

// Genesis dep-group out-point for secp256k1_blake160_sighash_all on --chain dev.
const DEVNET_SECP256K1_SIGHASH = {
  codeHash:
    "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
  hashType: "type",
  cellDeps: [
    {
      cellDep: {
        outPoint: {
          // genesis tx[1] = the dep groups; index 0 = secp256k1 sighash group
          txHash:
            "0x6b092c0cdacdddaa8e9cdcb0c9331c455244c4ee8f7d0ed9aa2721344cfe93a8",
          index: 0,
        },
        depType: "depGroup",
      },
    },
  ],
};

export function devnetClient() {
  // Spread the testnet defaults so every KnownScript codeHash is present (the
  // signer enumerates several), then override only secp256k1's devnet cellDep.
  // Scripts not deployed on this bare devnet simply resolve to no cells.
  return new ClientPublicTestnet({
    url: DEVNET_RPC,
    scripts: {
      ...TESTNET_SCRIPTS,
      [KnownScript.Secp256k1Blake160]: DEVNET_SECP256K1_SIGHASH,
    },
  });
}
