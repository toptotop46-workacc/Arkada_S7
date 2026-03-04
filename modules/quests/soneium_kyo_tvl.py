# -*- coding: utf-8 -*-
"""
Квест Soneium Score — Kyo Finance TVL: страница кампании, Verify; при невыполненном квесте —
внести ликвидность в пул ETH/USDC.e на Kyo (0.001–0.01 USDC.e + пропорциональный ETH по цене пула),
сразу вывести, ожидание 10 с, повторный Verify, затем Claim.
Цена ETH и тики берутся из пула (slot0).
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from web3 import Web3

from modules.quests import soneium_velodrome
from modules.quests.constants import (
    PAUSE_MAX,
    PAUSE_MIN,
    PAUSE_SHORT_MAX,
    PAUSE_SHORT_MIN,
    QUEST_UI_ALREADY_DONE_TIMEOUT_MS,
    QUEST_UI_CLAIM_TIMEOUT_MS,
    QUEST_UI_CONTINUE_TIMEOUT_MS,
    QUEST_UI_QUEST_COMPLETED_TIMEOUT_MS,
    QUEST_UI_RETRY_DELAY_MAX,
    QUEST_UI_RETRY_DELAY_MIN,
    QUEST_UI_VERIFY_TIMEOUT_MS,
    QUEST_UI_WAIT_AFTER_GOTO_SEC,
    get_soneium_chain_id,
    get_soneium_rpc_url,
)

from modules.quests.storage import all_quests_already_claimed, save_completed_quest

# RPC и сеть — из конфига, как в остальных квестах
RPC_URL = get_soneium_rpc_url()
CHAIN_ID = get_soneium_chain_id()

# --- URL кампании ---
CAMPAIGN_URLS = [
    "https://app.arkada.gg/en/campaign/soneium-score-seventh-kyo-tvl",
]

# --- Конфиг Soneium / Kyo CL ---
WETH = "0x4200000000000000000000000000000000000006"
USDCE = "0xbA9986D2381edf1DA03B0B9c1f8b00dc4AacC369"
KYO_POSITIONS = "0xAE2B32E603D303ED120f45B4Bc2ebAc314de080b"

POOL_FEE = 3000
TICK_SPACING = 60
USDC_DECIMALS = 6
USDC_AMOUNT_MIN = 0.001
USDC_AMOUNT_MAX = 0.01
# amount0Min/amount1Min: 0 для mint (квест «внести и вывести»), иначе "Price slippage check"
USE_ZERO_SLIPPAGE_MINT = True
SLIPPAGE_BPS = 2000  # на случай если USE_ZERO_SLIPPAGE_MINT = False
MAX_ATTEMPTS = 5
UI_RETRIES_AFTER_ONCHAIN = 5  # повторы только UI (Verify/Claim), без повторного ончейна
SECONDS_AFTER_CONFIRMATION = 10
RPC_TIMEOUT = 60
RPC_RETRIES = 3
RECEIPT_TIMEOUT = 120
# Ретраи для read-only вызовов (пул, slot0) при сбоях RPC
POOL_READ_RETRIES = 3
POOL_READ_RETRY_DELAY = 4

# Минимальные ABI
FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "uint24", "name": "", "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_ABI = [
    {"inputs": [{"internalType": "address", "name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "spender", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

KYO_POSITIONS_ABI = [
    {"inputs": [], "name": "factory", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "token0", "type": "address"},
                    {"internalType": "address", "name": "token1", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "int24", "name": "tickLower", "type": "int24"},
                    {"internalType": "int24", "name": "tickUpper", "type": "int24"},
                    {"internalType": "uint256", "name": "amount0Desired", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount1Desired", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount0Min", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount1Min", "type": "uint256"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                ],
                "internalType": "struct INonfungiblePositionManager.MintParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "mint",
        "outputs": [
            {"internalType": "uint256", "name": "tokenId", "type": "uint256"},
            {"internalType": "uint128", "name": "liquidity", "type": "uint128"},
            {"internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "uint256", "name": "tokenId", "type": "uint256"},
                    {"internalType": "uint128", "name": "liquidity", "type": "uint128"},
                    {"internalType": "uint256", "name": "amount0Min", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount1Min", "type": "uint256"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                ],
                "internalType": "struct INonfungiblePositionManager.DecreaseLiquidityParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "decreaseLiquidity",
        "outputs": [
            {"internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "uint256", "name": "tokenId", "type": "uint256"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint128", "name": "amount0Max", "type": "uint128"},
                    {"internalType": "uint128", "name": "amount1Max", "type": "uint128"},
                ],
                "internalType": "struct INonfungiblePositionManager.CollectParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "collect",
        "outputs": [
            {"internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {"inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}], "name": "burn", "outputs": [], "stateMutability": "payable", "type": "function"},
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"internalType": "uint96", "name": "nonce", "type": "uint96"},
            {"internalType": "address", "name": "operator", "type": "address"},
            {"internalType": "address", "name": "token0", "type": "address"},
            {"internalType": "address", "name": "token1", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
            {"internalType": "int24", "name": "tickLower", "type": "int24"},
            {"internalType": "int24", "name": "tickUpper", "type": "int24"},
            {"internalType": "uint128", "name": "liquidity", "type": "uint128"},
            {"internalType": "uint256", "name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"internalType": "uint256", "name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"internalType": "uint128", "name": "tokensOwed0", "type": "uint128"},
            {"internalType": "uint128", "name": "tokensOwed1", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


def _get_w3() -> Web3:
    return Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))


def _get_usdc_balance(address: str) -> int:
    w3 = _get_w3()
    if not w3.is_connected():
        return 0
    token = w3.eth.contract(address=Web3.to_checksum_address(USDCE), abi=ERC20_ABI)
    return token.functions.balanceOf(Web3.to_checksum_address(address)).call()


def _get_pool_address() -> Optional[str]:
    """Адрес пула WETH/USDC.e (fee=3000). При сбоях RPC — ретраи. Пробует оба порядка токенов."""
    zero = "0x0000000000000000000000000000000000000000"
    token_pairs = [
        (Web3.to_checksum_address(WETH), Web3.to_checksum_address(USDCE)),
        (Web3.to_checksum_address(USDCE), Web3.to_checksum_address(WETH)),
    ]
    last_factory: Optional[str] = None
    last_pool_result: Optional[str] = None
    for attempt in range(1, POOL_READ_RETRIES + 1):
        try:
            w3 = _get_w3()
            if not w3.is_connected():
                if attempt < POOL_READ_RETRIES:
                    time.sleep(POOL_READ_RETRY_DELAY)
                continue
            nft = w3.eth.contract(address=Web3.to_checksum_address(KYO_POSITIONS), abi=KYO_POSITIONS_ABI)
            factory_addr = nft.functions.factory().call()
            last_factory = factory_addr
            factory = w3.eth.contract(address=factory_addr, abi=FACTORY_ABI)
            for token_a, token_b in token_pairs:
                pool = factory.functions.getPool(token_a, token_b, POOL_FEE).call()
                last_pool_result = pool
                if pool and pool != zero:
                    return pool
        except Exception as e:
            logger.debug("_get_pool_address попытка {}: {}", attempt, e)
            if attempt < POOL_READ_RETRIES:
                time.sleep(POOL_READ_RETRY_DELAY)
            continue
        if attempt < POOL_READ_RETRIES:
            time.sleep(POOL_READ_RETRY_DELAY)
    logger.warning(
        "Пул ETH/USDC.e не найден после {} попыток (factory={}, getPool={})",
        POOL_READ_RETRIES,
        last_factory or "?",
        last_pool_result if last_pool_result is not None else "?",
    )
    return None


def _get_slot0(pool_address: str) -> Optional[tuple[int, int]]:
    """Возвращает (sqrt_price_x96, tick) или None. Ретраи при сбоях RPC, логирование ошибок."""
    for attempt in range(1, POOL_READ_RETRIES + 1):
        try:
            w3 = _get_w3()
            if not w3.is_connected():
                if attempt < POOL_READ_RETRIES:
                    time.sleep(POOL_READ_RETRY_DELAY)
                continue
            pool = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=POOL_ABI)
            result = pool.functions.slot0().call()
            return (result[0], result[1])
        except Exception as e:
            logger.debug("_get_slot0 попытка {}: {}", attempt, e)
            if attempt == POOL_READ_RETRIES:
                logger.warning("Не удалось прочитать slot0 пула после {} попыток: {}", POOL_READ_RETRIES, e)
            elif attempt < POOL_READ_RETRIES:
                time.sleep(POOL_READ_RETRY_DELAY)
    return None


def _amount0_from_amount1_and_sqrt_price_x96(amount1_raw: int, sqrt_price_x96: int) -> int:
    """По amount1 (USDC 6 decimals) и sqrtPriceX96 пула считаем amount0 (WETH wei). price = (sqrtPriceX96/2^96)^2 = amount1/amount0 => amount0 = amount1 * 2^192 / sqrtPriceX96^2."""
    if sqrt_price_x96 <= 0:
        return 0
    return (amount1_raw * (1 << 192)) // (sqrt_price_x96 * sqrt_price_x96)


def _tick_range_from_current(current_tick: int) -> tuple[int, int]:
    """tickLower и tickUpper вокруг текущего тика (tickSpacing = 60)."""
    base = (current_tick // TICK_SPACING) * TICK_SPACING
    return (base - TICK_SPACING, base + TICK_SPACING)


def _do_approve_usdc(private_key: str, amount: int) -> Optional[str]:
    retriable = (ConnectionError, OSError, TimeoutError)
    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = _get_w3()
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if rpc_attempt < RPC_RETRIES:
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                logger.warning("RPC недоступен или неверный chain_id после {} попыток", RPC_RETRIES)
                return None
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            token = w3.eth.contract(address=Web3.to_checksum_address(USDCE), abi=ERC20_ABI)
            max_u256 = (1 << 256) - 1
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = gas_price, gas_price // 10
            except Exception:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = None, None
            tx_params: dict[str, Any] = {"chainId": CHAIN_ID, "from": wallet, "nonce": nonce}
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = gas_price
            try:
                gas_estimate = token.functions.approve(Web3.to_checksum_address(KYO_POSITIONS), max_u256).estimate_gas({"from": wallet})
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception:
                tx_params["gas"] = 100000
            tx = token.functions.approve(Web3.to_checksum_address(KYO_POSITIONS), max_u256).build_transaction(tx_params)
            signed = account.sign_transaction(tx)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            if raw_tx is None:
                try:
                    raw_tx = signed["rawTransaction"] or signed["raw_transaction"]
                except (TypeError, KeyError):
                    pass
            if not raw_tx:
                return None
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            tx_hex = tx_hash.hex()
            logger.info("Транзакция approve USDC.e отправлена: https://soneium.blockscout.com/tx/{}", tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
            if receipt["status"] == 1:
                return tx_hex
            return None
        except retriable:
            if rpc_attempt < RPC_RETRIES:
                time.sleep(5 + (rpc_attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка approve USDC.e: {}", e)
            return None
    return None


def _do_mint(
    private_key: str,
    amount0_wei: int,
    amount1_raw: int,
    tick_lower: int,
    tick_upper: int,
) -> Optional[tuple[int, int]]:
    """Mint позиции. Возвращает (tokenId, liquidity) или None."""
    retriable = (ConnectionError, OSError, TimeoutError)
    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = _get_w3()
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if rpc_attempt < RPC_RETRIES:
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                return None
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            nft = w3.eth.contract(address=Web3.to_checksum_address(KYO_POSITIONS), abi=KYO_POSITIONS_ABI)
            if USE_ZERO_SLIPPAGE_MINT:
                amount0_min = 0
                amount1_min = 0
            else:
                amount0_min = int(amount0_wei * (10000 - SLIPPAGE_BPS) / 10000)
                amount1_min = int(amount1_raw * (10000 - SLIPPAGE_BPS) / 10000)
            deadline = int(time.time()) + 600
            params = (
                Web3.to_checksum_address(WETH),
                Web3.to_checksum_address(USDCE),
                POOL_FEE,
                tick_lower,
                tick_upper,
                amount0_wei,
                amount1_raw,
                amount0_min,
                amount1_min,
                Web3.to_checksum_address(wallet),
                deadline,
            )
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = gas_price, gas_price // 10
            except Exception:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = None, None
            tx_params: dict[str, Any] = {"chainId": CHAIN_ID, "from": wallet, "nonce": nonce, "value": amount0_wei}
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = gas_price
            try:
                gas_estimate = nft.functions.mint(params).estimate_gas({"value": amount0_wei, **tx_params})
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception as e:
                logger.debug("estimate_gas mint: {}", e)
                tx_params["gas"] = 600000
            tx = nft.functions.mint(params).build_transaction(tx_params)
            signed = account.sign_transaction(tx)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            if raw_tx is None:
                try:
                    raw_tx = signed["rawTransaction"] or signed["raw_transaction"]
                except (TypeError, KeyError):
                    pass
            if not raw_tx:
                return None
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            tx_hex = tx_hash.hex()
            logger.info("Транзакция mint (добавление ликвидности) отправлена: https://soneium.blockscout.com/tx/{}", tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
            if receipt["status"] != 1:
                return None
            # Извлекаем tokenId из события Transfer (ERC721: from, to, tokenId — все indexed)
            transfer_topic = w3.keccak(text="Transfer(address,address,uint256)")
            kyo_lower = Web3.to_checksum_address(KYO_POSITIONS).lower()
            token_id = None
            for log in receipt.get("logs") or []:
                if (log.get("address") or "").lower() != kyo_lower:
                    continue
                topics = log.get("topics") or []
                if len(topics) >= 4 and topics[0] == transfer_topic:
                    token_id = int.from_bytes(topics[3], "big")
                    break
            if token_id is None:
                return None
            pos = nft.functions.positions(token_id).call()
            liquidity = pos[7]
            logger.info("Позиция создана: tokenId={}, liquidity={}", token_id, liquidity)
            return (token_id, liquidity)
        except retriable:
            if rpc_attempt < RPC_RETRIES:
                time.sleep(5 + (rpc_attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка mint: {}", e)
            return None
    return None


def _do_decrease_and_collect_and_burn(private_key: str, token_id: int, liquidity: int) -> bool:
    retriable = (ConnectionError, OSError, TimeoutError)
    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = _get_w3()
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if rpc_attempt < RPC_RETRIES:
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                return False
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            nft = w3.eth.contract(address=Web3.to_checksum_address(KYO_POSITIONS), abi=KYO_POSITIONS_ABI)
            deadline = int(time.time()) + 600
            max_u128 = (1 << 128) - 1

            # decreaseLiquidity
            dec_params = (token_id, liquidity, 0, 0, deadline)
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = gas_price, gas_price // 10
            except Exception:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = None, None
            tx_params: dict[str, Any] = {"chainId": CHAIN_ID, "from": wallet, "nonce": nonce}
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = gas_price
            tx_params["gas"] = 400000
            tx = nft.functions.decreaseLiquidity(dec_params).build_transaction(tx_params)
            signed = account.sign_transaction(tx)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            if raw_tx is None:
                try:
                    raw_tx = signed["rawTransaction"] or signed["raw_transaction"]
                except (TypeError, KeyError):
                    pass
            if not raw_tx:
                return False
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
            if receipt["status"] != 1:
                logger.warning("decreaseLiquidity откатился")
                return False
            nonce += 1

            # collect
            collect_params = (token_id, Web3.to_checksum_address(wallet), max_u128, max_u128)
            tx_params["nonce"] = nonce
            tx = nft.functions.collect(collect_params).build_transaction(tx_params)
            signed = account.sign_transaction(tx)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            if raw_tx is None:
                try:
                    raw_tx = signed["rawTransaction"] or signed["raw_transaction"]
                except (TypeError, KeyError):
                    pass
            if not raw_tx:
                return False
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
            if receipt["status"] != 1:
                logger.warning("collect откатился")
                return False
            nonce += 1

            # burn
            tx_params["nonce"] = nonce
            tx = nft.functions.burn(token_id).build_transaction(tx_params)
            signed = account.sign_transaction(tx)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            if raw_tx is None:
                try:
                    raw_tx = signed["rawTransaction"] or signed["raw_transaction"]
                except (TypeError, KeyError):
                    pass
            if not raw_tx:
                return False
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
            if receipt["status"] == 1:
                logger.info("Вывод ликвидности и burn выполнены")
                return True
            return False
        except retriable:
            if rpc_attempt < RPC_RETRIES:
                time.sleep(5 + (rpc_attempt - 1) * 3)
            else:
                return False
        except Exception as e:
            logger.warning("Ошибка при выводе ликвидности: {}", e)
            return False
    return False


def _do_deposit_and_withdraw_flow(private_key: str, wallet: str) -> bool:
    """Проверка USDC.e → при необходимости свап → approve → mint → decrease + collect + burn. Возвращает True при успехе."""
    pool_addr = _get_pool_address()
    if not pool_addr:
        return False
    slot = _get_slot0(pool_addr)
    if not slot:
        return False
    sqrt_price_x96, current_tick = slot
    tick_lower, tick_upper = _tick_range_from_current(current_tick)

    amount_usdc = random.uniform(USDC_AMOUNT_MIN, USDC_AMOUNT_MAX)
    amount1_raw = int(amount_usdc * (10**USDC_DECIMALS))
    if amount1_raw <= 0:
        amount1_raw = int(USDC_AMOUNT_MIN * (10**USDC_DECIMALS))

    balance_usdc = _get_usdc_balance(wallet)
    if balance_usdc < amount1_raw:
        logger.info("Недостаточно USDC.e (есть {}), выполняем свап через Velodrome", balance_usdc)
        tx_swap = soneium_velodrome._swap_eth_to_usdc(private_key, None, None)
        if not tx_swap:
            logger.warning("Свап ETH→USDC.e не удался")
            return False
        pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
        logger.info("Пауза {:.0f} с", pause)
        time.sleep(pause)
        balance_usdc = _get_usdc_balance(wallet)
        if balance_usdc < amount1_raw:
            amount1_raw = balance_usdc
        if amount1_raw <= 0:
            logger.warning("После свапа USDC.e на кошельке 0")
            return False

    amount0_wei = _amount0_from_amount1_and_sqrt_price_x96(amount1_raw, sqrt_price_x96)
    if amount0_wei <= 0:
        logger.warning("Рассчитанный amount0 (ETH) = 0")
        return False

    amount_usdc_str = f"{amount_usdc:.4f}".rstrip("0").rstrip(".")
    logger.info("Вносим ликвидность: ~{} USDC.e + пропорциональный ETH (цена из пула)", amount_usdc_str)

    if _do_approve_usdc(private_key, amount1_raw) is None:
        return False
    time.sleep(random.uniform(PAUSE_SHORT_MIN, PAUSE_SHORT_MAX))
    result = _do_mint(private_key, amount0_wei, amount1_raw, tick_lower, tick_upper)
    if not result:
        return False
    pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
    logger.info("Пауза {:.0f} с", pause)
    time.sleep(pause)
    token_id, liquidity = result
    if not _do_decrease_and_collect_and_burn(private_key, token_id, liquidity):
        return False
    return True


async def run(
    page: Any,
    wallet_address: str,
    confirm_claim_in_rabby: Callable[[Any], Awaitable[None]],
    private_key: str,
) -> None:
    if not CAMPAIGN_URLS:
        return
    url = CAMPAIGN_URLS[0]
    name = url.rstrip("/").split("/")[-1] or url
    if all_quests_already_claimed(wallet_address, [name]):
        logger.success("Квест {}: уже выполнен", name)
        return
    logger.info("Квест Kyo TVL: {}", name)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(QUEST_UI_WAIT_AFTER_GOTO_SEC)
    except Exception as e:
        logger.warning("Квест {}: не удалось открыть страницу — {}", name, e)
        return

    already_done = page.get_by_text("Congratulations").or_(
        page.get_by_role("button", name="Go to next quest")
    )
    try:
        await already_done.first.wait_for(state="visible", timeout=QUEST_UI_ALREADY_DONE_TIMEOUT_MS)
        logger.success("Квест {}: уже выполнен ранее", name)
        save_completed_quest(wallet_address, name, "already_claimed")
        go_next = page.get_by_role("button", name="Go to next quest")
        if await go_next.is_visible():
            await go_next.click()
        return
    except Exception:
        pass

    claim_btn = page.get_by_role("button", name="Claim Reward")
    try:
        await claim_btn.wait_for(state="visible", timeout=8000)
        logger.info("Квест {}: награда не заклаймлена — забираем", name)
        await claim_btn.click()
        await confirm_claim_in_rabby(page)
        continue_btn = page.get_by_role("button", name="Continue")
        await continue_btn.wait_for(state="visible", timeout=QUEST_UI_CONTINUE_TIMEOUT_MS)
        await continue_btn.click()
        logger.success("Квест {}: награда забрана", name)
        save_completed_quest(wallet_address, name, "reward_claimed")
        return
    except Exception:
        pass

    verify_btn = page.get_by_role("button", name="Verify")
    try:
        await verify_btn.wait_for(state="visible", timeout=QUEST_UI_VERIFY_TIMEOUT_MS)
    except Exception:
        try:
            await already_done.first.wait_for(state="visible", timeout=3000)
            logger.success("Квест {}: уже выполнен ранее", name)
            save_completed_quest(wallet_address, name, "already_claimed")
            go_next = page.get_by_role("button", name="Go to next quest")
            if await go_next.is_visible():
                await go_next.click()
        except Exception:
            logger.warning("Квест {}: кнопка Verify не появилась", name)
        return

    await verify_btn.click()
    await asyncio.sleep(3)
    completed = page.get_by_text("Quest completed")
    not_completed = page.get_by_text("Quest not completed")
    try:
        await completed.wait_for(state="visible", timeout=QUEST_UI_QUEST_COMPLETED_TIMEOUT_MS)
        claim_btn = page.get_by_role("button", name="Claim Reward")
        await claim_btn.wait_for(state="visible", timeout=QUEST_UI_CLAIM_TIMEOUT_MS)
        await claim_btn.click()
        await confirm_claim_in_rabby(page)
        continue_btn = page.get_by_role("button", name="Continue")
        await continue_btn.wait_for(state="visible", timeout=QUEST_UI_CONTINUE_TIMEOUT_MS)
        await continue_btn.click()
        logger.success("Квест {}: награда забрана после Verify", name)
        save_completed_quest(wallet_address, name, "verified_and_claimed")
        return
    except Exception:
        pass

    try:
        await not_completed.wait_for(state="visible", timeout=5000)
        alert_btn = page.locator("role=alert").get_by_role("button").first
        if await alert_btn.is_visible():
            await alert_btn.click()
    except Exception:
        pass
    logger.info("Квест {}: не выполнен — вносим ликвидность в Kyo ETH/USDC.e (до {} попыток ончейна)", name, MAX_ATTEMPTS)

    on_chain_done = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if not on_chain_done:
            logger.info("Попытка ончейна {}/{}", attempt, MAX_ATTEMPTS)
            ok = await asyncio.to_thread(_do_deposit_and_withdraw_flow, private_key, wallet_address)
            if not ok:
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(random.uniform(QUEST_UI_RETRY_DELAY_MIN, QUEST_UI_RETRY_DELAY_MAX))
                continue
            on_chain_done = True

        logger.info("Ожидание {} с после подтверждения", SECONDS_AFTER_CONFIRMATION)
        await asyncio.sleep(SECONDS_AFTER_CONFIRMATION)

        for ui_retry in range(1, UI_RETRIES_AFTER_ONCHAIN + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(QUEST_UI_WAIT_AFTER_GOTO_SEC)
                verify_btn2 = page.get_by_role("button", name="Verify")
                await verify_btn2.wait_for(state="visible", timeout=QUEST_UI_VERIFY_TIMEOUT_MS)
                await verify_btn2.click()
                await asyncio.sleep(3)
                completed2 = page.get_by_text("Quest completed")
                await completed2.wait_for(state="visible", timeout=QUEST_UI_QUEST_COMPLETED_TIMEOUT_MS)
                claim_btn = page.get_by_role("button", name="Claim Reward")
                await claim_btn.wait_for(state="visible", timeout=QUEST_UI_CLAIM_TIMEOUT_MS)
                await claim_btn.click()
                await confirm_claim_in_rabby(page)
                continue_btn = page.get_by_role("button", name="Continue")
                await continue_btn.wait_for(state="visible", timeout=QUEST_UI_CONTINUE_TIMEOUT_MS)
                await continue_btn.click()
                logger.success("Квест {}: ликвидность внесена, награда забрана", name)
                save_completed_quest(wallet_address, name, "verified_and_claimed")
                return
            except Exception as e:
                logger.warning("После внесения ликвидности Verify/Claim (попытка UI {}/{}): {}", ui_retry, UI_RETRIES_AFTER_ONCHAIN, e)
                if ui_retry < UI_RETRIES_AFTER_ONCHAIN:
                    await asyncio.sleep(random.uniform(QUEST_UI_RETRY_DELAY_MIN, QUEST_UI_RETRY_DELAY_MAX))

        if on_chain_done:
            logger.warning("Квест {}: ончейн выполнен, но Verify/Claim не удался после {} попыток UI — новых ончейн-транзакций не делаем", name, UI_RETRIES_AFTER_ONCHAIN)
            return

    logger.warning("Квест {}: не удалось выполнить ончейн за {} попыток", name, MAX_ATTEMPTS)
