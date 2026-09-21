// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;

import {IHub} from "aave-v4/src/hub/interfaces/IHub.sol";
import {ISpoke} from "aave-v4/src/spoke/interfaces/ISpoke.sol";

/// @title DragonAaveV4LiquidityLens
/// @notice Read-only adapter exposing the Aave V4 Hub -> Spoke -> Reserve model.
/// @dev No state changes, approvals, signing, or transaction submission.
contract DragonAaveV4LiquidityLens {
    struct HubAssetView {
        uint256 liquidity;
        uint8 decimals;
        uint120 addedShares;
        uint120 drawnShares;
        uint120 premiumShares;
        uint16 liquidityFee;
        address underlying;
        address feeReceiver;
        uint200 deficitRay;
    }

    struct SpokeConfigView {
        uint40 addCap;
        uint40 drawCap;
        uint24 riskPremiumThreshold;
        bool active;
        bool halted;
    }

    struct ReserveView {
        address underlying;
        address hub;
        uint16 assetId;
        uint8 decimals;
        uint24 collateralRisk;
        uint32 dynamicConfigKey;
    }

    struct ReserveConfigView {
        uint24 collateralRisk;
        bool paused;
        bool frozen;
        bool borrowable;
        bool receiveSharesEnabled;
    }

    function hubAsset(address hub, uint256 assetId) external view returns (HubAssetView memory out) {
        IHub.Asset memory a = IHub(hub).getAsset(assetId);
        out = HubAssetView({
            liquidity: a.liquidity,
            decimals: a.decimals,
            addedShares: a.addedShares,
            drawnShares: a.drawnShares,
            premiumShares: a.premiumShares,
            liquidityFee: a.liquidityFee,
            underlying: a.underlying,
            feeReceiver: a.feeReceiver,
            deficitRay: a.deficitRay
        });
    }

    function spokeConfig(address hub, uint256 assetId, address spoke)
        external
        view
        returns (SpokeConfigView memory out)
    {
        IHub.SpokeConfig memory c = IHub(hub).getSpokeConfig(assetId, spoke);
        out = SpokeConfigView({
            addCap: c.addCap,
            drawCap: c.drawCap,
            riskPremiumThreshold: c.riskPremiumThreshold,
            active: c.active,
            halted: c.halted
        });
    }

    function reserve(address spoke, uint256 reserveId)
        external
        view
        returns (ReserveView memory reserveView, ReserveConfigView memory configView)
    {
        ISpoke target = ISpoke(spoke);
        ISpoke.Reserve memory r = target.getReserve(reserveId);
        ISpoke.ReserveConfig memory c = target.getReserveConfig(reserveId);

        reserveView = ReserveView({
            underlying: r.underlying,
            hub: address(r.hub),
            assetId: r.assetId,
            decimals: r.decimals,
            collateralRisk: r.collateralRisk,
            dynamicConfigKey: r.dynamicConfigKey
        });

        configView = ReserveConfigView({
            collateralRisk: c.collateralRisk,
            paused: c.paused,
            frozen: c.frozen,
            borrowable: c.borrowable,
            receiveSharesEnabled: c.receiveSharesEnabled
        });
    }

    function reserveIdFor(address spoke, address hub, uint256 assetId)
        external
        view
        returns (uint256)
    {
        return ISpoke(spoke).getReserveId(hub, assetId);
    }

    function reserveLiquidity(address spoke, uint256 reserveId)
        external
        view
        returns (uint256 suppliedAssets, uint256 suppliedShares, uint256 totalDebt)
    {
        ISpoke target = ISpoke(spoke);
        suppliedAssets = target.getReserveSuppliedAssets(reserveId);
        suppliedShares = target.getReserveSuppliedShares(reserveId);
        totalDebt = target.getReserveTotalDebt(reserveId);
    }
}
