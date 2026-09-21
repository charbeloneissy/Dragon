from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SOURCE_REPOSITORY = "aave-dao/aave-address-book"
SOURCE_COMMIT = "8d51d3edd585c23c04bafbc22fc9d195b596c3be"
SOURCE_URL = "https://github.com/aave-dao/aave-address-book"
LEGACY_SOURCE_URL = "https://github.com/bgd-labs/aave-address-book"


@dataclass(frozen=True)
class AaveV3Deployment:
    chain_id: int
    name: str
    pool_addresses_provider: str
    pool: str
    oracle: str
    data_provider: str


@dataclass(frozen=True)
class AaveV4Deployment:
    chain_id: int
    name: str
    access_manager: str
    config_engine: str
    hubs: dict[str, str]
    spokes: dict[str, str]
    spoke_oracles: dict[str, str]


# Generated from Aave's maintained address-book snapshot above.
# These are metadata only; no private keys or permissions are embedded.
AAVE_V3: dict[int, AaveV3Deployment] = {
    1: AaveV3Deployment(1, "Ethereum", "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e", "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2", "0x54586bE62E3c3580375aE3723C145253060Ca0C2", "0x0a16f2FCC0D44FaE41cc54e079281D84A363bECD"),
    10: AaveV3Deployment(10, "Optimism", "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", "0x794a61358D6845594F94dc1DB02A252b5b4814aD", "0xD81eb3728a631871a7eBBaD631b5f424909f0c77", "0x243Aa95cAC2a25651eda86e80bEe66114413c43b"),
    56: AaveV3Deployment(56, "BNB", "0xff75B6da14FfbbfD355Daf7a2731456b3562Ba6D", "0x6807dc923806fE8Fd134338EABCA509979a7e0cB", "0x39bc1bfDa2130d6Bb6DBEfd366939b4c7aa7C697", "0xc90Df74A7c16245c5F5C5870327Ceb38Fe5d5328"),
    137: AaveV3Deployment(137, "Polygon", "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", "0x794a61358D6845594F94dc1DB02A252b5b4814aD", "0xb023e699F5a33916Ea823A16485e259257cA8Bd1", "0x243Aa95cAC2a25651eda86e80bEe66114413c43b"),
    8453: AaveV3Deployment(8453, "Base", "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D", "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5", "0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156", "0x0F43731EB8d45A581f4a36DD74F5f358bc90C73A"),
    42161: AaveV3Deployment(42161, "Arbitrum", "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", "0x794a61358D6845594F94dc1DB02A252b5b4814aD", "0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7", "0x243Aa95cAC2a25651eda86e80bEe66114413c43b"),
    43114: AaveV3Deployment(43114, "Avalanche", "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", "0x794a61358D6845594F94dc1DB02A252b5b4814aD", "0xEBd36016B3eD09D4693Ed4251c67Bd858c3c7C9C", "0x243Aa95cAC2a25651eda86e80bEe66114413c43b"),
}


