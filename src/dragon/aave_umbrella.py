from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from web3 import Web3


ETHEREUM_CHAIN_ID = 1

# Ethereum Umbrella deployment from Aave's current interface deployment configuration.
UMBRELLA_ETHEREUM = {
    "umbrella": "0xD400fc38ED4732893174325693a63C30ee3881a8",
    "stake_token_impl": "0x75e8aC0c063B6966E2A9954adEdf39BdE9370197",
    "rewards_controller": "0x4655Ce3D625a63d30bA704087E52B4C31E38188B",
    "batch_helper": "0xCe6Ced23118EDEb23054E06118a702797b13fc2F",
    "config_engine": "0x3f3EfAeba02bbA78BA7E89Dc6Ec503C8fe5fd5a4",
    "stake_data_provider": "0x6321ba6b41fbddb6b678cd80db067f20a8770879",
    "source": "aave/interface + aave-dao/aave-umbrella",
}

ERC20_ABI = [
    {"inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

ERC4626_ABI = ERC20_ABI + [
    {"inputs": [], "name": "asset", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalAssets", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupply", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "assets", "type": "uint256"}], "name": "previewDeposit", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "shares", "type": "uint256"}], "name": "convertToAssets", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}], "name": "maxDeposit", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}], "name": "maxWithdraw", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "assets", "type": "uint256"}, {"name": "receiver", "type": "address"}], "name": "deposit", "outputs": [{"type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "shares", "type": "uint256"}, {"name": "receiver", "type": "address"}, {"name": "owner", "type": "address"}], "name": "redeem", "outputs": [{"type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "assets", "type": "uint256"}, {"name": "receiver", "type": "address"}, {"name": "owner", "type": "address"}], "name": "withdraw", "outputs": [{"type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "paused", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getCooldown", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getUnstakeWindow", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "user", "type": "address"}], "name": "getStakerCooldown", "outputs": [{"components": [{"name": "amount", "type": "uint192"}, {"name": "endOfCooldown", "type": "uint32"}, {"name": "withdrawalWindow", "type": "uint32"}], "type": "tuple"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getMaxSlashableAssets", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "MIN_ASSETS_REMAINING", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "cooldown", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

REWARDS_ABI = [
    {"inputs": [], "name": "getAllAssets", "outputs": [{"type": "address[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getAllRewards", "outputs": [{"type": "address[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "user", "type": "address"}], "name": "calculateCurrentUserRewards", "outputs": [{"type": "address[]"}, {"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "reward", "type": "address"}], "name": "getRewardData", "outputs": [{"components": [{"name": "addr", "type": "address"}, {"name": "index", "type": "uint256"}, {"name": "maxEmissionPerSecond", "type": "uint256"}, {"name": "distributionEnd", "type": "uint256"}], "type": "tuple"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getAssetData", "outputs": [{"components": [{"name": "targetLiquidity", "type": "uint256"}, {"name": "lastUpdateTimestamp", "type": "uint256"}], "type": "tuple"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "assets", "type": "address[]"}, {"name": "receiver", "type": "address"}], "name": "claimAllRewards", "outputs": [{"name": "claimed", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"},
]

UMBRELLA_ABI = [
    {"inputs": [], "name": "getStkTokens", "outputs": [{"type": "address[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "stakeToken", "type": "address"}], "name": "isUmbrellaStkToken", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
]


@dataclass(frozen=True)
class UmbrellaStakeSnapshot:
    stake_token: str
    underlying: str
    user: str
    user_shares: int
    redeemable_assets: int
    total_assets: int
    total_shares: int
    max_deposit: int
    max_withdraw: int
    target_liquidity: int
    rewards: tuple[dict[str, Any], ...]
    cooldown_seconds: int
    withdrawal_window_seconds: int
    cooldown_amount_shares: int
    cooldown_end: int
    max_slashable_assets: int
    min_assets_remaining: int
    paused: bool

    @property
    def slashing_exposure(self) -> bool:
        return not self.paused

    def as_dict(self) -> dict[str, Any]:
        return {
            "stake_token": self.stake_token,
            "underlying": self.underlying,
            "user_shares": str(self.user_shares),
            "redeemable_assets": str(self.redeemable_assets),
            "total_assets": str(self.total_assets),
            "total_shares": str(self.total_shares),
            "max_deposit": str(self.max_deposit),
            "max_withdraw": str(self.max_withdraw),
            "target_liquidity": str(self.target_liquidity),
            "rewards": list(self.rewards),
            "cooldown_seconds": self.cooldown_seconds,
            "withdrawal_window_seconds": self.withdrawal_window_seconds,
            "cooldown_amount_shares": str(self.cooldown_amount_shares),
            "cooldown_end": self.cooldown_end,
            "max_slashable_assets": str(self.max_slashable_assets),
            "min_assets_remaining": str(self.min_assets_remaining),
            "paused": self.paused,
            "slashing_exposure": self.slashing_exposure,
        }


class AaveUmbrellaClient:
    """Read/prepare client for Aave Umbrella on Ethereum.

    It never signs or broadcasts. The live stake-token set is obtained from
    Umbrella itself; no individual StakeToken address is hard-coded.
    """

    def __init__(self, w3: Web3 | None = None, rpc_url: str | None = None):
        if w3 is not None:
            self.w3 = w3
        else:
            url = rpc_url or os.getenv("AAVE_UMBRELLA_RPC_URL") or os.getenv("ETHEREUM_RPC_URL") or os.getenv("CHAIN_RPC_1")
            if not url:
                raise ValueError("An Ethereum RPC URL is required for Umbrella")
            self.w3 = Web3(Web3.HTTPProvider(url))

    def _contract(self, address: str, abi: list[dict[str, Any]]):
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)

    def stake_tokens(self) -> list[str]:
        return list(self._contract(UMBRELLA_ETHEREUM["umbrella"], UMBRELLA_ABI).functions.getStkTokens().call())

    def snapshot(self, user: str) -> list[UmbrellaStakeSnapshot]:
        user = Web3.to_checksum_address(user)
        rewards_controller = self._contract(UMBRELLA_ETHEREUM["rewards_controller"], REWARDS_ABI)
        output: list[UmbrellaStakeSnapshot] = []

        for stake_token in self.stake_tokens():
            stake = self._contract(stake_token, ERC4626_ABI)
            underlying = Web3.to_checksum_address(stake.functions.asset().call())
            shares = int(stake.functions.balanceOf(user).call())
            total_assets = int(stake.functions.totalAssets().call())
            total_shares = int(stake.functions.totalSupply().call())
            redeemable = int(stake.functions.convertToAssets(shares).call())
            max_deposit = int(stake.functions.maxDeposit(user).call())
            max_withdraw = int(stake.functions.maxWithdraw(user).call())
            paused = bool(stake.functions.paused().call())
            cooldown = int(stake.functions.getCooldown().call())
            window = int(stake.functions.getUnstakeWindow().call())
            cooldown_data = stake.functions.getStakerCooldown(user).call()
            target_liquidity = int(rewards_controller.functions.getAssetData(stake_token).call()[0])

            reward_addresses = list(rewards_controller.functions.getAllRewards(stake_token).call())
            _, accrued = rewards_controller.functions.calculateCurrentUserRewards(stake_token, user).call()
            rewards: list[dict[str, Any]] = []
            for idx, reward in enumerate(reward_addresses):
                data = rewards_controller.functions.getRewardData(stake_token, reward).call()
                rewards.append({
                    "reward": Web3.to_checksum_address(reward),
                    "accrued": str(int(accrued[idx])) if idx < len(accrued) else "0",
                    "max_emission_per_second": str(int(data[2])),
                    "distribution_end": int(data[3]),
                })

            output.append(
                UmbrellaStakeSnapshot(
                    stake_token=Web3.to_checksum_address(stake_token),
                    underlying=underlying,
                    user=user,
                    user_shares=shares,
                    redeemable_assets=redeemable,
                    total_assets=total_assets,
                    total_shares=total_shares,
                    max_deposit=max_deposit,
                    max_withdraw=max_withdraw,
                    target_liquidity=target_liquidity,
                    rewards=tuple(rewards),
                    cooldown_seconds=cooldown,
                    withdrawal_window_seconds=window,
                    cooldown_amount_shares=int(cooldown_data[0]),
                    cooldown_end=int(cooldown_data[1]),
                    max_slashable_assets=int(stake.functions.getMaxSlashableAssets().call()),
                    min_assets_remaining=int(stake.functions.MIN_ASSETS_REMAINING().call()),
                    paused=paused,
                )
            )
        return output

    def build_deposit(self, stake_token: str, owner: str, amount: int, receiver: str | None = None) -> list[dict[str, Any]]:
        if amount <= 0:
            raise ValueError("Umbrella deposit amount must be positive")
        receiver = receiver or owner
        stake = self._contract(stake_token, ERC4626_ABI)
        underlying = Web3.to_checksum_address(stake.functions.asset().call())
        approval = self._contract(underlying, ERC20_ABI).functions.approve(
            Web3.to_checksum_address(stake_token), int(amount)
        ).build_transaction({"from": Web3.to_checksum_address(owner), "chainId": ETHEREUM_CHAIN_ID})
        deposit = stake.functions.deposit(
            int(amount), Web3.to_checksum_address(receiver)
        ).build_transaction({"from": Web3.to_checksum_address(owner), "chainId": ETHEREUM_CHAIN_ID})
        return [approval, deposit]

    def build_cooldown(self, stake_token: str, owner: str) -> dict[str, Any]:
        return self._contract(stake_token, ERC4626_ABI).functions.cooldown().build_transaction(
            {"from": Web3.to_checksum_address(owner), "chainId": ETHEREUM_CHAIN_ID}
        )

    def build_redeem(self, stake_token: str, owner: str, shares: int, receiver: str | None = None) -> dict[str, Any]:
        if shares <= 0:
            raise ValueError("Umbrella redeem shares must be positive")
        receiver = receiver or owner
        return self._contract(stake_token, ERC4626_ABI).functions.redeem(
            int(shares), Web3.to_checksum_address(receiver), Web3.to_checksum_address(owner)
        ).build_transaction({"from": Web3.to_checksum_address(owner), "chainId": ETHEREUM_CHAIN_ID})

    def build_withdraw(self, stake_token: str, owner: str, assets: int, receiver: str | None = None) -> dict[str, Any]:
        if assets <= 0:
            raise ValueError("Umbrella withdraw amount must be positive")
        receiver = receiver or owner
        return self._contract(stake_token, ERC4626_ABI).functions.withdraw(
            int(assets), Web3.to_checksum_address(receiver), Web3.to_checksum_address(owner)
        ).build_transaction({"from": Web3.to_checksum_address(owner), "chainId": ETHEREUM_CHAIN_ID})

    def build_claim_all_rewards(self, stake_token: str, owner: str, receiver: str | None = None) -> dict[str, Any]:
        receiver = receiver or owner
        return self._contract(
            UMBRELLA_ETHEREUM["rewards_controller"], REWARDS_ABI
        ).functions.claimAllRewards(
            [Web3.to_checksum_address(stake_token)], Web3.to_checksum_address(receiver)
        ).build_transaction({"from": Web3.to_checksum_address(owner), "chainId": ETHEREUM_CHAIN_ID})


def choose_umbrella_candidates(snapshots: list[UmbrellaStakeSnapshot]) -> list[UmbrellaStakeSnapshot]:
    """Return stake positions that are currently enterable without implying safety.

    Umbrella staking carries slashing risk. The ranking therefore does not
    treat a higher reward emission as a free yield advantage.
    """
    eligible = [
        item for item in snapshots
        if not item.paused and item.max_deposit > 0
    ]
    return sorted(
        eligible,
        key=lambda item: (
            Decimal(item.target_liquidity) if item.target_liquidity else Decimal("0"),
            Decimal(item.max_slashable_assets),
        ),
        reverse=True,
    )
