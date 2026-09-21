from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


AAVE_V4_GRAPHQL_URL = "https://api.v4.aave.com/graphql"
AAVE_V3_GRAPHQL_URL = "https://api.v3.aave.com/graphql"
AAVE_HORIZON_POOL_ADDRESS = "0xAe05Cd22df81871bc7cC2a04BeCfb516bFe332C8"
AAVE_V4_ARC_CHAIN_ID = 5042


class AaveGraphQLError(RuntimeError):
    pass


class AaveGraphQLClient:
    """Low-level AaveKit GraphQL client.

    The same client also supports Aave V3/Horizon market reads.


    This client is transport/data-layer only. It can read AaveKit data and
    prepare transaction responses returned by GraphQL, but it never signs or
    broadcasts transactions.
    """

    HORIZON_MARKET_QUERY = """
    query HorizonMarketData($request: MarketRequest!) {
      market(request: $request) {
        name
        chain { name chainId }
        address
        totalMarketSize
        totalAvailableLiquidity
        reserves {
          underlyingToken { symbol name address }
          supplyInfo {
            apy { formatted }
            canBeCollateral
          }
          borrowInfo {
            apy { formatted }
          }
        }
      }
    }
    """

    CHAINS_QUERY = """
    query Chains {
      chains(request: { query: { filter: ALL } }) {
        name
        chainId
        icon
        explorerUrl
        isTestnet
        nativeWrappedToken
      }
    }
    """



    SPOKES_QUERY = """
    query Spokes($request: SpokesRequest!) {
      spokes(request: $request) {
        id
        name
        address
        chain { chainId name icon explorerUrl isTestnet nativeWrappedToken }
        liquidationConfig {
          targetHealthFactor
          healthFactorForMaxBonus
          liquidationBonusFactor { normalized value onChainValue decimals }
        }
        summary(currency: USD) {
          totalBorrowed { value symbol decimals }
          totalBorrowCap { value symbol decimals }
          totalSupplied { value symbol decimals }
          totalSupplyCap { value symbol decimals }
          uniqueAssets
          connectedHubs
        }
        connectedHubs(currency: USD) {
          hub { id address name chain { chainId name explorerUrl } }
          summary {
            totalBorrowed { value symbol decimals }
            totalSupplied { value symbol decimals }
            creditLine { value symbol decimals }
            creditUsed { normalized value onChainValue decimals }
          }
        }
      }
    }
    """

    RESERVES_QUERY = """
    query Reserves($request: ReservesRequest!) {
      value: reserves(request: $request) {
        id
        onChainId
        chain { chainId name explorerUrl }
        spoke { id name address chain { chainId name } }
        asset {
          id
          token { chainId address name symbol decimals }
          summary {
            reservesCount
            activeReservesCount
            averageSupplyApy: supplyApy(metric: AVERAGE) { normalized value }
            averageBorrowApy: borrowApy(metric: AVERAGE) { normalized value }
          }
          price(currency: USD) { current { value symbol decimals } }
        }
        summary {
          supplied { amount { value decimals } }
          borrowed { amount { value decimals } }
          supplyApy { normalized value }
          borrowApy { normalized value }
        }
        settings {
          collateralFactor { normalized value }
          maxLiquidationBonus { normalized value }
          liquidationFee { normalized value }
          collateralRisk { normalized value }
          borrowable
          collateral
          suppliable
          receiveSharesEnabled
          latestDynamicConfigKey
          borrowCap { amount { value decimals } }
          supplyCap { amount { value decimals } }
        }
        status { frozen paused active }
        canBorrow
        canSupply
        canUseAsCollateral
        canSwapFrom
      }
    }
    """

    ASSET_QUERY = """
    query Asset($request: AssetRequest!, $currency: Currency!, $timeWindow: TimeWindow!) {
      value: asset(request: $request) {
        id
        token { chainId address name symbol decimals }
        summary {
          totalSupplyCap { amount { current { value decimals } } }
          totalSupplied { amount { current { value decimals } } }
          totalSuppliable { amount { current { value decimals } } }
          totalBorrowCap { amount { current { value decimals } } }
          totalBorrowed { amount { current { value decimals } } }
          totalBorrowable { amount { current { value decimals } } }
          reservesCount
          activeReservesCount
          averageSupplyApy: supplyApy(metric: AVERAGE) { normalized value }
          averageBorrowApy: borrowApy(metric: AVERAGE) { normalized value }
        }
        price(currency: $currency) {
          current { value symbol decimals }
          change(window: $timeWindow) { normalized value }
        }
      }
    }
    """

    MULTICHAIN_ASSET_QUERY = """
    query MultichainAsset($request: MultichainAssetRequest!) {
      value: multichainAsset(request: $request) {
        assets {
          id
          token { chainId address name symbol decimals }
        }
        summary {
          totalSupplied { value symbol decimals }
          totalBorrowed { value symbol decimals }
          totalSupplyCap { value symbol decimals }
          totalBorrowCap { value symbol decimals }
          totalAvailableLiquidity { value symbol decimals }
          utilizationRate { normalized value }
          highestSupplyApy { normalized value }
          lowestSupplyApy { normalized value }
          averageSupplyApy { normalized value }
          highestBorrowApy { normalized value }
          lowestBorrowApy { normalized value }
          averageBorrowApy { normalized value }
          chainCount
          reservesCount
          activeReservesCount
        }
      }
    }
    """


    PREVIEW_QUERY = """
    query Preview($request: PreviewRequest!, $currency: Currency!, $timeWindow: TimeWindow!) {
      value: preview(request: $request) {
        __typename
        id
        healthFactor { __typename }
        netApy { before { normalized value } after { normalized value } }
        riskPremium { before { normalized value } after { normalized value } }
        netCollateral(currency: $currency) {
          before { value symbol decimals }
          after { value symbol decimals }
        }
        netBalance(currency: $currency) {
          before { value symbol decimals }
          after { value symbol decimals }
        }
        projectedEarnings {
          before { value symbol decimals }
          after { value symbol decimals }
        }
        maxBorrowingPower {
          before { value symbol decimals }
          after { value symbol decimals }
        }
        remainingBorrowingPower {
          before { value symbol decimals }
          after { value symbol decimals }
        }
        reserveRates {
          supplyApy { before { normalized value } after { normalized value } }
          borrowApy { before { normalized value } after { normalized value } }
        }
        otherConditions {
          __typename
          ... on CollateralFactorVariation {
            reserveId
            current { normalized value }
            after { normalized value }
          }
          ... on MaxLiquidationBonusVariation {
            reserveId
            current { normalized value }
            after { normalized value }
          }
          ... on LiquidationFeeVariation {
            reserveId
            current { normalized value }
            after { normalized value }
          }
        }
      }
    }
    """

    UPDATE_POSITION_CONDITIONS_QUERY = """
    query UpdateUserPositionConditions($request: UpdateUserPositionConditionsRequest!) {
      value: updateUserPositionConditions(request: $request) {
        to
        from
        data
        value
        chainId
        operations
      }
    }
    """

    PROCESSED_TX_QUERY = """
    query HasProcessedKnownTransaction($operations: [OperationType!]!, $txHash: TxHash!) {
      value: hasProcessedKnownTransaction(
        request: { operations: $operations, txHash: $txHash }
      )
    }
    """

    def __init__(
        self,
        url: str | None = None,
        timeout: float | None = None,
        v3_url: str | None = None,
    ) -> None:
        self.url = (url or os.getenv("AAVE_V4_GRAPHQL_URL", AAVE_V4_GRAPHQL_URL)).rstrip("/")
        self.v3_url = (v3_url or os.getenv("AAVE_V3_GRAPHQL_URL", AAVE_V3_GRAPHQL_URL)).rstrip("/")
        configured_timeout = float(
            timeout if timeout is not None else os.getenv("AAVE_V4_GRAPHQL_TIMEOUT_SECONDS", "8")
        )
        self.timeout = max(1.0, configured_timeout)
        self.max_attempts = max(1, min(4, int(os.getenv("AAVE_V4_GRAPHQL_MAX_ATTEMPTS", "3"))))

    async def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._query_url(self.url, query, variables)

    async def query_v3(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._query_url(self.v3_url, query, variables)

    async def _query_url(
        self,
        url: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "variables": variables or {},
        }
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers={"content-type": "application/json", "accept": "application/json"},
                ) as client:
                    response = await client.post(url, json=payload)

                response.raise_for_status()
                body = response.json()
                if body.get("errors"):
                    raise AaveGraphQLError(str(body["errors"]))

                data = body.get("data")
                if not isinstance(data, dict):
                    raise AaveGraphQLError("AaveKit GraphQL response did not contain an object in data")
                return data
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(min(1.5, 0.2 * (attempt + 1)))

        raise AaveGraphQLError(f"AaveKit GraphQL request failed: {last_error}") from last_error

    async def horizon_market(self) -> dict[str, Any] | None:
        data = await self.query_v3(
            self.HORIZON_MARKET_QUERY,
            {
                "request": {
                    "address": AAVE_HORIZON_POOL_ADDRESS,
                    "chainId": 1,
                }
            },
        )
        value = data.get("market")
        return value if isinstance(value, dict) else None


    async def spokes(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        data = await self.query(self.SPOKES_QUERY, {"request": request})
        rows = data.get("spokes", [])
        if not isinstance(rows, list):
            raise AaveGraphQLError("AaveKit spokes response was not a list")
        return [row for row in rows if isinstance(row, dict)]

    async def reserves(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        data = await self.query(self.RESERVES_QUERY, {"request": request})
        rows = data.get("value", [])
        if not isinstance(rows, list):
            raise AaveGraphQLError("AaveKit reserves response was not a list")
        return [row for row in rows if isinstance(row, dict)]

    async def asset(
        self,
        request: dict[str, Any],
        *,
        currency: str = "USD",
        time_window: str = "LAST_DAY",
    ) -> dict[str, Any] | None:
        data = await self.query(
            self.ASSET_QUERY,
            {"request": request, "currency": currency, "timeWindow": time_window},
        )
        value = data.get("value")
        return value if isinstance(value, dict) else None

    async def multichain_asset(self, request: dict[str, Any]) -> dict[str, Any] | None:
        data = await self.query(self.MULTICHAIN_ASSET_QUERY, {"request": request})
        value = data.get("value")
        return value if isinstance(value, dict) else None


    async def preview(
        self,
        action: dict[str, Any],
        *,
        currency: str = "USD",
        time_window: str = "LAST_WEEK",
    ) -> dict[str, Any] | None:
        if not isinstance(action, dict) or len(action) != 1:
            raise ValueError("preview action must contain exactly one action key")
        if "updateUserPositionConditions" in action:
            request = action["updateUserPositionConditions"]
            if not isinstance(request, dict):
                raise ValueError("updateUserPositionConditions must be an object")
            if request.get("update") not in {"ALL_DYNAMIC_CONFIG", "JUST_RISK_PREMIUM"}:
                raise ValueError("update must be ALL_DYNAMIC_CONFIG or JUST_RISK_PREMIUM")
            if not str(request.get("userPositionId", "")).strip():
                raise ValueError("userPositionId is required")
        data = await self.query(
            self.PREVIEW_QUERY,
            {
                "request": {"action": action},
                "currency": currency,
                "timeWindow": time_window,
            },
        )
        value = data.get("value")
        return value if isinstance(value, dict) else None

    async def update_user_position_conditions_tx(
        self,
        *,
        user_position_id: str,
        update: str = "ALL_DYNAMIC_CONFIG",
    ) -> dict[str, Any] | None:
        normalized = str(update).strip().upper()
        if normalized not in {"ALL_DYNAMIC_CONFIG", "JUST_RISK_PREMIUM"}:
            raise ValueError("update must be ALL_DYNAMIC_CONFIG or JUST_RISK_PREMIUM")
        if not str(user_position_id).strip():
            raise ValueError("user_position_id is required")
        data = await self.query(
            self.UPDATE_POSITION_CONDITIONS_QUERY,
            {
                "request": {
                    "userPositionId": str(user_position_id),
                    "update": normalized,
                }
            },
        )
        value = data.get("value")
        return value if isinstance(value, dict) else None

    async def chains(self) -> list[dict[str, Any]]:
        data = await self.query(self.CHAINS_QUERY)
        rows = data.get("chains", [])
        if not isinstance(rows, list):
            raise AaveGraphQLError("AaveKit chains response was not a list")
        return [row for row in rows if isinstance(row, dict)]

    async def arc_chain(self) -> dict[str, Any] | None:
        rows = await self.chains()
        for row in rows:
            try:
                if int(row.get("chainId")) == AAVE_V4_ARC_CHAIN_ID:
                    return row
            except (TypeError, ValueError):
                continue
        return None

    async def has_processed_known_transaction(
        self,
        *,
        operations: list[str],
        tx_hash: str,
    ) -> bool:
        if not operations:
            raise ValueError("operations must contain at least one OperationType")
        if not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
            raise ValueError("tx_hash must be a hex transaction hash")
        data = await self.query(
            self.PROCESSED_TX_QUERY,
            {"operations": operations, "txHash": tx_hash},
        )
        return bool(data.get("value", False))

    async def transaction_query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a caller-supplied AaveKit transaction-preparation query.

        The returned payload is intentionally not interpreted as a signed
        transaction. The caller must pass the resulting TransactionRequest or
        ExecutionPlan to an explicit wallet integration.
        """
        return await self.query(query, variables)
