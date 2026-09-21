from src.dragon.aave_agent import AaveAgentPolicy


def test_error_warning_blocks_agent():
    policy = AaveAgentPolicy()
    result = policy.guard(
        phase="build",
        result={"warnings": [{"level": "error", "code": "BAD_ACTION"}]},
    )
    assert result.allowed is False
    assert result.reason == "error_warning:BAD_ACTION"


def test_uncovered_chain_blocks_zero_assumption():
    policy = AaveAgentPolicy()
    result = policy.guard(
        phase="inspect",
        result={"data": []},
        chains_covered=[],
        chains_not_covered=[42161],
    )
    assert result.allowed is False
    assert result.reason == "required_chain_not_covered"


def test_uncovered_chain_from_aave_response_is_detected():
    policy = AaveAgentPolicy()
    result = policy.guard(
        phase="inspect",
        result={
            "data": [],
            "chainsCovered": [1],
            "chainsNotCovered": [42161],
            "chainsNotServed": [],
        },
    )
    assert result.allowed is False
    assert result.reason == "required_chain_not_covered"


def test_discovery_requires_coverage_metadata():
    policy = AaveAgentPolicy()
    result = policy.guard(
        phase="discover",
        result={"data": []},
    )
    assert result.allowed is False
    assert result.reason == "no_coverage_reported"


def test_simulation_required_before_buildable_action():
    policy = AaveAgentPolicy()
    result = policy.guard(
        phase="borrow",
        result={"data": {}},
    )
    assert result.allowed is False
    assert result.reason == "simulation_required"

    simulated = policy.mark_simulation({"data": {}, "healthFactorAfter": "2.1"})
    result = policy.guard(phase="borrow", result=simulated)
    assert result.allowed is True


def test_transaction_request_is_unsigned_wallet_action():
    policy = AaveAgentPolicy()
    summary = policy.execution_plan_summary({
        "__typename": "TransactionRequest",
        "to": "0x0000000000000000000000000000000000000001",
        "from": "0x0000000000000000000000000000000000000002",
        "chainId": 1,
        "operation": "SUPPLY",
    })
    assert summary["ready"] is True
    assert summary["requires_wallet_signature"] is True
