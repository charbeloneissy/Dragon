from src.dragon.aave_umbrella import UmbrellaStakeSnapshot, choose_umbrella_candidates


def make_snapshot(stake_token: str, *, paused: bool = False, max_deposit: int = 100) -> UmbrellaStakeSnapshot:
    return UmbrellaStakeSnapshot(
        stake_token=stake_token,
        underlying="0x0000000000000000000000000000000000000001",
        user="0x0000000000000000000000000000000000000002",
        user_shares=0,
        redeemable_assets=0,
        total_assets=1_000_000,
        total_shares=1_000_000,
        max_deposit=max_deposit,
        max_withdraw=0,
        target_liquidity=500_000,
        rewards=(
            {
                "reward": "0x0000000000000000000000000000000000000003",
                "accrued": "0",
                "max_emission_per_second": "100",
                "distribution_end": 4102444800,
            },
        ),
        cooldown_seconds=20 * 24 * 3600,
        withdrawal_window_seconds=2 * 24 * 3600,
        cooldown_amount_shares=0,
        cooldown_end=0,
        max_slashable_assets=10_000,
        min_assets_remaining=1,
        paused=paused,
    )


def test_umbrella_candidates_exclude_paused_or_full_staketokens():
    rows = [
        make_snapshot("0x0000000000000000000000000000000000000010"),
        make_snapshot("0x0000000000000000000000000000000000000011", paused=True),
        make_snapshot("0x0000000000000000000000000000000000000012", max_deposit=0),
    ]
    candidates = choose_umbrella_candidates(rows)
    assert [x.stake_token for x in candidates] == [
        "0x0000000000000000000000000000000000000010"
    ]


def test_umbrella_snapshot_exposes_slashing_and_cooldown():
    row = make_snapshot("0x0000000000000000000000000000000000000010")
    data = row.as_dict()
    assert data["slashing_exposure"] is True
    assert data["cooldown_seconds"] == 20 * 24 * 3600
    assert data["withdrawal_window_seconds"] == 2 * 24 * 3600
    assert data["max_slashable_assets"] == "10000"
