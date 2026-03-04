# -*- coding: utf-8 -*-
"""
Квест Soneium Score — Sake Finance Deposit: страница кампании, Verify; при невыполненном квесте —
проверка баланса ≥24$ в ETH (целевой с запасом 25$), затем: свап ETH→aSuperUSD (LI.FI), при необходимости ETH→USDC.e,
approve/deposit aSuperUSD, borrow 10 USDC.e, approve/repay, withdraw, свапы sSuperUSD→USDC.e и USDC.e→ETH (LI.FI).
Паузы 10–30 с между шагами, кроме сразу после апрувов и между LI.FI-свапами (короткая пауза).
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Optional

import requests
from loguru import logger
from web3 import Web3

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
from modules.quests.funding import ensure_soneium_balance_for_quest
from modules.quests.storage import all_quests_already_claimed, save_completed_quest

# --- Конфиг Soneium / Sake Finance ---
RPC_URL = get_soneium_rpc_url()
CHAIN_ID = get_soneium_chain_id()
NATIVE_ETH = "0x0000000000000000000000000000000000000000"
USDCE = "0xbA9986D2381edf1DA03B0B9c1f8b00dc4AacC369"
A_SUPER_USD = "0x139450C2dCeF827C9A2a0Bb1CB5506260940c9fd"
POOL = "0x3C3987A310ee13F7B8cBBe21D97D4436ba5E4B5f"

MIN_BALANCE_USD = 24.0  # минимум для выполнения квеста; с запасом 1$ целевой баланс = 25$
RESERVE_USD = 1.0
SWAP_ETH_MIN = 0.01131
SWAP_ETH_MAX = 0.012
SWAP_USDCE_ETH_MIN = 0.00001001
SWAP_USDCE_ETH_MAX = 0.00002
BORROW_USDCE_AMOUNT = 10_000_000  # 10 USDC.e (6 decimals)
INTEREST_RATE_MODE = 2  # variable
SECONDS_AFTER_CONFIRMATION = 10
RPC_TIMEOUT = 60
RPC_RETRIES = 3
MAX_UINT256 = 2**256 - 1

# LI.FI
LI_FI_API_BASE = "https://li.quest/v1"
LI_FI_API_KEY = "aeaa4f26-c3c3-4b71-aad3-50bd82faf815.1e83cb78-2d75-412d-a310-57272fd0e622"
LI_FI_INTEGRATOR = "Soneium"
LI_FI_FEE_PERCENTAGE = "0.005"
LI_FI_SLIPPAGE = "0.05"
LI_FI_RATE_LIMIT_INTERVAL = 0.3
LI_FI_QUOTE_RETRIES = 2  # повторов при 404/5xx (всего 1 + RETRIES запросов)
LI_FI_QUOTE_RETRY_DELAY = 8  # секунд между попытками
# Резерв на газ для первого свапа ETH→aSuperUSD: не отправляем в LI.FI больше (balance - reserve)
LI_FI_ETH_SWAP_GAS_LIMIT = 500_000  # ожидаемый gas для LI.FI tx
LI_FI_ETH_SWAP_GAS_RESERVE_MULTIPLIER = 1.5  # запас на рост gas price

CAMPAIGN_URLS = [
    "https://app.arkada.gg/en/campaign/soneium-score-seventh-sake-deposit",
]

ERC20_ABI = [
    {"inputs": [{"internalType": "address", "name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "owner", "type": "address"}, {"internalType": "address", "name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "spender", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

POOL_ABI = [
    {"inputs": [{"internalType": "address", "name": "asset", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}, {"internalType": "address", "name": "onBehalfOf", "type": "address"}, {"internalType": "uint16", "name": "referralCode", "type": "uint16"}], "name": "supply", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "asset", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}, {"internalType": "uint256", "name": "interestRateMode", "type": "uint256"}, {"internalType": "uint16", "name": "referralCode", "type": "uint16"}, {"internalType": "address", "name": "onBehalfOf", "type": "address"}], "name": "borrow", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "asset", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}, {"internalType": "uint256", "name": "interestRateMode", "type": "uint256"}, {"internalType": "address", "name": "onBehalfOf", "type": "address"}], "name": "repay", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "asset", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}, {"internalType": "address", "name": "to", "type": "address"}], "name": "withdraw", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

_lifi_last_request_time = 0.0


def _is_nonce_too_low(err: Any) -> bool:
    """Проверяет, что ошибка RPC — «nonce too low» (повторная отправка с новым nonce)."""
    if isinstance(err, dict):
        msg = str(err.get("message", err))
    else:
        msg = getattr(err, "message", "") or str(err)
    return "nonce too low" in msg.lower()


def _lifi_rate_limit() -> None:
    global _lifi_last_request_time
    now = time.monotonic()
    elapsed = now - _lifi_last_request_time
    if elapsed < LI_FI_RATE_LIMIT_INTERVAL:
        time.sleep(LI_FI_RATE_LIMIT_INTERVAL - elapsed)
    _lifi_last_request_time = time.monotonic()


def _get_eth_price_usd() -> float:
    """Цена 1 ETH в USD через LI.FI (1 ETH -> USDC.e). При ошибке — fallback CoinGecko. Возвращает 0.0 при полном провале."""
    _lifi_rate_limit()
    one_eth = 10**18
    params = {
        "fromChain": CHAIN_ID,
        "toChain": CHAIN_ID,
        "fromToken": NATIVE_ETH,
        "toToken": USDCE,
        "fromAmount": str(one_eth),
        "fromAddress": "0x0000000000000000000000000000000000000001",
        "slippage": LI_FI_SLIPPAGE,
        "integrator": LI_FI_INTEGRATOR,
        "fee": LI_FI_FEE_PERCENTAGE,
    }
    headers = {"x-lifi-api-key": LI_FI_API_KEY, "User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(f"{LI_FI_API_BASE}/quote", params=params, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        est = data.get("estimate") or {}
        to_amount = est.get("toAmount")
        if not to_amount and data.get("action"):
            to_amount = data["action"].get("toAmount")
        if to_amount is not None:
            return int(to_amount) / 1e6
    except Exception as e:
        logger.debug("Ошибка получения цены ETH через LI.FI: {}", e)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
            headers={"User-Agent": "Arkada-S7"},
            timeout=10,
        )
        if r.status_code == 200:
            j = r.json()
            price = (j.get("ethereum") or {}).get("usd")
            if isinstance(price, (int, float)) and price > 0:
                return float(price)
    except Exception as e:
        logger.debug("CoinGecko fallback цены ETH: {}", e)
    return 0.0


def _get_lifi_quote(
    from_token: str,
    to_token: str,
    from_amount_wei: int,
    from_address: str,
) -> Optional[dict]:
    """
    Получить котировку LI.FI. Возвращает полный ответ API (data), чтобы извлечь
    transactionRequest и estimate.approvalAddress для апрува ERC20 перед свапом.
    При 404/5xx повторяет запрос до LI_FI_QUOTE_RETRIES раз с паузой.
    """
    params = {
        "fromChain": CHAIN_ID,
        "toChain": CHAIN_ID,
        "fromToken": from_token,
        "toToken": to_token,
        "fromAmount": str(from_amount_wei),
        "fromAddress": from_address,
        "slippage": LI_FI_SLIPPAGE,
        "integrator": LI_FI_INTEGRATOR,
        "fee": LI_FI_FEE_PERCENTAGE,
    }
    headers = {"x-lifi-api-key": LI_FI_API_KEY, "User-Agent": "Mozilla/5.0"}
    max_attempts = 1 + LI_FI_QUOTE_RETRIES

    for attempt in range(1, max_attempts + 1):
        _lifi_rate_limit()
        try:
            r = requests.get(
                f"{LI_FI_API_BASE}/quote",
                params=params,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as e:
            logger.warning("Ошибка LI.FI quote (попытка {}): {}", attempt, e)
            if attempt == max_attempts:
                return None
            time.sleep(LI_FI_QUOTE_RETRY_DELAY)
            continue

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception as e:
                logger.warning("LI.FI quote: неверный JSON (попытка {}): {}", attempt, e)
                if attempt == max_attempts:
                    return None
                time.sleep(LI_FI_QUOTE_RETRY_DELAY)
                continue
            req = data.get("transactionRequest") or (data.get("action") and data["action"].get("transactionRequest"))
            if not req or not req.get("to") or not req.get("data"):
                logger.warning("LI.FI не вернул transactionRequest")
                return None
            data["_transaction_request"] = req
            return data

        if r.status_code == 404 or r.status_code >= 500:
            body_preview = (r.text or "")[:300]
            logger.warning(
                "LI.FI quote: HTTP {} (попытка {}/{}), ответ: {}",
                r.status_code,
                attempt,
                max_attempts,
                body_preview,
            )
            if attempt == max_attempts:
                return None
            time.sleep(LI_FI_QUOTE_RETRY_DELAY)
            continue

        logger.warning("Ошибка LI.FI quote: HTTP {}", r.status_code)
        return None

    return None


def _execute_lifi_tx(private_key: str, transaction_request: dict) -> Optional[str]:
    """Отправить транзакцию из LI.FI transactionRequest. Возвращает tx hash или None."""
    retriable = (ConnectionError, OSError, TimeoutError)
    for attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if attempt < RPC_RETRIES:
                    time.sleep(5 + (attempt - 1) * 3)
                    continue
                return None
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            to = Web3.to_checksum_address(transaction_request["to"])
            data = transaction_request["data"]
            if isinstance(data, bytes):
                data = "0x" + data.hex()
            raw_val = transaction_request.get("value")
            if isinstance(raw_val, str):
                value = int(raw_val, 16) if raw_val.startswith("0x") else int(raw_val)
            else:
                value = int(raw_val or 0)
            gas_limit = transaction_request.get("gasLimit")
            gas_int = int(gas_limit, 16) if isinstance(gas_limit, str) else (int(gas_limit) * 2 if gas_limit else 500000)
            tx_hash = None
            for nonce_attempt in range(3):
                try:
                    nonce = w3.eth.get_transaction_count(wallet, "pending")
                    try:
                        gas_price = w3.eth.gas_price
                        max_fee, max_priority = gas_price, gas_price // 10
                    except Exception:
                        max_fee, max_priority = None, None
                    tx_params: dict[str, Any] = {"chainId": CHAIN_ID, "from": wallet, "nonce": nonce, "to": to, "data": data, "value": value, "gas": gas_int}
                    if max_fee is not None:
                        tx_params["maxFeePerGas"] = max_fee
                        tx_params["maxPriorityFeePerGas"] = max_priority
                    else:
                        tx_params["gasPrice"] = w3.eth.gas_price
                    signed = account.sign_transaction(tx_params)
                    raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
                    if raw_tx is None:
                        try:
                            raw_tx = signed["rawTransaction"] or signed["raw_transaction"]
                        except (TypeError, KeyError):
                            pass
                    if not raw_tx:
                        return None
                    tx_hash = w3.eth.send_raw_transaction(raw_tx)
                    break
                except Exception as send_err:
                    if nonce_attempt < 2 and _is_nonce_too_low(send_err):
                        logger.debug("nonce too low, повтор с новым nonce (LI.FI tx)...")
                        time.sleep(1)
                        continue
                    raise
            if tx_hash is None:
                return None
            tx_hex = tx_hash.hex()
            logger.info("LI.FI tx отправлена: https://soneium.blockscout.com/tx/{}", tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex)
            receipt = None
            for _ in range(3):
                try:
                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
                    break
                except retriable:
                    time.sleep(5)
            if receipt is None or receipt.get("status") != 1:
                return None
            return tx_hex
        except retriable:
            if tx_hash is not None:
                return None
            if attempt < RPC_RETRIES:
                time.sleep(5 + (attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка выполнения LI.FI tx: {}", e)
            return None
    return None


def _get_balance_eth(address: str) -> float:
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return 0.0
    balance_wei = w3.eth.get_balance(Web3.to_checksum_address(address))
    return float(Web3.from_wei(balance_wei, "ether"))


def _get_balance_eth_wei(address: str) -> int:
    """Баланс ETH в wei. При ошибке — 0."""
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return 0
    return w3.eth.get_balance(Web3.to_checksum_address(address))


def _get_gas_reserve_wei_for_lifi_swap() -> int:
    """Ориентировочный резерв wei на газ для одной LI.FI tx (gas_limit * gas_price * multiplier)."""
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return int(Web3.to_wei(0.0005, "ether"))  # fallback ~0.0005 ETH
    try:
        gas_price = w3.eth.gas_price
    except Exception:
        return int(Web3.to_wei(0.0005, "ether"))
    return int(LI_FI_ETH_SWAP_GAS_LIMIT * gas_price * LI_FI_ETH_SWAP_GAS_RESERVE_MULTIPLIER)


def _get_token_balance(token: str, address: str) -> int:
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
    if not w3.is_connected():
        return 0
    contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    return contract.functions.balanceOf(Web3.to_checksum_address(address)).call()


def _approve(private_key: str, token: str, spender: str, amount: int) -> Optional[str]:
    retriable = (ConnectionError, OSError, TimeoutError)
    for attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if attempt < RPC_RETRIES:
                    time.sleep(5 + (attempt - 1) * 3)
                    continue
                return None
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            allowance = contract.functions.allowance(wallet, Web3.to_checksum_address(spender)).call()
            if allowance >= amount:
                return "skip"
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = gas_price, gas_price // 10
            except Exception:
                max_fee, max_priority = None, None
            tx_params: dict[str, Any] = {"chainId": CHAIN_ID, "from": wallet, "nonce": nonce}
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = w3.eth.gas_price
            try:
                gas_estimate = contract.functions.approve(Web3.to_checksum_address(spender), amount).estimate_gas({"from": wallet})
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception:
                tx_params["gas"] = 80000
            tx = contract.functions.approve(Web3.to_checksum_address(spender), amount).build_transaction(tx_params)
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
            logger.info("Approve отправлен: https://soneium.blockscout.com/tx/{}", tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            if receipt["status"] == 1:
                return tx_hex
            return None
        except retriable:
            if attempt < RPC_RETRIES:
                time.sleep(5 + (attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка approve: {}", e)
            return None
    return None


def _supply(private_key: str, amount: int) -> Optional[str]:
    return _pool_call(private_key, "supply", [Web3.to_checksum_address(A_SUPER_USD), amount, None, 0])


def _borrow(private_key: str) -> Optional[str]:
    return _pool_call(private_key, "borrow", [Web3.to_checksum_address(USDCE), BORROW_USDCE_AMOUNT, INTEREST_RATE_MODE, 0, None])


def _repay(private_key: str, amount: int) -> Optional[str]:
    return _pool_call(private_key, "repay", [Web3.to_checksum_address(USDCE), amount, INTEREST_RATE_MODE, None])


def _withdraw(private_key: str, amount: int) -> Optional[str]:
    return _pool_call(private_key, "withdraw", [Web3.to_checksum_address(A_SUPER_USD), amount, None])


def _pool_call(private_key: str, method: str, args: list) -> Optional[str]:
    """Общая отправка вызова к Pool. args: список аргументов, None заменяется на wallet."""
    retriable = (ConnectionError, OSError, TimeoutError)
    for attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if attempt < RPC_RETRIES:
                    time.sleep(5 + (attempt - 1) * 3)
                    continue
                return None
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            resolved = [wallet if a is None else a for a in args]
            pool = w3.eth.contract(address=Web3.to_checksum_address(POOL), abi=POOL_ABI)
            fn = getattr(pool.functions, method)(*resolved)
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = gas_price, gas_price // 10
            except Exception:
                max_fee, max_priority = None, None
            tx_params: dict[str, Any] = {"chainId": CHAIN_ID, "from": wallet, "nonce": nonce}
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = w3.eth.gas_price
            try:
                tx_params["gas"] = int(fn.estimate_gas({"from": wallet}) * 1.2)
            except Exception:
                tx_params["gas"] = 350000
            tx = fn.build_transaction(tx_params)
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
            logger.info("{} отправлен: https://soneium.blockscout.com/tx/{}", method, tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            if receipt["status"] == 1:
                return tx_hex
            return None
        except retriable:
            if attempt < RPC_RETRIES:
                time.sleep(5 + (attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка {}: {}", method, e)
            return None
    return None


def _do_onchain_steps(private_key: str, wallet_address: str) -> bool:
    """
    Выполняет шаги 1–10. Паузы: 10–30 с между шагами, кроме короткой (1–3 с) после апрувов и между LI.FI-свапами.
    Возвращает True при полном успехе.
    """
    # 1) Свап ETH → aSuperUSD (0.01131–0.012 ETH); сумма не больше (balance − резерв на газ)
    amount_eth = random.uniform(SWAP_ETH_MIN, SWAP_ETH_MAX)
    amount_wei = int(Web3.to_wei(amount_eth, "ether"))
    balance_wei = _get_balance_eth_wei(wallet_address)
    gas_reserve_wei = _get_gas_reserve_wei_for_lifi_swap()
    amount_wei_capped = min(amount_wei, max(0, balance_wei - gas_reserve_wei))
    min_swap_wei = int(Web3.to_wei(SWAP_ETH_MIN, "ether"))
    if amount_wei_capped < min_swap_wei:
        logger.warning(
            "Недостаточно ETH для первого свапа с резервом на газ: баланс {} wei, резерв {} wei, нужно не менее {} wei",
            balance_wei,
            gas_reserve_wei,
            min_swap_wei,
        )
        return False
    if amount_wei_capped < amount_wei:
        logger.info(
            "Сумма первого свапа ограничена резервом на газ: {} wei (было {} wei)",
            amount_wei_capped,
            amount_wei,
        )
    quote = _get_lifi_quote(NATIVE_ETH, A_SUPER_USD, amount_wei_capped, wallet_address)
    if not quote:
        logger.warning("LI.FI котировка ETH→aSuperUSD недоступна")
        return False
    if _execute_lifi_tx(private_key, quote["_transaction_request"]) is None:
        return False
    pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
    logger.info("Пауза {:.0f} с", pause)
    time.sleep(pause)

    # 2) Если нет USDC.e — свап 0.00001001–0.00002 ETH → USDC.e
    balance_usdce = _get_token_balance(USDCE, wallet_address)
    if balance_usdce == 0:
        amount_eth2 = random.uniform(SWAP_USDCE_ETH_MIN, SWAP_USDCE_ETH_MAX)
        amount_wei2 = int(Web3.to_wei(amount_eth2, "ether"))
        quote2 = _get_lifi_quote(NATIVE_ETH, USDCE, amount_wei2, wallet_address)
        if not quote2:
            logger.warning("LI.FI котировка ETH→USDC.e недоступна")
            return False
        if _execute_lifi_tx(private_key, quote2["_transaction_request"]) is None:
            return False
        pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
        logger.info("Пауза {:.0f} с", pause)
        time.sleep(pause)

    # 3) Approve aSuperUSD → Pool (ждём появления баланса после свапа — индексер может отставать)
    balance_asuper = 0
    for _ in range(5):
        balance_asuper = _get_token_balance(A_SUPER_USD, wallet_address)
        if balance_asuper > 0:
            break
        time.sleep(3)
    if balance_asuper == 0:
        logger.warning("Нет aSuperUSD после свапа (проверяли 5 раз с паузой 3 с)")
        return False
    if _approve(private_key, A_SUPER_USD, POOL, MAX_UINT256) is None:
        return False
    time.sleep(random.uniform(PAUSE_SHORT_MIN, PAUSE_SHORT_MAX))

    # 4) Supply aSuperUSD
    if _supply(private_key, balance_asuper) is None:
        return False
    pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
    logger.info("Пауза {:.0f} с", pause)
    time.sleep(pause)

    # 5) Borrow 10 USDC.e
    if _borrow(private_key) is None:
        return False
    pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
    logger.info("Пауза {:.0f} с", pause)
    time.sleep(pause)

    # 6) Approve USDC.e для repay
    if _approve(private_key, USDCE, POOL, MAX_UINT256) is None:
        return False
    time.sleep(random.uniform(PAUSE_SHORT_MIN, PAUSE_SHORT_MAX))

    # 7) Repay весь долг
    if _repay(private_key, MAX_UINT256) is None:
        return False
    pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
    logger.info("Пауза {:.0f} с", pause)
    time.sleep(pause)

    # 8) Withdraw весь aSuperUSD (получаем обратно aSuperUSD)
    if _withdraw(private_key, MAX_UINT256) is None:
        return False
    pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
    logger.info("Пауза {:.0f} с", pause)
    time.sleep(pause)

    # 9) Свап весь aSuperUSD → USDC.e (перед LI.FI нужен approve токена на роутер)
    balance_asuper2 = _get_token_balance(A_SUPER_USD, wallet_address)
    if balance_asuper2 > 0:
        quote9 = _get_lifi_quote(A_SUPER_USD, USDCE, balance_asuper2, wallet_address)
        if quote9:
            req9 = quote9["_transaction_request"]
            est9 = quote9.get("estimate") or {}
            approval_addr = est9.get("approvalAddress")
            if not approval_addr and quote9.get("includedSteps"):
                est9 = quote9["includedSteps"][0].get("estimate") or {}
                approval_addr = est9.get("approvalAddress")
            if approval_addr:
                if _approve(private_key, A_SUPER_USD, approval_addr, balance_asuper2) is None:
                    logger.warning("Approve aSuperUSD для LI.FI не выполнен")
                else:
                    time.sleep(random.uniform(PAUSE_SHORT_MIN, PAUSE_SHORT_MAX))
            if _execute_lifi_tx(private_key, req9):
                time.sleep(random.uniform(PAUSE_SHORT_MIN, PAUSE_SHORT_MAX))
            else:
                logger.warning("Свап aSuperUSD→USDC.e не выполнен")

    # 10) Свап весь USDC.e → ETH (перед LI.FI нужен approve USDC.e на роутер)
    balance_usdce3 = _get_token_balance(USDCE, wallet_address)
    if balance_usdce3 > 0:
        quote10 = _get_lifi_quote(USDCE, NATIVE_ETH, balance_usdce3, wallet_address)
        if not quote10:
            logger.warning("LI.FI котировка USDC.e→ETH недоступна")
            return False
        req10 = quote10["_transaction_request"]
        est10 = quote10.get("estimate") or {}
        approval_addr10 = est10.get("approvalAddress")
        if not approval_addr10 and quote10.get("includedSteps"):
            est10 = quote10["includedSteps"][0].get("estimate") or {}
            approval_addr10 = est10.get("approvalAddress")
        if approval_addr10:
            if _approve(private_key, USDCE, approval_addr10, balance_usdce3) is None:
                logger.warning("Approve USDC.e для LI.FI не выполнен")
            else:
                time.sleep(random.uniform(PAUSE_SHORT_MIN, PAUSE_SHORT_MAX))
        if _execute_lifi_tx(private_key, req10) is None:
            logger.warning("Свап USDC.e→ETH не выполнен")
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
    logger.info("Квест Sake Deposit: {}", name)

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

    # Пополнение до 25$ при нехватке (бридж из L2 или вывод с MEXC + бридж)
    await asyncio.to_thread(ensure_soneium_balance_for_quest, wallet_address, private_key)

    # Проверка баланса ≥24$ в ETH (целевой с запасом 25$; цена через LI.FI)
    balance_eth = await asyncio.to_thread(_get_balance_eth, wallet_address)
    eth_price = await asyncio.to_thread(_get_eth_price_usd)
    if eth_price <= 0:
        logger.warning("Не удалось получить цену ETH — квест пропущен")
        return
    balance_usd = balance_eth * eth_price
    if balance_usd < MIN_BALANCE_USD:
        logger.warning(
            "Минимальный баланс для выполнения квеста — 24$ в ETH (целевой 25$). Сейчас ~{:.2f}$ ({} ETH)",
            balance_usd,
            balance_eth,
        )
        return

    logger.info("Выполняем ончейн-шаги Sake Deposit (баланс ~{:.2f}$)", balance_usd)
    ok = await asyncio.to_thread(_do_onchain_steps, private_key, wallet_address)
    if not ok:
        logger.warning("Квест {}: не удалось выполнить все шаги", name)
        return

    logger.info("Ожидание {} с после подтверждения", SECONDS_AFTER_CONFIRMATION)
    await asyncio.sleep(SECONDS_AFTER_CONFIRMATION)

    UI_RETRIES_AFTER_ONCHAIN = 5
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
            logger.success("Квест {}: депозит выполнен, награда забрана", name)
            save_completed_quest(wallet_address, name, "verified_and_claimed")
            return
        except Exception as e:
            logger.warning("После шагов Verify/Claim (попытка UI {}/{}): {}", ui_retry, UI_RETRIES_AFTER_ONCHAIN, e)
            if ui_retry < UI_RETRIES_AFTER_ONCHAIN:
                await asyncio.sleep(random.uniform(QUEST_UI_RETRY_DELAY_MIN, QUEST_UI_RETRY_DELAY_MAX))
    logger.warning("Квест {}: ончейн выполнен, но Verify/Claim не удался после {} попыток UI", name, UI_RETRIES_AFTER_ONCHAIN)
