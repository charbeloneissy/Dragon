// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ISpoke} from "aave-v4/src/spoke/interfaces/ISpoke.sol";

/// @notice Minimal Aave V3 flash-loan receiver for Dragon.
/// @dev Owner-only entrypoint; DEX targets must be explicitly allowlisted.
interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

interface IAaveV3Pool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

interface IAaveFlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

contract DragonAaveFlashExecutor is IAaveFlashLoanSimpleReceiver {
    error NotOwner();
    error NotPool();
    error NotInitiator();
    error TargetNotAllowed();
    error CallFailed();
    error InsufficientProfit();
    error InvalidAsset();
    error InvalidPool();
    error TransferFailed();
    error ApprovalFailed();
    error InvalidYieldPlan();
    error InvalidYieldBps();
    error YieldAssetMismatch();
    error YieldSupplyFailed();
    error WithdrawFailed();

    struct Approval {
        address token;
        address spender;
        uint256 amount;
    }

    struct Call {
        address target;
        uint256 value;
        bytes data;
    }

    struct YieldPlan {
        address spoke;
        uint256 reserveId;
        uint16 bps;
    }

    struct FlashPlan {
        uint256 minProfit;
        Approval[] approvals;
        Call[] calls;
        YieldPlan yieldPlan;
    }

    address public immutable owner;
    address public immutable pool;
    mapping(address => bool) public allowedTarget;

    event TargetPermissionUpdated(address indexed target, bool allowed);
    event FlashStarted(address indexed asset, uint256 amount, uint256 minProfit);
    event FlashCompleted(address indexed asset, uint256 amount, uint256 premium, uint256 profit);
    event ProfitSuppliedToAaveV4(
        address indexed spoke,
        uint256 indexed reserveId,
        uint256 amount,
        uint256 remainingProfit
    );
    event AaveV4SupplyWithdrawn(
        address indexed spoke,
        uint256 indexed reserveId,
        uint256 amount,
        address indexed to
    );

    constructor(address pool_, address owner_) {
        if (pool_ == address(0) || owner_ == address(0)) revert InvalidPool();
        pool = pool_;
        owner = owner_;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    function setAllowedTarget(address target, bool allowed) external onlyOwner {
        allowedTarget[target] = allowed;
        emit TargetPermissionUpdated(target, allowed);
    }

    function setAllowedTargets(address[] calldata targets, bool allowed) external onlyOwner {
        for (uint256 i; i < targets.length; ++i) {
            allowedTarget[targets[i]] = allowed;
            emit TargetPermissionUpdated(targets[i], allowed);
        }
    }

    /// @notice Starts one atomic flash-loan plan. The transaction reverts if
    /// the final asset balance cannot repay principal + premium + minProfit.
    function startFlashLoan(
        address asset,
        uint256 amount,
        FlashPlan calldata plan
    ) external onlyOwner {
        if (asset == address(0) || amount == 0) revert InvalidAsset();
        emit FlashStarted(asset, amount, plan.minProfit);
        bytes memory params = abi.encode(plan);
        IAaveV3Pool(pool).flashLoanSimple(address(this), asset, amount, params, 0);
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        if (msg.sender != pool) revert NotPool();
        if (initiator != address(this)) revert NotInitiator();

        FlashPlan memory plan = abi.decode(params, (FlashPlan));
        uint256 beforeBalance = IERC20(asset).balanceOf(address(this));

        for (uint256 i; i < plan.approvals.length; ++i) {
            Approval memory a = plan.approvals[i];
            if (!allowedTarget[a.spender]) revert TargetNotAllowed();
            if (!IERC20(a.token).approve(a.spender, a.amount)) revert ApprovalFailed();
        }

        for (uint256 i; i < plan.calls.length; ++i) {
            Call memory c = plan.calls[i];
            if (!allowedTarget[c.target]) revert TargetNotAllowed();
            (bool ok,) = c.target.call{value: c.value}(c.data);
            if (!ok) revert CallFailed();
        }

        uint256 owed = amount + premium;
        uint256 afterBalance = IERC20(asset).balanceOf(address(this));
        if (afterBalance < beforeBalance + owed + plan.minProfit) revert InsufficientProfit();

        uint256 profit = afterBalance - beforeBalance - owed;
        uint256 yieldAmount;

        if (plan.yieldPlan.bps != 0) {
            if (plan.yieldPlan.spoke == address(0)) revert InvalidYieldPlan();
            if (plan.yieldPlan.bps > 10_000) revert InvalidYieldBps();

            yieldAmount = (profit * plan.yieldPlan.bps) / 10_000;
            if (profit < yieldAmount || profit - yieldAmount < plan.minProfit) {
                revert InsufficientProfit();
            }

            ISpoke spoke = ISpoke(plan.yieldPlan.spoke);
            ISpoke.Reserve memory reserve = spoke.getReserve(plan.yieldPlan.reserveId);
            if (reserve.underlying != asset) revert YieldAssetMismatch();
            if (address(reserve.hub) == address(0)) revert InvalidYieldPlan();

            if (!IERC20(asset).approve(address(reserve.hub), yieldAmount)) {
                revert ApprovalFailed();
            }
            try spoke.supply(plan.yieldPlan.reserveId, yieldAmount, address(this)) returns (uint256, uint256) {
            } catch {
                revert YieldSupplyFailed();
            }

            emit ProfitSuppliedToAaveV4(
                plan.yieldPlan.spoke,
                plan.yieldPlan.reserveId,
                yieldAmount,
                profit - yieldAmount
            );
        }

        if (!IERC20(asset).approve(pool, owed)) revert ApprovalFailed();

        uint256 remainingProfit = profit - yieldAmount;
        if (remainingProfit > 0 && !IERC20(asset).transfer(owner, remainingProfit)) {
            revert TransferFailed();
        }

        emit FlashCompleted(asset, amount, premium, remainingProfit);
        return true;
    }

    function withdrawAaveV4Supply(
        address spokeAddress,
        uint256 reserveId,
        uint256 amount,
        address to
    ) external onlyOwner returns (uint256 withdrawnAmount) {
        if (spokeAddress == address(0) || to == address(0)) revert InvalidYieldPlan();
        if (amount == 0) revert InvalidYieldPlan();

        try ISpoke(spokeAddress).withdraw(reserveId, amount, address(this)) returns (
            uint256,
            uint256 assets
        ) {
            withdrawnAmount = assets;
        } catch {
            revert WithdrawFailed();
        }

        if (!IERC20(ISpoke(spokeAddress).getReserve(reserveId).underlying).transfer(to, withdrawnAmount)) {
            revert TransferFailed();
        }

        emit AaveV4SupplyWithdrawn(spokeAddress, reserveId, withdrawnAmount, to);
    }

    receive() external payable {}
}
