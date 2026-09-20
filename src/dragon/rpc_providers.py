"""Optional private RPC provider support (Alchemy, Infura, QuickNode).

Each provider key is opt-in through an environment variable. When a key is
present, the module builds authenticated per-chain endpoint URLs and merges them
ahead of the public fallback, so the RpcPool prefers private endpoints and drops
to public ones only during failures.

Provider key environment variables
----------------------------------
ALCHEMY_API_KEY      Alchemy dashboard key (also accepts ALCHEMY_KEY)
INFURA_API_KEY       Infura project id (also accepts INFURA_PROJECT_ID)
QUICKNODE_ENDPOINT   QuickNode endpoint host, e.g. ``base-mainnet`` plus the
QUICKNODE_API_KEY    token, or a full ``QUICKNODE_<CHAIN>_URL`` override.

Per-chain overrides always win over provider templates:
  <CHAIN>_RPC_URL / <CHAIN>_RPC_URLS for a specific chain (see chains.py),
  or ``ALCHEMY_<CHAIN>_URL`` / ``INFURA_<CHAIN>_URL`` / ``QUICKNODE_<CHAIN>_URL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Alchemy network slugs per chain id. Alchemy names testnets/product variants
# explicitly; these are the mainnet slugs verified against the dashboard naming.
ALCHEMY_NETWORKS: dict[int, str] = {
    1: "eth-mainnet",
    10: "opt-mainnet",
    56: "bnb-mainnet",
    137: "polygon-mainnet",
    324: "zksync-mainnet",
    42161: "arb-mainnet",
    43114: "avax-mainnet",
    8453: "base-mainnet",
    59144: "linea-mainnet",
    534352: "scroll-mainnet",
    81457: "blast-mainnet",
}

# Infura's network path is appended to https://<network>.infura.io/v3/<key>.
INFURA_NETWORKS: dict[int, str] = {
    1: "mainnet",
    10: "optimism-mainnet",
    56: "bsc-mainnet",
    137: "polygon-mainnet",
    324: "zksync-mainnet",
    42161: "arbitrum-mainnet",
    43114: "avalanche-mainnet",
    8453: "base-mainnet",
    59144: "linea-mainnet",
    534352: "scroll-mainnet",
    81457: "blast-mainnet",
}

# QuickNode's default endpoint slugs per chain, used with QUICKNODE_ENDPOINT.
QUICKNODE_ENDPOINTS: dict[int, str] = {
    1: "ethereum-mainnet",
    10: "optimism-mainnet",
    56: "bsc-mainnet",
    137: "polygon-mainnet",
    324: "zksync-mainnet",
    42161: "arbitrum-mainnet",
    43114: "avalanche-mainnet",
    8453: "base-mainnet",
    59144: "linea-mainnet",
    534352: "scroll-mainnet",
    81457: "blast-mainnet",
}

# Human-readable chain token used in per-chain override env var names.
_CHAIN_ENV: dict[int, str] = {
    1: "ETHEREUM",
    10: "OPTIMISM",
    56: "BNB",
    137: "POLYGON",
    324: "ZKSYNC",
    42161: "ARBITRUM",
    43114: "AVALANCHE",
    8453: "BASE",
    59144: "LINEA",
    534352: "SCROLL",
    81457: "BLAST",
    480: "WORLDCHAIN",
    5000: "MANTLE",
    130: "UNICHAIN",
    42220: "CELO",
}


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class Providers:
    alchemy_key: str
    infura_key: str
    quicknode_key: str
    quicknode_endpoint: str

    @property
    def any_enabled(self) -> bool:
        return bool(self.alchemy_key or self.infura_key or (self.quicknode_key and self.quicknode_endpoint))


def load_providers() -> Providers:
    return Providers(
        alchemy_key=_first_env("ALCHEMY_API_KEY", "ALCHEMY_KEY"),
        infura_key=_first_env("INFURA_API_KEY", "INFURA_PROJECT_ID"),
        quicknode_key=_first_env("QUICKNODE_API_KEY", "QUICKNODE_TOKEN"),
        quicknode_endpoint=_first_env("QUICKNODE_ENDPOINT", "QUICKNODE_HOST"),
    )


def _override_url(provider: str, chain_id: int) -> str:
    token = _CHAIN_ENV.get(chain_id)
    if not token:
        return ""
    return _first_env(f"{provider}_{token}_URL")


def alchemy_urls(chain_id: int, key: str) -> list[str]:
    if not key:
        return []
    override = _override_url("ALCHEMY", chain_id)
    if override:
        return [override]
    network = ALCHEMY_NETWORKS.get(chain_id)
    if not network:
        return []
    return [f"https://{network}.g.alchemy.com/v2/{key}"]


def infura_urls(chain_id: int, key: str) -> list[str]:
    if not key:
        return []
    override = _override_url("INFURA", chain_id)
    if override:
        return [override]
    network = INFURA_NETWORKS.get(chain_id)
    if not network:
        return []
    return [f"https://{network}.infura.io/v3/{key}"]


def quicknode_urls(chain_id: int, key: str, endpoint: str) -> list[str]:
    if not key:
        return []
    override = _override_url("QUICKNODE", chain_id)
    if override:
        return [override]
    # A single QUICKNODE_ENDPOINT host serves exactly one chain, so it is only
    # used for QUICKNODE_CHAIN_ID. Other chains need QUICKNODE_<CHAIN>_URL.
    allowed_raw = _first_env("QUICKNODE_CHAIN_ID", "QUICKNODE_CHAIN")
    if not allowed_raw:
        return []
    try:
        allowed = int(allowed_raw)
    except ValueError:
        return []
    if allowed != chain_id:
        return []
    slug = endpoint or QUICKNODE_ENDPOINTS.get(chain_id, "")
    if not slug:
        return []
    return [f"https://{slug}.quiknode.pro/{key}/"]


def private_urls(chain_id: int, providers: Providers) -> list[str]:
    """Authenticated provider URLs for one chain, in preference order."""
    urls: list[str] = []
    for url in [
        *alchemy_urls(chain_id, providers.alchemy_key),
        *infura_urls(chain_id, providers.infura_key),
        *quicknode_urls(chain_id, providers.quicknode_key, providers.quicknode_endpoint),
    ]:
        if url and url not in urls:
            urls.append(url)
    return urls


def provider_status(providers: Providers) -> dict[str, bool]:
    return {
        "alchemy": bool(providers.alchemy_key),
        "infura": bool(providers.infura_key),
        "quicknode": bool(providers.quicknode_key and providers.quicknode_endpoint),
    }
