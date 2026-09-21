from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


AAVE_V4_GRAPHQL_URL = "https://api.v4.aave.com/graphql"
AAVE_V4_ARC_CHAIN_ID = 5042


class AaveGraphQLError(RuntimeError):
    pass


class AaveGraphQLClient:
    """Low-level AaveKit v4 GraphQL client.

    This client is transport/data-layer only. It can read AaveKit data and
    prepare transaction responses returned by GraphQL, but it never signs or
    broadcasts transactions.
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
    ) -> None:
        self.url = (url or os.getenv("AAVE_V4_GRAPHQL_URL", AAVE_V4_GRAPHQL_URL)).rstrip("/")
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
                    response = await client.post(self.url, json=payload)

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
