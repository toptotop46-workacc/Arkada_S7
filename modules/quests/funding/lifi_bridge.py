# -*- coding: utf-8 -*-
"""Бридж ETH из L2 (OP/BASE/ARB) в Soneium через LI.FI."""

from __future__ import annotations

import time
from typing import Any, Optional

import requests
from loguru import logger
from web3 import Web3

from modules.quests.constants import get_soneium_chain_id, get_soneium_rpc_url
from modules.quests.funding.balances import ARB_CHAIN_ID, BASE_CHAIN_ID, OP_CHAIN_ID

LI_FI_API_BASE = "https://li.quest/v1"
LI_FI_API_KEY = "aeaa4f26-c3c3-4b71-aad3-50bd82faf815.1e83cb78-2d75-412d-a310-57272fd0e622"
LI_FI_INTEGRATOR = "Soneium"
LI_FI_FEE = "0.005"
LI_FI_SLIPPAGE = "0.05"
NATIVE_ETH = "0x0000000000000000000000000000000000000000"

def _is_nonce_too_low(err: Any) -> bool:
    """Проверяет, что ошибка RPC — «nonce too low» (повторная отправка с новым nonce)."""
    if isinstance(err, dict):
        msg = str(err.get("message", err))
    else:
        msg = getattr(err, "message", "") or str(err)
    return "nonce too low" in msg.lower()


RPC_BY_CHAIN: dict[int, str] = {
    OP_CHAIN_ID: "https://optimism.publicnode.com",
    BASE_CHAIN_ID: "https://base.publicnode.com",
    ARB_CHAIN_ID: "https://arbitrum-one.publicnode.com",
}
RPC_TIMEOUT = 60
RPC_RETRIES = 3
RECEIPT_TIMEOUT = 300
_rate_limit_last = 0.0
_rate_limit_interval = 0.3


def _rate_limit() -> None:
    global _rate_limit_last
    now = time.monotonic()
    if now - _rate_limit_last < _rate_limit_interval:
        time.sleep(_rate_limit_interval - (now - _rate_limit_last))
    _rate_limit_last = time.monotonic()


