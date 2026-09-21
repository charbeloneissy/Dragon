import asyncio

import pytest

from src.dragon.aave_mcp import AaveMCPClient, _unwrap_result


def test_aave_mcp_preserves_warnings_and_next_actions():
    payload = {
        "structuredContent": {
            "data": {"healthFactor": {"before": "2", "after": "1.5"}},
            "warnings": [{"level": "warning", "code": "HF_DROP", "message": "Health factor decreases"}],
            "next_actions": ["get_user_summary"],
            "chainsCovered": [1],
        }
    }
    result = _unwrap_result(payload)
    assert result["healthFactor"]["after"] == "1.5"
    assert result["warnings"][0]["code"] == "HF_DROP"
    assert result["next_actions"] == ["get_user_summary"]
    assert result["chainsCovered"] == [1]
    assert "_aave_envelope" in result


class InventoryClient(AaveMCPClient):
    async def _rpc_request(self, method, params):
        assert method == "tools/list"
        return {
            "tools": [
                {"name": "get_markets", "description": "markets"},
                {"name": "preview_action", "description": "preview"},
            ]
        }


@pytest.mark.asyncio
async def test_live_tool_inventory_is_authoritative():
    client = InventoryClient()
    inventory = await client.list_tools(refresh=True)
    assert "get_markets" in inventory
    tool = await client.ensure_tool("preview_action")
    assert tool["name"] == "preview_action"
    with pytest.raises(Exception):
        await client.ensure_tool("missing_tool")
