# -*- coding: utf-8 -*-
"""
Квест Soneium Score — Velodrome: страница кампании, Verify; при невыполненном квесте —
1 свап ETH→USDC.E через Velodrome Universal Router (0.1–1% баланса), ожидание 10 с, повторный Verify, затем Claim.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Optional

from eth_abi import encode as abi_encode
from loguru import logger
from web3 import Web3

from modules.quests.constants import (
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

# --- URL кампании ---
CAMPAIGN_URLS = [
    "https://app.arkada.gg/en/campaign/soneium-score-seventh-velodrome",
]

# --- Конфиг Soneium / Velodrome Universal Router ---
RPC_URL = get_soneium_rpc_url()
CHAIN_ID = get_soneium_chain_id()
VELODROME_UNIVERSAL_ROUTER = "0x01D40099fCD87C018969B0e8D4aB1633Fb34763C"
WETH = "0x4200000000000000000000000000000000000006"
USDCE = "0xbA9986D2381edf1DA03B0B9c1f8b00dc4AacC369"

WRAP_ETH = 0x0B
V3_SWAP_EXACT_IN = 0x00
POOL_FEE_TIER = 100

SWAP_PERCENT_MIN = 0.1
SWAP_PERCENT_MAX = 1.0
MIN_SWAP_ETH = 0.000001
SLIPPAGE_BPS = 1000
SMALL_AMOUNT_ETH_THRESHOLD = 0.0001
RPC_TIMEOUT = 60
RECEIPT_TIMEOUT = 120
RPC_RETRIES = 3

MAX_SWAP_ATTEMPTS = 5
SECONDS_AFTER_CONFIRMATION = 10

ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "bytes", "name": "commands", "type": "bytes"},
            {"internalType": "bytes[]", "name": "inputs", "type": "bytes[]"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "execute",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    }
]


def _get_balance_eth(address: str) -> float:
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return 0.0
    balance_wei = w3.eth.get_balance(Web3.to_checksum_address(address))
    return float(Web3.from_wei(balance_wei, "ether"))


def _build_path(token_in: str, token_out: str, fee: int) -> bytes:
    a = Web3.to_checksum_address(token_in)
    b = Web3.to_checksum_address(token_out)
    return (
        bytes.fromhex(a[2:].lower())
        + fee.to_bytes(3, "big")
        + bytes.fromhex(b[2:].lower())
    )


def _encode_wrap_eth(recipient: str, amount_wei: int) -> bytes:
    return abi_encode(
        ["address", "uint256"],
        [Web3.to_checksum_address(recipient), amount_wei],
    )


def _encode_v3_swap_exact_in(
    recipient: str,
    amount_in: int,
    amount_out_min: int,
    path: bytes,
    payer_is_user: bool,
    is_uni: bool,
) -> bytes:
    return abi_encode(
        ["address", "uint256", "uint256", "bytes", "bool", "bool"],
        [
            Web3.to_checksum_address(recipient),
            amount_in,
            amount_out_min,
            path,
            payer_is_user,
            is_uni,
        ],
    )


def _swap_eth_to_usdc(
    private_key: str,
    amount_eth: Optional[float] = None,
    percent: Optional[float] = None,
    slippage_bps: int = SLIPPAGE_BPS,
) -> Optional[str]:
    """Свап ETH → USDC.E через Velodrome (WRAP_ETH + V3_SWAP_EXACT_IN). Возвращает tx hash или None."""
    retriable = (ConnectionError, OSError, TimeoutError)
    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if rpc_attempt < RPC_RETRIES:
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                logger.warning("RPC недоступен или неверный chain_id")
                return None

            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            balance_eth = _get_balance_eth(wallet)
            if balance_eth <= 0:
                logger.warning("Баланс ETH 0")
                return None

            if amount_eth is not None:
                amount_eth = max(0.0, min(amount_eth, balance_eth))
            elif percent is not None:
                amount_eth = balance_eth * (percent / 100.0)
            else:
                percent = random.uniform(SWAP_PERCENT_MIN, SWAP_PERCENT_MAX)
                amount_eth = balance_eth * (percent / 100.0)

            if amount_eth < MIN_SWAP_ETH:
                amount_eth = MIN_SWAP_ETH

            amount_wei = int(Web3.to_wei(amount_eth, "ether"))
            amount_str = f"{amount_eth:.8f}".rstrip("0").rstrip(".")
            logger.info("Свап ETH → USDC.E: ~{} ETH (Velodrome)", amount_str)

            router_addr = Web3.to_checksum_address(VELODROME_UNIVERSAL_ROUTER)
            path = _build_path(WETH, USDCE, POOL_FEE_TIER)
            if amount_eth < SMALL_AMOUNT_ETH_THRESHOLD:
                amount_out_min = 0
            else:
                estimated_usdc = amount_eth * 2000 * (10**6)
                amount_out_min = int(estimated_usdc * (10000 - slippage_bps) / 10000)

            wrap_input = _encode_wrap_eth(router_addr, amount_wei)
            swap_input = _encode_v3_swap_exact_in(
                recipient=wallet,
                amount_in=amount_wei,
                amount_out_min=amount_out_min,
                path=path,
                payer_is_user=False,
                is_uni=False,
            )

            commands = bytes([WRAP_ETH, V3_SWAP_EXACT_IN])
            inputs = [wrap_input, swap_input]
            deadline = int(time.time()) + 600

            router = w3.eth.contract(address=router_addr, abi=ROUTER_ABI)
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee = gas_price
                max_priority = gas_price // 10
            except Exception:
                gas_price = w3.eth.gas_price
                max_fee = None
                max_priority = None

            tx_params: dict[str, Any] = {
                "chainId": CHAIN_ID,
                "from": wallet,
                "nonce": nonce,
                "value": amount_wei,
            }
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = gas_price

            try:
                gas_estimate = router.functions.execute(
                    commands, inputs, deadline
                ).estimate_gas({"value": amount_wei, **tx_params})
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception as e:
                logger.debug("estimate_gas: {}", e)
                tx_params["gas"] = 350000

            tx = router.functions.execute(commands, inputs, deadline).build_transaction(
                tx_params
            )
            signed = account.sign_transaction(tx)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(
                signed, "rawTransaction", None
            )
            if raw_tx is None:
                try:
                    raw_tx = signed["rawTransaction"] or signed["raw_transaction"]
                except (TypeError, KeyError):
                    pass
            if not raw_tx:
                logger.warning("Не удалось получить raw_transaction")
                return None

            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            tx_hex = tx_hash.hex()
            if not tx_hex.startswith("0x"):
                tx_hex = "0x" + tx_hex
            logger.info(
                "Транзакция отправлена: https://soneium.blockscout.com/tx/{}", tx_hex
            )
            try:
                receipt = w3.eth.wait_for_transaction_receipt(
                    tx_hash, timeout=RECEIPT_TIMEOUT
                )
            except (TimeoutError, Exception) as e:
                logger.warning(
                    "Ожидание рецепта прервано ({}). Проверьте tx в эксплорере.", e
                )
                return None
            if receipt["status"] == 1:
                logger.success("Свап ETH → USDC.E подтверждён")
                return tx_hex
            logger.warning("Транзакция откатилась (status=0)")
            return None

        except retriable:
            if rpc_attempt < RPC_RETRIES:
                time.sleep(5 + (rpc_attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка свапа: {}", e)
            return None
    return None


async def run(
    page: Any,
    wallet_address: str,
    confirm_claim_in_rabby: Callable[[Any], Awaitable[None]],
    private_key: str,
) -> None:
    """
    Страница квеста → Verify; при «Quest not completed» — до 5 попыток свапа через Velodrome (0.1–1% баланса),
    после успеха: ожидание 10 с, повторный Verify → Claim → подтверждение в кошельке.
    """
    if not CAMPAIGN_URLS:
        return
    url = CAMPAIGN_URLS[0]
    name = url.rstrip("/").split("/")[-1] or url
    if all_quests_already_claimed(wallet_address, [name]):
        logger.success("Квест {}: уже выполнен", name)
        return
    logger.info("Квест Velodrome: {}", name)

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
    logger.info(
        "Квест {}: не выполнен — выполняем свап через Velodrome (до {} попыток ончейна)",
        name,
        MAX_SWAP_ATTEMPTS,
    )
    UI_RETRIES_AFTER_SWAP = 5

    swap_done = False
    for attempt in range(1, MAX_SWAP_ATTEMPTS + 1):
        if not swap_done:
            logger.info("Попытка свапа {}/{}", attempt, MAX_SWAP_ATTEMPTS)
            tx_hash = await asyncio.to_thread(_swap_eth_to_usdc, private_key, None, None)
            if not tx_hash:
                if attempt < MAX_SWAP_ATTEMPTS:
                    delay = random.uniform(5, 15)
                    logger.info("Пауза {:.0f} с перед повтором", delay)
                    await asyncio.sleep(delay)
                continue
            swap_done = True

        logger.info(
            "Ожидание {} с после подтверждения транзакции",
            SECONDS_AFTER_CONFIRMATION,
        )
        await asyncio.sleep(SECONDS_AFTER_CONFIRMATION)

        for ui_retry in range(1, UI_RETRIES_AFTER_SWAP + 1):
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
                logger.success("Квест {}: свап выполнен, награда забрана", name)
                save_completed_quest(wallet_address, name, "verified_and_claimed")
                return
            except Exception as e:
                logger.warning("После свапа Verify/Claim (попытка UI {}/{}): {}", ui_retry, UI_RETRIES_AFTER_SWAP, e)
                if ui_retry < UI_RETRIES_AFTER_SWAP:
                    await asyncio.sleep(random.uniform(QUEST_UI_RETRY_DELAY_MIN, QUEST_UI_RETRY_DELAY_MAX))

        if swap_done:
            logger.warning(
                "Квест {}: свап выполнен, но Verify/Claim не удался после {} попыток UI — новых свапов не делаем",
                name,
                UI_RETRIES_AFTER_SWAP,
            )
            return

    logger.warning(
        "Квест {}: не удалось выполнить свап за {} попыток",
        name,
        MAX_SWAP_ATTEMPTS,
    )