AAVE_V4: dict[int, AaveV4Deployment] = {
    1: AaveV4Deployment(
        1, "Ethereum", "0x08aE3BE30958cDd1847ec58fFfd4C451a87fDF01", "0xa1673fbD457747A05e91D9ef904Cb12827916B1E",
        {
            "CORE_HUB": "0xCca852Bc40e560adC3b1Cc58CA5b55638ce826c9",
            "PLUS_HUB": "0x06002e9c4412CB7814a791eA3666D905871E536A",
            "PRIME_HUB": "0x943827DCA022D0F354a8a8c332dA1e5Eb9f9F931",
            "GLOBAL_DOLLAR_HUB": "0x62d63197660c080236193CA60b70E49A08E90368",
        },
        {
            "MAIN_SPOKE": "0x94e7A5dCbE816e498b89aB752661904E2F56c485",
            "FOREX_SPOKE": "0xD8B93635b8C6d0fF98CbE90b5988E3F2d1Cd9da1",
            "GOLD_SPOKE": "0x65407b940966954b23dfA3caA5C0702bB42984DC",
        },
        {
            "MAIN_SPOKE_ORACLE": "0x99B2B6CEa9C3D2fd8F4d90f86741C44B212a6127",
            "FOREX_SPOKE_ORACLE": "0xB3CE6E7b6d389a66eA4a3777bA07219d00FB3a9D",
            "GOLD_SPOKE_ORACLE": "0x0083421fd178749af2201ddA5A7C3feB5790B80c",
        },
    ),
    5042: AaveV4Deployment(
        5042, "Arc", "0x24761DB265998ba1D38E8a29031cF72C2CeF3A7D", "0x0A3af96f72b1B52c9BB9778FcD839154c2599371",
        {"CORE_HUB": "0x17288dfc86205301064577b98B02b81017e6F79C"},
        {
            "MAIN_SPOKE": "0xB843bdC3a87A05E77E07Df9FE48928b3A34b134d",
            "FOREX_SPOKE": "0x4164EBCAF74670aa74C8D4F59de6157c0780F1bB",
        },
        {
            "MAIN_SPOKE_ORACLE": "0x6ffE98F3422041236c19923EDB949F18A69e8A09",
            "FOREX_SPOKE_ORACLE": "0x2abd2B5C30D649273B3b762b0E1758BaC8F87cFE",
        },
    ),
    8453: AaveV4Deployment(
        8453, "Base", "0x4010C94698EDE9d895814502B6EB122D764a1Cc6", "0x8753d579B592f3F45902b4Dc13B547B8E8BD03c4",
        {"EQUITIES_HUB": "0xa4d5947Eb727A052bae69C593FfC84247EC9864E"},
        {"MAG7_SPOKE": "0x17905Db0e4A3514467539956c084180616AE7B8D"},
        {"MAG7_SPOKE_ORACLE": "0xaBaf048fD7675Ea34a84332371ffd5D55E322A47"},
    ),
    43114: AaveV4Deployment(
        43114, "Avalanche", "0xe069096bDAfF9bAD15b2f1079EaF0f1685a24522", "0x1F0C67Fde7FcaF7eCEA43b76A23461803972c45c",
        {"CORE_HUB": "0xd07369fAE4A5BB13c9Ce446B052c7867B1AbDf6e"},
        {
            "MAIN_SPOKE": "0x435272CefF93a1E657E8ABfdf0A13e95900A3a56",
            "FOREX_SPOKE": "0x6a37776B5E026dBdF043b4F933c323C84DD1B514",
        },
        {
            "MAIN_SPOKE_ORACLE": "0x84B50B131a82dA689C0205C00d603c1c92A5f8a4",
            "FOREX_SPOKE_ORACLE": "0xECA623E8c923A103Eef75cd127Cc8Bf8fF886cf5",
        },
    ),
}


def aave_v3(chain_id: int) -> AaveV3Deployment | None:
    return AAVE_V3.get(int(chain_id))


def aave_v4(chain_id: int) -> AaveV4Deployment | None:
    return AAVE_V4.get(int(chain_id))


def v3_flash_loan_pool(chain_id: int) -> str:
    deployment = aave_v3(chain_id)
    return deployment.pool if deployment else ""


def snapshot() -> dict[str, Any]:
    return {
        "repository": SOURCE_REPOSITORY,
        "legacy_repository": LEGACY_SOURCE_URL,
        "commit": SOURCE_COMMIT,
        "url": SOURCE_URL,
        "v3": {
            str(cid): {
                "name": row.name,
                "pool_addresses_provider": row.pool_addresses_provider,
                "pool": row.pool,
                "oracle": row.oracle,
                "data_provider": row.data_provider,
            }
            for cid, row in AAVE_V3.items()
        },
        "v4": {
            str(cid): {
                "name": row.name,
                "access_manager": row.access_manager,
                "config_engine": row.config_engine,
                "hubs": row.hubs,
                "spokes": row.spokes,
                "spoke_oracles": row.spoke_oracles,
            }
            for cid, row in AAVE_V4.items()
        },
    }
