from dataclasses import replace

import pytest

from ckbbench.run.chain_profile import (
    CHAIN_PROFILE_SCHEMA_VERSION,
    ChainProfile,
    ChainProfileError,
    LOCAL_HERMETIC_PROFILE,
)


def _testnet() -> ChainProfile:
    return ChainProfile(
        profile_id="ckb-testnet-pudge-v1",
        chain_track="testnet",
        chain_id="ckb_testnet",
        genesis_hash="0x" + "1" * 64,
    )


def test_chain_profiles_round_trip_with_stable_identity():
    testnet = _testnet()
    assert ChainProfile.from_dict(testnet.to_dict()) == testnet
    assert ChainProfile.from_dict(LOCAL_HERMETIC_PROFILE.to_dict()) == LOCAL_HERMETIC_PROFILE
    assert testnet.schema_version == CHAIN_PROFILE_SCHEMA_VERSION
    assert len(testnet.sha256) == 64
    assert testnet.sha256 != LOCAL_HERMETIC_PROFILE.sha256


@pytest.mark.parametrize(
    "change",
    [
        {"chain_track": "devnet"},
        {"chain_id": None},
        {"genesis_hash": None},
        {"genesis_hash": "1" * 64},
        {"genesis_hash": "0x" + "0" * 64},
        {"schema_version": "ckbbench-chain-profile-v2"},
    ],
)
def test_testnet_profile_refuses_identity_and_schema_drift(change: dict):
    with pytest.raises(ChainProfileError):
        replace(_testnet(), **change)


def test_local_profile_refuses_public_chain_identity():
    with pytest.raises(ChainProfileError, match="cannot carry"):
        replace(LOCAL_HERMETIC_PROFILE, chain_id="ckb_testnet")


def test_chain_profile_refuses_unknown_fields():
    document = _testnet().to_dict()
    document["rpc_url"] = "https://example.invalid"
    with pytest.raises(ChainProfileError, match="exactly"):
        ChainProfile.from_dict(document)
