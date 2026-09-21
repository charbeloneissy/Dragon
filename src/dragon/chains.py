"""Multi-chain registry for the Dragon cross-DEX arbitrage engine.

Each entry declares everything the adapters need to talk to a chain: its chain
id, the native gas token, the default wrapped-native address, the wrapped-native
decimals, and which environment variables hold RPC endpoints.

Only chains with a verified on-chain DEX venue are listed as executable.
"""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ChainSpec:
    chain_id: int
    name: str
    family: str
    native_symbol: str
    wrapped_native: str
    wrapped_native_decimals: int
    explorer: str
    rpc_env: str
    rpc_fallback_env: str
    default_rpc: str = ""


EVM_CHAINS: dict[int, ChainSpec] = {
    1: ChainSpec(
        chain_id=1,
        name="ethereum",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        wrapped_native_decimals=18,
        explorer="https://etherscan.io",
        rpc_env="ETHEREUM_RPC_URL",
        rpc_fallback_env="ETHEREUM_RPC_URLS",
        default_rpc="https://ethereum-rpc.publicnode.com",
    ),
    56: ChainSpec(
        chain_id=56,
        name="bnb",
        family="evm",
        native_symbol="BNB",
        wrapped_native="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        wrapped_native_decimals=18,
        explorer="https://bscscan.com",
        rpc_env="BNB_RPC_URL",
        rpc_fallback_env="BNB_RPC_URLS",
        default_rpc="https://bsc-rpc.publicnode.com",
    ),
    43114: ChainSpec(
        chain_id=43114,
        name="avalanche",
        family="evm",
        native_symbol="AVAX",
        wrapped_native="0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
        wrapped_native_decimals=18,
        explorer="https://snowtrace.io",
        rpc_env="AVALANCHE_RPC_URL",
        rpc_fallback_env="AVALANCHE_RPC_URLS",
        default_rpc="https://avalanche-c-chain-rpc.publicnode.com",
    ),
    84532: ChainSpec(
        chain_id=84532,
        name="base-sepolia",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        wrapped_native_decimals=18,
        explorer="https://sepolia.basescan.org",
        rpc_env="BASE_SEPOLIA_RPC_URL",
        rpc_fallback_env="BASE_SEPOLIA_RPC_URLS",
        default_rpc="https://sepolia.base.org",
    ),
    8453: ChainSpec(
        chain_id=8453,
        name="base",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        wrapped_native_decimals=18,
        explorer="https://basescan.org",
        rpc_env="DEX_RPC_URL",
        rpc_fallback_env="DEX_RPC_URLS",
        default_rpc="https://base-rpc.publicnode.com",
    ),
    42161: ChainSpec(
        chain_id=42161,
        name="arbitrum",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        wrapped_native_decimals=18,
        explorer="https://arbiscan.io",
        rpc_env="ARBITRUM_RPC_URL",
        rpc_fallback_env="ARBITRUM_RPC_URLS",
        default_rpc="https://arbitrum-one-rpc.publicnode.com",
    ),
    10: ChainSpec(
        chain_id=10,
        name="optimism",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        wrapped_native_decimals=18,
        explorer="https://optimistic.etherscan.io",
        rpc_env="OPTIMISM_RPC_URL",
        rpc_fallback_env="OPTIMISM_RPC_URLS",
        default_rpc="https://optimism-rpc.publicnode.com",
    ),
    137: ChainSpec(
        chain_id=137,
        name="polygon",
        family="evm",
        native_symbol="POL",
        wrapped_native="0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        wrapped_native_decimals=18,
        explorer="https://polygonscan.com",
        rpc_env="POLYGON_RPC_URL",
        rpc_fallback_env="POLYGON_RPC_URLS",
        default_rpc="https://polygon-bor-rpc.publicnode.com",
    ),
    130: ChainSpec(
        chain_id=130,
        name="unichain",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        wrapped_native_decimals=18,
        explorer="https://uniscan.xyz",
        rpc_env="UNICHAIN_RPC_URL",
        rpc_fallback_env="UNICHAIN_RPC_URLS",
        default_rpc="https://unichain-rpc.publicnode.com",
    ),
    324: ChainSpec(
        chain_id=324,
        name="zksync",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x5AEa5775959fBC2557Cc8789bC1bf90A239D9a91",
        wrapped_native_decimals=18,
        explorer="https://explorer.zksync.io",
        rpc_env="ZKSYNC_RPC_URL",
        rpc_fallback_env="ZKSYNC_RPC_URLS",
        default_rpc="https://mainnet.era.zksync.io",
    ),
    7777777: ChainSpec(
        chain_id=7777777,
        name="zora",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        wrapped_native_decimals=18,
        explorer="https://explorer.zora.energy",
        rpc_env="ZORA_RPC_URL",
        rpc_fallback_env="ZORA_RPC_URLS",
        default_rpc="https://rpc.zora.energy",
    ),
    480: ChainSpec(
        chain_id=480,
        name="worldchain",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        wrapped_native_decimals=18,
        explorer="https://worldscan.org",
        rpc_env="WORLDCHAIN_RPC_URL",
        rpc_fallback_env="WORLDCHAIN_RPC_URLS",
        default_rpc="https://worldchain-mainnet.g.alchemy.com/public",
    ),
    42220: ChainSpec(
        chain_id=42220,
        name="celo",
        family="evm",
        native_symbol="CELO",
        wrapped_native="0x471EcE3750Da237f93B8E339c536989b8978a438",
        wrapped_native_decimals=18,
        explorer="https://celoscan.io",
        rpc_env="CELO_RPC_URL",
        rpc_fallback_env="CELO_RPC_URLS",
        default_rpc="https://celo-rpc.publicnode.com",
    ),
    59144: ChainSpec(
        chain_id=59144,
        name="linea",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0xe5D7C2a44FfDDf6b295A15c148167daaAf5CF34f",
        wrapped_native_decimals=18,
        explorer="https://lineascan.build",
        rpc_env="LINEA_RPC_URL",
        rpc_fallback_env="LINEA_RPC_URLS",
        default_rpc="https://linea-rpc.publicnode.com",
    ),
    534352: ChainSpec(
        chain_id=534352,
        name="scroll",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x5300000000000000000000000000000000000004",
        wrapped_native_decimals=18,
        explorer="https://scrollscan.com",
        rpc_env="SCROLL_RPC_URL",
        rpc_fallback_env="SCROLL_RPC_URLS",
        default_rpc="https://scroll-rpc.publicnode.com",
    ),
    81457: ChainSpec(
        chain_id=81457,
        name="blast",
        family="evm",
        native_symbol="ETH",
        wrapped_native="0x4300000000000000000000000000000000000004",
        wrapped_native_decimals=18,
        explorer="https://blastscan.io",
        rpc_env="BLAST_RPC_URL",
        rpc_fallback_env="BLAST_RPC_URLS",
        default_rpc="https://blast-rpc.publicnode.com",
    ),
    5000: ChainSpec(
        chain_id=5000,
        name="mantle",
        family="evm",
        native_symbol="MNT",
        wrapped_native="0x78c1b0C915c4FAA5FffA6CAbf0219DA63d7f4cb8",
        wrapped_native_decimals=18,
        explorer="https://mantlescan.xyz",
        rpc_env="MANTLE_RPC_URL",
        rpc_fallback_env="MANTLE_RPC_URLS",
        default_rpc="https://mantle-rpc.publicnode.com",
    ),
}

