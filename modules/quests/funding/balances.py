# -*- coding: utf-8 -*-
"""Балансы ETH по сетям: Soneium, OP, BASE, ARB."""

from __future__ import annotations

from typing import Optional

from web3 import Web3

from modules.quests.constants import get_soneium_chain_id, get_soneium_rpc_url

SONEIUM_CHAIN_ID = 1868
OP_CHAIN_ID = 10
BASE_CHAIN_ID = 8453
ARB_CHAIN_ID = 42161

# Публичные RPC для L2 (fallback если в config нет)
RPC_BY_CHAIN: dict[int, list[str]] = {
    SONEIUM_CHAIN_ID: [],  # заполняется из get_soneium_rpc_url()
    10: ["https://optimism.publicnode.com", "https://optimism.llamarpc.com"],
    8453: ["https://base.publicnode.com", "https://base.llamarpc.com"],
    42161: ["https://arbitrum-one.publicnode.com", "https://arbitrum.llamarpc.com"],
}

TIMEOUT = 15


def _rpc_for_chain(chain_id: int) -> list[str]:
    urls = list(RPC_BY_CHAIN.get(chain_id, []))
    if chain_id == get_soneium_chain_id():
        urls.insert(0, get_soneium_rpc_url())
    return urls or [get_soneium_rpc_url()]


def get_eth_balance_wei(chain_id: int, address: str) -> int:
    """Баланс native ETH в wei. Возвращает 0 при ошибке."""
    address = Web3.to_checksum_address(address)
    for rpc in _rpc_for_chain(chain_id):
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != chain_id:
                continue
            return w3.eth.get_balance(address)
        except Exception:
            continue
    return 0


def get_eth_balance(chain_id: int, address: str) -> float:
    """Баланс native ETH в ETH (float)."""
    wei = get_eth_balance_wei(chain_id, address)
    return float(Web3.from_wei(wei, "ether"))


def get_soneium_balance_usd(address: str, eth_price_usd: float) -> float:
    """Баланс в Soneium в долларах (ETH * price)."""
    if eth_price_usd <= 0:
        return 0.0
    eth = get_eth_balance(get_soneium_chain_id(), address)
    return eth * eth_price_usd


def get_l2_balances(address: str) -> list[tuple[int, str, float]]:
    """Балансы ETH в OP, BASE, ARB. Возвращает список (chain_id, name, balance_eth)."""
    chains = [
        (OP_CHAIN_ID, "OP"),
        (BASE_CHAIN_ID, "BASE"),
        (ARB_CHAIN_ID, "ARB"),
    ]
    out = []
    for cid, name in chains:
        bal = get_eth_balance(cid, address)
        out.append((cid, name, bal))
    return out