def get_bridge_quote(
    from_chain_id: int,
    to_chain_id: int,
    from_amount_wei: int,
    from_address: str,
    to_address: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Квота LI.FI на бридж ETH from_chain → to_chain. Возвращает ответ API или None."""
    _rate_limit()
    to_addr = to_address or from_address
    params = {
        "fromChain": from_chain_id,
        "toChain": to_chain_id,
        "fromToken": NATIVE_ETH,
        "toToken": NATIVE_ETH,
        "fromAmount": str(from_amount_wei),
        "fromAddress": from_address,
        "toAddress": to_addr,
        "slippage": LI_FI_SLIPPAGE,
        "integrator": LI_FI_INTEGRATOR,
        "fee": LI_FI_FEE,
    }
    headers = {"x-lifi-api-key": LI_FI_API_KEY, "User-Agent": "Arkada-S7"}
    try:
        r = requests.get(f"{LI_FI_API_BASE}/quote", params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            logger.debug("LI.FI quote {}: {}", r.status_code, r.text[:200])
            return None
        data = r.json()
        req = data.get("transactionRequest") or (data.get("action") and data.get("action", {}).get("transactionRequest"))
        if not req or not req.get("to") or not req.get("data"):
            return None
        return data
    except Exception as e:
        logger.debug("LI.FI quote error: {}", e)
        return None


def execute_bridge(
    private_key: str,
    quote: dict[str, Any],
    source_chain_id: int,
) -> Optional[str]:
    """Выполнить бридж: подписать и отправить транзакцию в source_chain. Возвращает tx hash или None."""
    req = quote.get("transactionRequest") or (quote.get("action") or {}).get("transactionRequest")
    if not req:
        return None
    rpc = RPC_BY_CHAIN.get(source_chain_id)
    if not rpc:
        if source_chain_id == get_soneium_chain_id():
            rpc = get_soneium_rpc_url()
        else:
            return None
    retriable = (ConnectionError, OSError, TimeoutError)
    for attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": RPC_TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != source_chain_id:
                if attempt < RPC_RETRIES:
                    time.sleep(5 + (attempt - 1) * 3)
                    continue
                logger.warning("RPC недоступен или неверный chain_id после {} попыток", RPC_RETRIES)
                return None
            account = w3.eth.account.from_key(private_key)
            to = Web3.to_checksum_address(req["to"])
            data = req["data"]
            if isinstance(data, bytes):
                data = "0x" + data.hex()
            raw_val = req.get("value", 0)
            value = int(raw_val, 16) if isinstance(raw_val, str) and raw_val.startswith("0x") else int(raw_val or 0)
            gas_limit = req.get("gasLimit") or req.get("gas")
            gas_int = int(gas_limit, 16) if isinstance(gas_limit, str) else (int(gas_limit or 0) * 2 or 500000)
            try:
                base_fee = w3.eth.get_block("latest").get("baseFeePerGas") or 0
                gas_price_for_reserve = (base_fee * 2 + (base_fee // 10)) if base_fee else w3.eth.gas_price
            except Exception:
                gas_price_for_reserve = w3.eth.gas_price
            gas_cost = gas_int * gas_price_for_reserve
            gas_reserve = int(gas_cost * 2)  # запас 100% на рост base_fee к моменту отправки
            balance = w3.eth.get_balance(account.address)
            if balance < value + gas_reserve:
                max_value = balance - gas_reserve
                if max_value <= 0:
                    logger.warning(
                        "Недостаточно средств для бриджа (баланс меньше комиссии): have {} wei, reserve {} wei",
                        balance,
                        gas_reserve,
                    )
                    return None
                value = min(value, max_value)
                logger.debug("value уменьшен до {} wei (резерв под газ)", value)
            tx_hash = None
            for nonce_attempt in range(3):
                try:
                    nonce = w3.eth.get_transaction_count(account.address, "pending")
                    # Актуальные base_fee и баланс перед отправкой, чтобы не уйти в "insufficient funds"
                    balance_now = w3.eth.get_balance(account.address)
                    try:
                        base_fee = w3.eth.get_block("latest").get("baseFeePerGas") or 0
                        if base_fee:
                            gas_price = base_fee * 2 + (base_fee // 10)
                        else:
                            gas_price = w3.eth.gas_price
                    except Exception:
                        base_fee = 0
                        gas_price = w3.eth.gas_price
                    actual_gas_cost = gas_int * gas_price
                    # Резерв 1e9 wei на округление и мелкие колебания
                    if balance_now < value + actual_gas_cost + 1_000_000_000:
                        max_value_now = balance_now - actual_gas_cost - 1_000_000_000
                        if max_value_now <= 0:
                            logger.warning(
                                "Недостаточно средств для газа при отправке: balance {} wei, gas cost {} wei",
                                balance_now,
                                actual_gas_cost,
                            )
                            raise ValueError("insufficient funds for gas")
                        value = min(value, max_value_now)
                        logger.debug("value уменьшен перед отправкой до {} wei", value)
                    # Используем уже посчитанные base_fee и gas_price — так сумма value+gas не превысит баланс
                    if base_fee:
                        tx_params = {
                            "chainId": source_chain_id,
                            "from": account.address,
                            "nonce": nonce,
                            "to": to,
                            "data": data,
                            "value": value,
                            "gas": gas_int,
                            "maxFeePerGas": gas_price,
                            "maxPriorityFeePerGas": base_fee // 10,
                        }
                    else:
                        tx_params = {
                            "chainId": source_chain_id,
                            "from": account.address,
                            "nonce": nonce,
                            "to": to,
                            "data": data,
                            "value": value,
                            "gas": gas_int,
                            "gasPrice": gas_price,
                        }
                    signed = account.sign_transaction(tx_params)
                    raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
                    if raw_tx is None:
                        try:
                            raw_tx = signed.get("rawTransaction") or signed.get("raw_transaction")
                        except (TypeError, AttributeError):
                            pass
                    if not raw_tx:
                        return None
                    tx_hash = w3.eth.send_raw_transaction(raw_tx)
                    break
                except Exception as send_err:
                    if nonce_attempt < 2 and _is_nonce_too_low(send_err):
                        logger.debug("nonce too low, повтор с новым nonce (бридж)...")
                        time.sleep(1)
                        continue
                    raise
            if tx_hash is None:
                return None
            tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
            logger.info("Бридж tx отправлена (chain {}): {}", source_chain_id, tx_hex[:16] + "…")
            receipt = None
            for _ in range(3):
                try:
                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
                    break
                except retriable:
                    time.sleep(5)
            if receipt is None:
                logger.warning("Бридж: не получен receipt по tx {}", tx_hex[:16] + "…")
                return None
            if receipt.get("status") != 1:
                logger.warning(
                    "Бридж tx отменена/ревертнута на L2 (status={}), средства остались в исходной сети",
                    receipt.get("status"),
                )
                return None
            return tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex
        except retriable:
            if tx_hash is not None:
                return None
            if attempt < RPC_RETRIES:
                time.sleep(5 + (attempt - 1) * 3)
            else:
                logger.warning("RPC недоступен после {} попыток (бридж)", RPC_RETRIES)
                return None
        except Exception as e:
            logger.warning("Ошибка выполнения бриджа: {}", e)
            return None
    return None
