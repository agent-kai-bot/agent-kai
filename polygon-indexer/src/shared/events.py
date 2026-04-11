from __future__ import annotations

from .evm import topic_from_signature

TRANSFER_SIGNATURE = "Transfer(address,address,uint256)"
APPROVAL_SIGNATURE = "Approval(address,address,uint256)"
V2_SWAP_SIGNATURE = "Swap(address,uint256,uint256,uint256,uint256,address)"
V3_SWAP_SIGNATURE = "Swap(address,address,int256,int256,uint160,uint128,int24)"
OWNERSHIP_TRANSFERRED_SIGNATURE = "OwnershipTransferred(address,address)"
UPGRADED_SIGNATURE = "Upgraded(address)"
PAUSED_SIGNATURE = "Paused(address)"
UNPAUSED_SIGNATURE = "Unpaused(address)"

TRANSFER_TOPIC = topic_from_signature(TRANSFER_SIGNATURE)
APPROVAL_TOPIC = topic_from_signature(APPROVAL_SIGNATURE)
V2_SWAP_TOPIC = topic_from_signature(V2_SWAP_SIGNATURE)
V3_SWAP_TOPIC = topic_from_signature(V3_SWAP_SIGNATURE)
OWNERSHIP_TRANSFERRED_TOPIC = topic_from_signature(OWNERSHIP_TRANSFERRED_SIGNATURE)
UPGRADED_TOPIC = topic_from_signature(UPGRADED_SIGNATURE)
PAUSED_TOPIC = topic_from_signature(PAUSED_SIGNATURE)
UNPAUSED_TOPIC = topic_from_signature(UNPAUSED_SIGNATURE)

SAFE_RPC_METHODS = {
    "eth_blockNumber",
    "eth_call",
    "eth_chainId",
    "eth_feeHistory",
    "eth_getBlockByHash",
    "eth_getBlockByNumber",
    "eth_getLogs",
    "eth_getTransactionReceipt",
    "eth_syncing",
    "net_version",
    "txpool_content",
    "txpool_status",
    "web3_clientVersion",
}

LOCAL_ONLY_METHODS = {"txpool_content", "txpool_status"}

PROBED_METHODS = (
    "eth_blockNumber",
    "eth_getBlockByNumber",
    "eth_getLogs",
    "eth_getTransactionReceipt",
    "eth_call",
    "eth_feeHistory",
    "eth_syncing",
    "net_version",
    "web3_clientVersion",
    "txpool_content",
    "txpool_status",
)

NEW_BLOCK_CHANNEL = "new_block"
NEW_TRANSFERS_CHANNEL = "new_transfers"
NEW_SWAPS_CHANNEL = "new_swaps"
REORG_CHANNEL = "reorg"
WHALE_TRANSFERS_CHANNEL = "whale_transfers"

DEX_SWAP_TOPICS = [V2_SWAP_TOPIC, V3_SWAP_TOPIC]
# eth_getLogs uses a nested list to express OR matching for topic[0].
DEX_SWAP_TOPIC_FILTER = [DEX_SWAP_TOPICS]
GOVERNANCE_TOPICS = [
    OWNERSHIP_TRANSFERRED_TOPIC,
    UPGRADED_TOPIC,
    PAUSED_TOPIC,
    UNPAUSED_TOPIC,
]

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}

QUOTE_SYMBOLS = {"USDC", "USDT", "DAI", "WETH", "WBTC", "WMATIC", "MATIC", "POL"}

ERC20_METADATA_CALLS = {
    "name": "0x06fdde03",
    "symbol": "0x95d89b41",
    "decimals": "0x313ce567",
    "totalSupply": "0x18160ddd",
}
