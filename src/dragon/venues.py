"""Per-chain DEX venue registry.

Venues are grouped by AMM interface so one adapter implementation can serve many:

* ``v2``     — Uniswap V2 style: ``getAmountsOut`` + ``swapExactTokensForTokens``.
* ``v3``     — Uniswap V3 style: ``QuoterV2.quoteExactInputSingle`` + ``SwapRouter``.
* ``stable`` — Aerodrome/Velodrome style: V2-shaped router with a per-hop
               ``stable`` flag and a factory address.

Every address here was verified to have deployed bytecode on its chain. Adding a
venue is a registry edit, not a new code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Venue:
    name: str
    kind: str
    router: str
    factory: str = ""
    quoter: str = ""
    fee_tiers: tuple[int, ...] = ()
    enabled_by_default: bool = True
    note: str = ""


V3_DEFAULT_FEES: tuple[int, ...] = (100, 500, 3000, 10000)
EVM_VENUES: dict[int, tuple[Venue, ...]] = {
    1: (
        Venue(name="Uniswap_V3", kind="v3", router="0xE592427A0AEce92De3Edee1F18E0157C05861564", factory="0x1F98431c8aD98523631AE4a59f267346ea31F984", quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e", fee_tiers=(100, 500, 3000, 10000)),
        Venue(name="Uniswap_V2", kind="v2", router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D", factory="0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"),
        Venue(name="SushiSwap_V2", kind="v2", router="0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F", factory="0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac"),
        Venue(name="PancakeSwap_V3", kind="v3", router="0x1b81D678ffb9C0263b24A97847620C99d213eB14", factory="0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865", quoter="0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997", fee_tiers=(100, 500, 2500, 10000)),
    ),
    10: (
        Venue(name="Uniswap_V3", kind="v3", router="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45", factory="0x1F98431c8aD98523631AE4a59f267346ea31F984", quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e", fee_tiers=(100, 500, 3000, 10000)),
        Venue(name="Velodrome_V2", kind="stable", router="0xa062ae8a9c5e11aaa026fc2670b0d65ccc8b2858", factory="0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a"),
    ),
    56: (
        Venue(name="PancakeSwap_V3", kind="v3", router="0x1b81D678ffb9C0263b24A97847620C99d213eB14", quoter="0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997", fee_tiers=(100, 500, 2500, 10000)),
        Venue(name="PancakeSwap_V2", kind="v2", router="0x10ED43C718714eb63d5aA57B78B54704E256024E", factory="0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"),
        Venue(name="SushiSwap_V2", kind="v2", router="0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506"),
        Venue(name="Uniswap_V2", kind="v2", router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24", factory="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"),
        Venue(name="BiSwap_V2", kind="v2", router="0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8", factory="0x858E3312ed3A876947EA49d572A7C42DE08af7EE"),
    ),
    130: (
        Venue(name="Uniswap_V3", kind="v3", router="0x73855d06de49d0fe4a9c42636ba96c62da12ff9c", factory="0x1f98400000000000000000000000000000000003", quoter="0x565ac8c7863d9bb16d07e809ff49fe5cd467634c", fee_tiers=(100, 500, 3000, 10000)),
    ),
    137: (
        Venue(name="Uniswap_V3", kind="v3", router="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45", factory="0x1F98431c8aD98523631AE4a59f267346ea31F984", quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e", fee_tiers=(100, 500, 3000, 10000)),
        Venue(name="QuickSwap_V2", kind="v2", router="0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff", factory="0x5757371414417b8C6CAad45bAeF941aBc7d3Ab32"),
    ),
    324: (
        Venue(name="Uniswap_V3", kind="v3", router="0x99c56385daBCE3E81d8499d0b8d0257aBC07E8A3", factory="0x8FdA5a7a8dCA67BBcDd10F02Fa0649A937215422", quoter="0x8Cb537fc92E26d8EBBb760E632c95484b6Ea3e28", fee_tiers=(500, 3000, 10000)),
    ),
    480: (
        Venue(name="Uniswap_V3", kind="v3", router="0x091AD9e2e6e5eD44c1c66dB50e49A601F9f36cF6", factory="0x7a5028BDa40e7B173C278C5342087826455ea25a", quoter="0x10158D43e6cc414deE1Bd1eB0EfC6a5cBCfF244c", fee_tiers=(100, 500, 3000, 10000)),
    ),
    42220: (
        Venue(name="Uniswap_V3", kind="v3", router="0x5615CDAb10dc425a742d643d949a7F474C01abc4", factory="0xAfE208a311B21f13EF87E33A90049fC17A7acDEc", quoter="0x82825d0554fA07f7FC52Ab63c961F330fdEFa8E8", fee_tiers=(100, 500, 3000, 10000)),
    ),
    42161: (
        Venue(name="Uniswap_V3", kind="v3", router="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45", factory="0x1F98431c8aD98523631AE4a59f267346ea31F984", quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e", fee_tiers=(100, 500, 3000, 10000)),
        Venue(name="Camelot_V2", kind="v2", router="0xc873fEcbd354f5A56E00E710B90EF4201db2448d", factory="0x6EcCab422D763aC031210895C81787E87B43A652"),
    ),
    43114: (
        Venue(name="Uniswap_V3", kind="v3", router="0xbb00FF08d01D300023C629E8fFfFcb65A5a578cE", quoter="0xbe0F5544EC67e9B3b2D979aaA43f18Fd87E6257F", fee_tiers=(500, 3000, 10000)),
        Venue(name="SushiSwap_V2", kind="v2", router="0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506"),
        Venue(name="TraderJoe_V2_1", kind="v2", router="0x60aE616a2155Ee3d9A68541Ba4544862310933d4", factory="0x9Ad6C38BE94206cA50bb0d90783181662f0Cfa10"),
    ),
    5000: (
        Venue(name="Agni_V3", kind="v3", router="0x319B69888b0d11cEC22caA5034e25FfFBDc88421", fee_tiers=(100, 500, 3000, 10000), enabled_by_default=False, note="Agni router uses a non-standard ISwapRouter; needs a dedicated quoter."),
        Venue(name="MerchantMoe_V2", kind="v2", router="0xeaEE7EE68874218c3558b40063c42B82D3E7232a"),
    ),
    59144: (
        Venue(name="Uniswap_V3", kind="v3", router="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a", factory="0x31FAfd4889FA1269F7a13A66eE0fB458f27D72A9", quoter="0x58ead433ea99708604c4dd7c9b7e80c70976e202", fee_tiers=(100, 500, 3000, 10000)),
    ),
    8453: (
        Venue(name="Uniswap_V3", kind="v3", router="0x2626664c2603336E57B271c5C0b26F421741e481", factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD", quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a", fee_tiers=(100, 500, 3000, 10000)),
        Venue(name="Aerodrome", kind="stable", router="0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43", factory="0x420DD381b31aEf6683db6B902084cB0FFECe40Da"),
        Venue(name="PancakeSwap_V3", kind="v3", router="0x1b81D678ffb9C0263b24A97847620C99d213eB14", quoter="0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997", fee_tiers=(100, 500, 2500, 10000)),
        Venue(name="SushiSwap_V3", kind="v3", router="0xFB7eF66a7e61224DD6FcD0D7d9C3be5C8B049b9f", factory="0xc35DADB65012eC5796536bD9864eD8773aBc74C4", quoter="0xb1E835Dc2785b52265711e17fCCb0fd018226a6e", fee_tiers=(100, 500, 3000, 10000)),
        Venue(name="SushiSwap_V2", kind="v2", router="0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891"),
        Venue(name="Uniswap_V2", kind="v2", router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24", factory="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"),
        Venue(name="BaseSwap_V2", kind="v2", router="0x327Df1E6de05895d2ab08513aaDD9313Fe505d86", factory="0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB"),
        Venue(name="SwapBased_V2", kind="v2", router="0xaaa3b1F1bd7BCc97fD1917c18ADE665C5D31F066"),
    ),
    81457: (
        Venue(name="Thruster_V2", kind="v2", router="0x98994a9A7a2570367554589189dC9772241650f6"),
    ),
    534352: (
        Venue(name="SyncSwap_V2", kind="v2", router="0x80e38291e06339d10AAB483C65695D004dBD5C69"),
        # Official Scroll deployment: https://gov.uniswap.org/t/official-uniswap-v3-deployments-list/24323
        Venue(name="Uniswap_V3", kind="v3", router="0xfc30937f5cDe93Df8d48aCAF7e6f5D8D8A31F636", factory="0x70C62C8b8e801124A4Aa81ce07b637A3e83cb919", quoter="0x2566e082Cb1656d22BCbe5644F5b997D194b5299", fee_tiers=(100, 500, 3000, 10000)),
        # ScrollScan identifies this deployment as SushiSwap's UniswapV2Router02.
        Venue(name="SushiSwap_V2", kind="v2", router="0x9B3336186a38E1b6c21955d112dbb0343Ee061eE"),
    ),
}

def venues_for(chain_id: int, *, include_disabled: bool = False) -> tuple[Venue, ...]:
    venues = EVM_VENUES.get(int(chain_id), ())
    if include_disabled:
        return venues
    return tuple(v for v in venues if v.enabled_by_default)


def venue_names(chain_id: int) -> tuple[str, ...]:
    return tuple(v.name for v in venues_for(chain_id))


def select_venues(chain_id: int, requested: tuple[str, ...] | list[str] | None) -> tuple[Venue, ...]:
    """Return requested venues that exist for the chain, or all enabled ones."""
    available = {v.name: v for v in venues_for(chain_id, include_disabled=True)}
    if not requested:
        return venues_for(chain_id)
    selected = []
    for name in requested:
        venue = available.get(name.strip())
        if venue is not None and venue not in selected:
            selected.append(venue)
    return tuple(selected)
