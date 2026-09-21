// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;

import {IHub} from "aave-v4/src/hub/interfaces/IHub.sol";
import {ISpoke} from "aave-v4/src/spoke/interfaces/ISpoke.sol";
import {IPriceOracle} from "aave-v4/src/spoke/interfaces/IPriceOracle.sol";

/// @title DragonAaveV4LiquidityLens
/// @notice Read-only adapter exposing the live Aave V4 Hub -> Spoke -> Reserve model.
/// @dev Synchronized against Aave V4 main commit 40232a0a91150d8ee5cab42bd3ddd0baf4ffff9f.
///      No state changes, approvals, signing, or transaction submission.
contract DragonAaveV4LiquidityLens {
    struct HubAssetView {
        uint256 liquidity;
        uint256 realizedFees;
        uint8 decimals;
        uint256 addedShares;
        uint256 swept;
        int256 premiumOffsetRay;
        uint256 drawnShares;
        uint256 premiumShares;
        uint16 liquidityFee;
        uint256 drawnIndex;
        uint256 drawnRate;
        uint256 lastUpdateTimestamp;
        address underlying;
        address irStrategy;
        address reinvestmentController;
        address feeReceiver;
        uint256 deficitRay;
        uint256 totalAddedAssets;
        uint256 totalOwedAssets;
        uint256 totalPremiumRay;
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

    struct OracleView {
        address oracle;
        address source;
        uint8 decimals;
        uint256 price;
    }

    struct ExecutionView {
        uint256 hubLiquidity;
        uint256 spokeAddedAssets;
        uint256 spokeDrawnShares;
        uint256 spokePremiumShares;
        uint256 spokeDrawnAssets;
        uint256 spokeDrawCap;
        uint256 drawHeadroom;
        uint256 executableLiquidity;
        bool spokeActive;
        bool spokeHalted;
        bool reservePaused;
        bool reserveFrozen;
        bool reserveBorrowable;
    }

    function hubAsset(address hub, uint256 assetId) external view returns (HubAssetView memory out) {
        IHub target = IHub(hub);
        IHub.Asset memory a = target.getAsset(assetId);
        out = HubAssetView({
            liquidity: a.liquidity,
            realizedFees: a.realizedFees,
            decimals: a.decimals,
            addedShares: a.addedShares,
            swept: a.swept,
            premiumOffsetRay: a.premiumOffsetRay,
            drawnShares: a.drawnShares,
            premiumShares: a.premiumShares,
            liquidityFee: a.liquidityFee,
            drawnIndex: a.drawnIndex,
            drawnRate: a.drawnRate,
            lastUpdateTimestamp: a.lastUpdateTimestamp,
            underlying: a.underlying,
            irStrategy: a.irStrategy,
            reinvestmentController: a.reinvestmentController,
            feeReceiver: a.feeReceiver,
            deficitRay: a.deficitRay,
            totalAddedAssets: target.getAddedAssets(assetId),
            totalOwedAssets: target.getAssetTotalOwed(assetId),
            totalPremiumRay: target.getAssetPremiumRay(assetId)
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

    function oracle(address spoke, uint256 reserveId)
        external
        view
        returns (OracleView memory out)
    {
        ISpoke target = ISpoke(spoke);
        IPriceOracle priceOracle = IPriceOracle(target.ORACLE());
        out = OracleView({
            oracle: address(priceOracle),
            source: _oracleSource(spoke, reserveId),
            decimals: priceOracle.decimals(),
            price: priceOracle.getReservePrice(reserveId)
        });
    }

    function executionCapacity(address spoke, uint256 reserveId)
        external
        view
        returns (ExecutionView memory out)
    {
        ISpoke target = ISpoke(spoke);
        ISpoke.Reserve memory reserveData = target.getReserve(reserveId);
        ISpoke.ReserveConfig memory reserveConfig = target.getReserveConfig(reserveId);
        IHub hub = reserveData.hub;
        IHub.Asset memory asset = hub.getAsset(reserveData.assetId);
        IHub.SpokeConfig memory config = hub.getSpokeConfig(reserveData.assetId, spoke);
        IHub.SpokeData memory spokeData = hub.getSpoke(reserveData.assetId, spoke);

        uint256 drawnAssets = hub.previewDrawByShares(
            reserveData.assetId,
            spokeData.drawnShares
        );
        uint256 drawHeadroom = config.drawCap >= drawnAssets
            ? uint256(config.drawCap) - drawnAssets
            : 0;

        if (!config.active || config.halted || reserveConfig.paused) {
            drawHeadroom = 0;
        }

        uint256 executable = asset.liquidity < drawHeadroom ? asset.liquidity : drawHeadroom;

        out = ExecutionView({
            hubLiquidity: asset.liquidity,
            spokeAddedAssets: hub.getAddedAssets(reserveData.assetId),
            spokeDrawnShares: spokeData.drawnShares,
            spokePremiumShares: spokeData.premiumShares,
            spokeDrawnAssets: drawnAssets,
            spokeDrawCap: config.drawCap,
            drawHeadroom: drawHeadroom,
            executableLiquidity: executable,
            spokeActive: config.active,
            spokeHalted: config.halted,
            reservePaused: reserveConfig.paused,
            reserveFrozen: reserveConfig.frozen,
            reserveBorrowable: reserveConfig.borrowable
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

    function userPosition(address spoke, uint256 reserveId, address user)
        external
        view
        returns (
            uint256 suppliedShares,
            uint256 drawnShares,
            bool usingAsCollateral,
            bool borrowing
        )
    {
        ISpoke target = ISpoke(spoke);
        ISpoke.UserPosition memory position = target.getUserPosition(reserveId, user);
        (bool collateral, bool borrowed) = target.getUserReserveStatus(reserveId, user);
        suppliedShares = position.suppliedShares;
        drawnShares = position.drawnShares;
        usingAsCollateral = collateral;
        borrowing = borrowed;
    }

    function userAccount(address spoke, address user)
        external
        view
        returns (ISpoke.UserAccountData memory)
    {
        return ISpoke(spoke).getUserAccountData(user);
    }

    function _oracleSource(address spoke, uint256 reserveId) internal view returns (address) {
        // IAaveOracle extends IPriceOracle and exposes the reserve source.
        // Calling through the concrete interface is kept in a helper to make
        // this lens tolerant of future interface expansion.
        return IAaveOracleLike(ISpoke(spoke).ORACLE()).getReserveSource(reserveId);
    }
}

interface IAaveOracleLike {
    function getReserveSource(uint256 reserveId) external view returns (address);
}
