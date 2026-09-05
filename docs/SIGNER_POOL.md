# TestNet Signer Pools

Campaign signing uses an owner-private JSON file outside the repository. The campaign provisioner
derives the exact required leases from the frozen manifest, creates fresh identities, funds them on
the manifest's TestNet, and validates the resulting pool before execution. Provisioning is an
explicit authorized operation and never happens inside a scored Task attempt.

The machine-readable field contract is [signer-pool.schema.json](signer-pool.schema.json). Runtime
validation is stricter than JSON Schema: it also binds the file to the frozen campaign and release.

## Required leases

Every signed campaign slot needs two entries, one for the original attempt and one for its only
permitted whole-Task retry. Each entry has its own:

- private key, public address and matching lock script;
- signer handle and lease identifier; and
- confirmed, unspent TestNet input cells.

No key, address, lease or input may appear twice. For each matched B/C trial and retry ordinal, the
leased input capacity lists must be identical. This keeps available capital from becoming a treatment
difference.

## Automated preparation

The high-level start command qualifies the model, freezes the campaign, provisions every required
signer lease, and then executes the frozen batches. The generated campaign ID is printed once its
manifest exists. Reuse that ID to resume retained progress after an infrastructure pause.

```bash
./bench campaign start \
  --profile gpt-5.6-luna \
  --trials-per-task 2 \
  --authorized-by-user

./bench campaign start \
  --campaign campaign-00000000000000000000000000000000 \
  --authorized-by-user
```

For a frozen campaign created through the granular workflow, provisioning can be run separately:

```bash
CKBBENCH_DOCKER=1 ./bench campaign provision-signers \
  --campaign campaign-00000000000000000000000000000000 \
  --authorized-by-user
```

The provisioner uses one faucet-funded donor for each bounded group of leases, then creates exact
single-assignment outputs in a signed split transaction. It persists enough private state to resume
the same funding or transaction after interruption without generating replacement identities or
silently requesting funds twice. Direct RPC and CKB AI must report the frozen TestNet identity
before any funding request. Every resulting cell must be live, committed, sufficiently confirmed,
plain capacity, and locked to its generated key.

Private keys and working state default to
`~/.local/share/ckb-ai-bench/<campaign-id>/`. Set `CKBBENCH_PRIVATE_DATA_ROOT` or pass
`--private-data-root` to use another absolute directory outside the repository. The directory is
mode `0700`; private JSON files are mode `0600`. A public receipt without keys is written beside the
campaign manifest. It records the leased capacity, split transaction IDs and provisioning fees
separately from model performance.

## Manual pools

A separately prepared pool remains supported. Generate and fund a fresh TestNet identity for every
signed slot and retry ordinal, wait for the Task-required confirmations, record dedicated unspent
cells, and validate the mode-`0600` file before execution:

```bash
chmod 0600 /absolute/private/path/signer-pool.json

./bench campaign validate-signer-pool \
  --campaign campaign-00000000000000000000000000000000 \
  --signer-pool /absolute/private/path/signer-pool.json \
  --repository-root .
```

Validation prints only the chain profile and entry count. It never prints, hashes for display, or
copies private keys. Live preflight later derives the address from each key again, checks direct RPC
chain identity, confirms every leased cell is unspent and sufficiently confirmed, and verifies the
declared funding floor before the first provider generation.

The public TestNet RPC default is `https://testnet.ckb.dev/rpc`. Set `CKBBENCH_TESTNET_RPC` when a
campaign uses a different trusted endpoint. Its chain identity must still match the frozen profile.
