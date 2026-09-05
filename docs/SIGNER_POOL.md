# TestNet Signer Pools

Campaign signing uses an owner-private JSON file outside the repository. The harness never generates,
funds or reuses a TestNet identity implicitly. An operator prepares the pool before a campaign and
passes it explicitly with `--signer-pool`.

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

## Preparation

1. Freeze the campaign and inspect its slots with `./bench campaign plan`.
2. Generate a fresh TestNet identity for every signed slot and retry ordinal using a trusted local
   CKB wallet or key tool.
3. Fund each address on the campaign's exact TestNet. Wait for the Task contract's required
   confirmations, then record dedicated unspent input cells. Do not allocate those cells elsewhere.
4. Write the pool outside the repository, set mode `0600`, and keep a private backup until the
   campaign and any retry are complete.
5. Validate the file offline against the frozen campaign before authorizing execution.

```bash
chmod 0600 /absolute/private/path/signer-pool.json

./bench campaign validate-signer-pool \
  --manifest campaign.json \
  --signer-pool /absolute/private/path/signer-pool.json \
  --repository-root . \
  "${RELEASE_ARGS[@]}"
```

Validation prints only the chain profile and entry count. It never prints, hashes for display, or
copies private keys. Live preflight later derives the address from each key again, checks direct RPC
chain identity, confirms every leased cell is unspent and sufficiently confirmed, and verifies the
declared funding floor before the first provider generation.

The public TestNet RPC default is `https://testnet.ckb.dev/rpc`. Set `CKBBENCH_TESTNET_RPC` when an
accepted campaign uses a different trusted endpoint, and make sure its chain identity matches the
frozen chain profile.
