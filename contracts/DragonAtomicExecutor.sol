// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;

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

interface IAaveV3FlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

contract DragonAtomicExecutor is IAaveV3FlashLoanSimpleReceiver {
    struct Leg {
        address target;
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint256 minAmountOut;
        bytes data;
    }

    address public immutable owner;
    IAaveV3Pool public immutable pool;
    uint256 public immutable minProfit;

    mapping(address => bool) public allowedTarget;

    error NotOwner();
    error NotPool();
    error BadInitiator();
    error TargetNotAllowed();
    error BadTokenBalance();
    error SwapFailed();
    error InsufficientProfit();
    error RepaymentFailed();

    event TargetPermission(address indexed target, bool allowed);
    event AtomicExecution(
        address indexed asset,
        uint256 borrowed,
        uint256 premium,
        uint256 profit
    );

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address pool_, uint256 minProfit_) {
        require(pool_ != address(0), "pool=0");
        owner = msg.sender;
        pool = IAaveV3Pool(pool_);
        minProfit = minProfit_;
    }

    function setAllowedTarget(address target, bool allowed) external onlyOwner {
        allowedTarget[target] = allowed;
        emit TargetPermission(target, allowed);
    }

    function startFlashLoan(
        address asset,
        uint256 amount,
        Leg calldata first,
        Leg calldata second
    ) external onlyOwner {
        bytes memory params = abi.encode(first, second);
        pool.flashLoanSimple(address(this), asset, amount, params, 0);
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        if (msg.sender != address(pool)) revert NotPool();
        if (initiator != address(this)) revert BadInitiator();

        (Leg memory first, Leg memory second) = abi.decode(params, (Leg, Leg));

        if (!allowedTarget[first.target] || !allowedTarget[second.target]) {
            revert TargetNotAllowed();
        }
        if (first.tokenIn != asset || first.amountIn != amount) {
            revert BadTokenBalance();
        }
        if (second.tokenIn != first.tokenOut || second.amountIn == 0) {
            revert BadTokenBalance();
        }

        uint256 beforeAsset = IERC20(asset).balanceOf(address(this));
        _approve(first.tokenIn, first.target, first.amountIn);
        _call(first);
        uint256 intermediate = IERC20(first.tokenOut).balanceOf(address(this));
        if (intermediate < second.amountIn || intermediate < first.minAmountOut) {
            revert BadTokenBalance();
        }

        _approve(second.tokenIn, second.target, second.amountIn);
        _call(second);

        uint256 finalBalance = IERC20(asset).balanceOf(address(this));
        uint256 repayment = amount + premium;
        if (finalBalance < beforeAsset + repayment + minProfit) revert InsufficientProfit();
        uint256 profit = finalBalance - beforeAsset - repayment;

        if (!IERC20(asset).approve(address(pool), repayment)) {
            revert RepaymentFailed();
        }

        emit AtomicExecution(asset, amount, premium, profit);
        return true;
    }

    function _approve(address token, address spender, uint256 amount) internal {
        if (!IERC20(token).approve(spender, amount)) revert SwapFailed();
    }

    function _call(Leg memory leg) internal {
        (bool ok, bytes memory result) = leg.target.call(leg.data);
        if (!ok) {
            if (result.length > 0) assembly { revert(add(result, 32), mload(result)) }
            revert SwapFailed();
        }
        if (leg.minAmountOut > 0) {
            // The router calldata itself carries amountOutMinimum. This value is
            // retained in the leg for the simulator/audit record and is not trusted
            // as a replacement for router-level slippage protection.
        }
    }

    function rescue(address token, address to, uint256 amount) external onlyOwner {
        require(to != address(0), "to=0");
        if (!IERC20(token).transfer(to, amount)) revert RepaymentFailed();
    }
}