SOLANA_CHAIN_ID = 101
SOLANA_SPEC = ChainSpec(
    chain_id=SOLANA_CHAIN_ID,
    name="solana",
    family="solana",
    native_symbol="SOL",
    wrapped_native="So11111111111111111111111111111111111111112",
    wrapped_native_decimals=9,
    explorer="https://solscan.io",
    rpc_env="SOLANA_RPC_URL",
    rpc_fallback_env="SOLANA_RPC_URLS",
)

# Ravencoin is deliberately absent: RVN is a Bitcoin fork with no EVM and no
# on-chain AMM/DEX venue, so there is nothing to arbitrage on-chain. See
# docs/multichain.md.

ALL_SPECS: dict[int, ChainSpec] = {**EVM_CHAINS, SOLANA_CHAIN_ID: SOLANA_SPEC}


def get_spec(chain_id: int) -> ChainSpec | None:
    return ALL_SPECS.get(int(chain_id))


def rpc_urls_for(chain_id: int) -> list[str]:
    spec = get_spec(chain_id)
    if spec is None:
        return []
    return rpc_urls(spec)


def rpc_urls(spec: ChainSpec) -> list[str]:
    primary = os.getenv(spec.rpc_env, "").strip()
    fallbacks = [x.strip() for x in os.getenv(spec.rpc_fallback_env, "").split(",") if x.strip()]

    urls: list[str] = []
    # Explicit per-chain env always wins, so an operator can pin one endpoint.
    for url in [primary, *fallbacks]:
        if url and url not in urls:
            urls.append(url)
    # Keep explicit endpoints, but never allow a single public provider to
    # become a hard dependency. Base in particular gets independent fallbacks
    # so a dRPC 429 cannot collapse the whole quote engine.
    if urls and spec.chain_id == 8453 and os.getenv("DEX_RPC_AUTO_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}:
        for url in (
            "https://mainnet.base.org",
            "https://base-rpc.publicnode.com",
            "https://base.llamarpc.com",
            "https://1rpc.io/base",
        ):
            if url not in urls:
                urls.append(url)
        return urls
    if urls:
        return urls

    # No explicit endpoint: prefer authenticated providers, then public fallbacks.
    from src.dragon.rpc_providers import load_providers, private_urls

    for url in private_urls(spec.chain_id, load_providers()):
        if url not in urls:
            urls.append(url)
    if spec.chain_id == 8453:
        for url in (
            "https://base-rpc.publicnode.com",
            "https://base.llamarpc.com",
            "https://1rpc.io/base",
        ):
            if url not in urls:
                urls.append(url)
    if not urls and spec.default_rpc:
        urls.append(spec.default_rpc)
    return urls


def env_chain_ids() -> list[int]:
    """Chains a deployment wants enabled, from DEX_CHAINS (default: Base only)."""
    raw = os.getenv("DEX_CHAINS", "1,56,43114,8453,42161,10,137,130,324,7777777,480,42220,59144,534352,81457,5000").strip()
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            cid = int(part)
        except ValueError:
            continue
        if cid in ALL_SPECS and cid not in ids:
            ids.append(cid)
    return ids or [8453]
