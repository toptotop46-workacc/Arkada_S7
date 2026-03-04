# -*- coding: utf-8 -*-
"""Обеспечение целевого баланса ETH в Soneium: бридж из L2 или вывод с MEXC + бридж."""

from __future__ import annotations

import random
import time
from typing import Optional

import requests
from loguru import logger

from modules.quests.constants import get_soneium_chain_id
from modules.quests.funding import balances as bal
from modules.quests.funding import lifi_bridge as lifi
from modules.quests.funding import mexc_withdraw as mexc

SONEIUM_CHAIN_ID = 1868
TARGET_USD_DEFAULT = 25.0
MIN_BALANCE_USD = 24.0
RESERVE_USD = 1.0
SLIPPAGE_FACTOR = 1.02
LI_FI_API_BASE = "https://li.quest/v1"
LI_FI_API_KEY = "aeaa4f26-c3c3-4b71-aad3-50bd82faf815.1e83cb78-2d75-412d-a310-57272fd0e622"
USDCE_SONEIUM = "0xbA9986D2381edf1DA03B0B9c1f8b00dc4AacC369"
NATIVE_ETH = "0x0000000000000000000000000000000000000000"
WAIT_AFTER_BRIDGE_POLL_INTERVAL = 10
WAIT_AFTER_BRIDGE_TIMEOUT = 600
WAIT_AFTER_MEXC_POLL_INTERVAL = 15
WAIT_AFTER_MEXC_TIMEOUT = 300

# netWork в MEXC -> chain_id для проверки баланса
MEXC_NETWORK_TO_CHAIN: dict[str, int] = {
    "ARBITRUM ONE(ARB)": 42161,
    "ARBITRUM": 42161,
    "ARB": 42161,
    "OPTIMISM(OP)": 10,
    "OPTIMISM": 10,
    "OP": 10,
    "BASE": 8453,
}


