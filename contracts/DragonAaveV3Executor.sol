// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 { function balanceOf(address account) external view returns (uint256); function allowance(address owner,address spender) external view returns (uint256); function approve(address spender,uint256 amount) external returns (bool); function transfer(address to,uint256 amount) external returns (bool); }
interface IAaveV3Pool { function flashLoanSimple(address receiverAddress,address asset,uint256 amount,bytes calldata params,uint16 referralCode) external; }
interface IFlashLoanSimpleReceiver { function executeOperation(address asset,uint256 amount,uint256 premium,address initiator,bytes calldata params) external returns (bool); }

contract DragonAaveV3Executor is IFlashLoanSimpleReceiver {
    error NotOwner(); error NotPool(); error InvalidAsset(); error InvalidAmount(); error InvalidTarget(); error InvalidBlock(); error PreExistingBalance(); error FirstLegFailed(); error SecondLegFailed(); error FirstLegSlippage(); error SecondLegSlippage(); error RepaymentShortfall(); error ProfitTooSmall(); error TransferFailed(); error Reentrancy(); error ProfitRecipientMismatch();
    struct Call { address target; bytes data; address sellToken; address buyToken; address allowanceTarget; uint256 sellAmount; uint256 minBuyAmount; }
    struct FlashParams { address owner; uint256 minProfit; uint256 maxBlockNumber; uint256 compoundAmount; bool compoundProfit; Call first; Call second; }
    address public immutable POOL; address public owner; bool private entered; mapping(address => mapping(address => bool)) private approvalCached; bool private poolApprovalCached;
    event OwnershipTransferred(address indexed oldOwner,address indexed newOwner);
    event FlashArbitrageExecuted(address indexed asset,uint256 amount,uint256 premium,uint256 profit,uint256 compoundAmount,uint256 retainedProfit,address indexed firstTarget,address indexed secondTarget);
    modifier onlyOwner(){if(msg.sender!=owner)revert NotOwner();_;}
    modifier nonReentrant(){if(entered)revert Reentrancy();entered=true;_;entered=false;}
    constructor(address pool,address initialOwner){if(pool==address(0)||initialOwner==address(0))revert InvalidTarget();POOL=pool;owner=initialOwner;emit OwnershipTransferred(address(0),initialOwner);}
    function transferOwnership(address newOwner) external onlyOwner {if(newOwner==address(0))revert InvalidTarget();emit OwnershipTransferred(owner,newOwner);owner=newOwner;}
    function executeFlashArbitrage(address asset,uint256 amount,FlashParams calldata params) external onlyOwner nonReentrant {
        if(params.owner!=owner)revert ProfitRecipientMismatch();
        if(params.compoundProfit||params.compoundAmount!=0)revert InvalidAmount();
        if(asset==address(0)||asset!=params.first.sellToken||asset!=params.second.buyToken)revert InvalidAsset();
        if(amount==0||params.first.sellAmount!=amount+params.compoundAmount)revert InvalidAmount(); if(params.maxBlockNumber<block.number)revert InvalidBlock();
        _validateCall(params.first);_validateCall(params.second);if(params.first.buyToken!=params.second.sellToken)revert InvalidAsset();
        IAaveV3Pool(POOL).flashLoanSimple(address(this),asset,amount,abi.encode(params),0);
    }
    function executeOperation(address asset,uint256 amount,uint256 premium,address initiator,bytes calldata rawParams) external override returns(bool){
        if(msg.sender!=POOL||initiator!=address(this))revert NotPool();
        FlashParams memory params=abi.decode(rawParams,(FlashParams));
        if(params.owner!=owner)revert ProfitRecipientMismatch();
        if(params.maxBlockNumber<block.number)revert InvalidBlock();
        if(params.first.sellToken!=asset||params.second.buyToken!=asset||params.first.sellAmount!=amount+params.compoundAmount)revert InvalidAsset();
        if(params.first.buyToken!=params.second.sellToken)revert InvalidAsset();_validateCall(params.first);_validateCall(params.second);
        _approve(params.first.sellToken,params.first.allowanceTarget,amount);
        uint256 baseBefore=IERC20(params.first.buyToken).balanceOf(address(this));
        (bool okFirst,)=params.first.target.call(params.first.data);if(!okFirst)revert FirstLegFailed();
        uint256 baseAfter=IERC20(params.first.buyToken).balanceOf(address(this));
        if(baseAfter<baseBefore||baseAfter-baseBefore<params.first.minBuyAmount)revert FirstLegSlippage();
        uint256 baseToSell=params.second.sellAmount;if(baseToSell==0||baseToSell>baseAfter-baseBefore)revert SecondLegSlippage();
        _approve(params.second.sellToken,params.second.allowanceTarget,baseToSell);
        uint256 quoteBeforeSecond=IERC20(asset).balanceOf(address(this));
        (bool okSecond,)=params.second.target.call(params.second.data);if(!okSecond)revert SecondLegFailed();
        uint256 quoteAfterSecond=IERC20(asset).balanceOf(address(this));
        if(quoteAfterSecond<quoteBeforeSecond||quoteAfterSecond-quoteBeforeSecond<params.second.minBuyAmount)revert SecondLegSlippage();
        uint256 repayment=amount+premium;uint256 roundTripOutput=quoteAfterSecond-quoteBeforeSecond;
        if(roundTripOutput<repayment+params.compoundAmount)revert RepaymentShortfall();
        uint256 profit=roundTripOutput-repayment-params.compoundAmount;if(profit<params.minProfit)revert ProfitTooSmall();
        _approvePool(asset);uint256 retainedProfit=0;
        if(profit>0)_safeTransfer(asset,params.owner,profit);
        emit FlashArbitrageExecuted(asset,amount,premium,profit,params.compoundAmount,retainedProfit,params.first.target,params.second.target);return true;
    }
    function rescueToken(address token,address to,uint256 amount) external onlyOwner {if(token==address(0)||to==address(0))revert InvalidTarget();_safeTransfer(token,to,amount);}
    function _validateCall(Call memory c) internal view {if(c.target==address(0)||c.target==POOL||c.allowanceTarget==address(0))revert InvalidTarget();if(c.sellToken==address(0)||c.buyToken==address(0)||c.sellToken==c.buyToken)revert InvalidAsset();if(c.sellAmount==0||c.minBuyAmount==0||c.data.length==0)revert InvalidAmount();}
    function _approve(address token,address spender,uint256 amount) internal { if(approvalCached[token][spender])return; if(!IERC20(token).approve(spender,type(uint256).max))revert TransferFailed(); approvalCached[token][spender]=true; }
    function _approvePool(address token) internal { if(poolApprovalCached)return; if(!IERC20(token).approve(POOL,type(uint256).max))revert TransferFailed(); poolApprovalCached=true; }
    function _safeTransfer(address token,address to,uint256 amount) internal {if(!IERC20(token).transfer(to,amount))revert TransferFailed();}
    receive() external payable {}
}