def _get_eth_price_usd() -> float:
    """Цена 1 ETH в USD через LI.FI (Soneium ETH -> USDC.e). При ошибке — fallback CoinGecko."""
    try:
        params = {
            "fromChain": get_soneium_chain_id(),
            "toChain": get_soneium_chain_id(),
            "fromToken": NATIVE_ETH,
            "toToken": USDCE_SONEIUM,
            "fromAmount": str(10**18),
            "fromAddress": "0x0000000000000000000000000000000000000001",
            "slippage": "0.05",
            "integrator": "Soneium",
            "fee": "0.005",
        }
        r = requests.get(
            f"{LI_FI_API_BASE}/quote",
            params=params,
            headers={"x-lifi-api-key": LI_FI_API_KEY, "User-Agent": "Arkada-S7"},
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            est = data.get("estimate") or {}
            to_amount = est.get("toAmount") or (data.get("action") or {}).get("toAmount")
            if to_amount is not None:
                return int(to_amount) / 1e6
    except Exception as e:
        logger.debug("Ошибка цены ETH: {}", e)
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


def _wait_for_balance(
    chain_id: int,
    address: str,
    min_eth: float,
    timeout_sec: int,
    poll_interval: int,
    label: str,
) -> bool:
    """Ждём пока баланс в сети chain_id станет >= min_eth."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        eth = bal.get_eth_balance(chain_id, address)
        if eth >= min_eth:
            logger.info("{}: баланс {} ETH (ожидали >={})", label, round(eth, 6), min_eth)
            return True
        logger.info(
            "{}: баланс {} ETH (нужно >={}), проверка через {} с",
            label,
            round(eth, 6),
            round(min_eth, 6),
            poll_interval,
        )
        time.sleep(poll_interval)
    return False


def _try_bridge_from_l2(
    private_key: str,
    from_chain_id: int,
    chain_name: str,
    from_address: str,
    amount_eth: float,
) -> bool:
    """Бриджим amount_eth из from_chain_id в Soneium. Возвращает True при успехе."""
    amount_wei = int(amount_eth * 1e18)
    to_chain = get_soneium_chain_id()
    quote = lifi.get_bridge_quote(from_chain_id, to_chain, amount_wei, from_address, from_address)
    if not quote:
        logger.warning("LI.FI не дал квоту {} -> Soneium для {} ETH", chain_name, amount_eth)
        return False
    # Баланс в Soneium до бриджа — читаем до execute_bridge, иначе prev может уже включать приход средств
    prev = bal.get_eth_balance(to_chain, from_address)
    tx = lifi.execute_bridge(private_key, quote, from_chain_id)
    if not tx:
        return False
    time.sleep(5)
    return _wait_for_balance(
        to_chain,
        from_address,
        prev + amount_eth * 0.95,
        WAIT_AFTER_BRIDGE_TIMEOUT,
        WAIT_AFTER_BRIDGE_POLL_INTERVAL,
        "Soneium после бриджа",
    )


def ensure_soneium_balance(
    wallet_address: str,
    private_key: str,
    target_usd: float = TARGET_USD_DEFAULT,
) -> bool:
    """
    Обеспечивает баланс ETH в Soneium не ниже target_usd (по умолчанию 25$).
    Если не хватает: сначала пробует бридж из OP/BASE/ARB; при нехватке там — вывод с MEXC в L2 и бридж.
    Возвращает True, если баланс после попыток >= MIN_BALANCE_USD (24$).
    """
    eth_price = _get_eth_price_usd()
    if eth_price <= 0:
        logger.warning("Не удалось получить цену ETH — пополнение пропущено")
        return False

    current_usd = bal.get_soneium_balance_usd(wallet_address, eth_price)
    shortfall_usd = max(0.0, target_usd - current_usd)
    if shortfall_usd <= 0:
        logger.info("Баланс Soneium ~{:.2f}$ — пополнение не требуется", current_usd)
        return True

    required_eth = shortfall_usd / eth_price
    required_eth_with_slippage = required_eth * SLIPPAGE_FACTOR
    required_wei = int(required_eth_with_slippage * 1e18)

    logger.info(
        "Не хватает ~{:.2f}$ (~{} ETH). Пробуем бридж из L2 или вывод с MEXC.",
        shortfall_usd,
        round(required_eth, 6),
    )

    # Проверяем балансы в L2
    l2 = bal.get_l2_balances(wallet_address)
    for chain_id, name, balance_eth in l2:
        if balance_eth >= required_eth_with_slippage + 0.0005:
            logger.info("Достаточно в {}: {} ETH — бриджим", name, round(balance_eth, 6))
            if _try_bridge_from_l2(private_key, chain_id, name, wallet_address, required_eth_with_slippage):
                return True
            logger.warning("Бридж из {} не удался, пробуем следующую сеть", name)

    # Не хватает в L2 — вывод с MEXC
    networks = mexc.get_eth_withdraw_networks()
    if not networks:
        logger.warning("MEXC: нет доступных сетей для вывода ETH или нет ключей в mexc_api.txt")
        return False

    # Выбираем сеть: withdrawMin <= required_eth и (required + fee) <= withdrawMax.
    suitable = [
        n for n in networks
        if n.get("withdrawMin", 999) <= required_eth_with_slippage
        and (required_eth_with_slippage + float(n.get("withdrawFee") or 0)) <= n.get("withdrawMax", float("inf"))
    ]
    if not suitable:
        logger.warning(
            "MEXC: нет сети, где {} ETH укладывается в лимиты (withdrawMin/withdrawMax)",
            round(required_eth_with_slippage, 6),
        )
        return False

    random.shuffle(suitable)
    net = None
    withdraw_id = None
    for candidate in suitable:
        net_work = candidate.get("netWork") or candidate.get("network") or ""
        fee = float(candidate.get("withdrawFee") or 0)
        amount_to_withdraw = required_eth_with_slippage + fee
        logger.info("Вывод с MEXC: {} ETH в сеть {}", round(amount_to_withdraw, 6), net_work)
        withdraw_id = mexc.withdraw("ETH", wallet_address, amount_to_withdraw, net_work)
        if withdraw_id:
            net = candidate
            break
        logger.warning("MEXC вывод в {} не выполнен (Insufficient balance или ошибка), пробуем другую сеть", net_work)
    if not withdraw_id or not net:
        logger.warning("MEXC вывод не выполнен ни в одной из сетей")
        return False
    net_work = net.get("netWork") or net.get("network") or ""
    fee = float(net.get("withdrawFee") or 0)
    amount_to_withdraw = required_eth_with_slippage + fee
    logger.info("MEXC вывод создан: {}", withdraw_id)

    chain_id = MEXC_NETWORK_TO_CHAIN.get(net_work.upper()) or next(
        (MEXC_NETWORK_TO_CHAIN.get(n.upper()) for n in net_work.split()),
        None,
    )
    if not chain_id:
        for k, v in MEXC_NETWORK_TO_CHAIN.items():
            if k in net_work.upper() or net_work.upper() in k:
                chain_id = v
                break
    if not chain_id:
        chain_id = 42161

    expected_eth = amount_to_withdraw - fee
    if not _wait_for_balance(
        chain_id,
        wallet_address,
        expected_eth * 0.99,
        WAIT_AFTER_MEXC_TIMEOUT,
        WAIT_AFTER_MEXC_POLL_INTERVAL,
        f"L2 ({net_work}) после MEXC",
    ):
        logger.warning("Средства с MEXC не поступили на L2 в течение {} с", WAIT_AFTER_MEXC_TIMEOUT)
        return False

    time.sleep(30)

    chain_name = {42161: "ARB", 10: "OP", 8453: "BASE"}.get(chain_id, str(chain_id))
    if _try_bridge_from_l2(private_key, chain_id, chain_name, wallet_address, expected_eth * 0.98):
        return True

    return False


def ensure_soneium_balance_for_quest(wallet_address: str, private_key: str) -> bool:
    """
    Удобная обёртка: целевой баланс 25$ (24$ минимум + 1$ запас).
    Возвращает True, если баланс в Soneium достаточно для квеста Sake Deposit (>= 24$).
    """
    ok = ensure_soneium_balance(wallet_address, private_key, target_usd=TARGET_USD_DEFAULT)
    if not ok:
        return False
    eth_price = _get_eth_price_usd()
    if eth_price <= 0:
        return False
    current_usd = bal.get_soneium_balance_usd(wallet_address, eth_price)
    return current_usd >= MIN_BALANCE_USD
