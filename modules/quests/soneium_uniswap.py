# -*- coding: utf-8 -*-
"""
Квест Soneium Score — Uniswap: страница кампании, Verify; при невыполненном квесте — 1 свап ETH→USDC.e,
ожидание подтверждения + 10 с, повторный Verify, затем Claim.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Optional

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

# --- Конфиг Soneium / Uniswap v4 (по примеру uniswap.py) ---
RPC_URL = get_soneium_rpc_url()
CHAIN_ID = get_soneium_chain_id()
QUOTER_ADDRESS = "0x3972c00f7ed4885e145823eb7c655375d275a1c5"
UNIVERSAL_ROUTER_ADDRESS = "0x0e2850543f69f678257266e0907ff9a58b3f13de"
USDCE_ADDRESS = "0xbA9986D2381edf1DA03B0B9c1f8b00dc4AacC369"
NATIVE_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"
FEE_TIER = 500
TICK_SPACING = 10
SWAP_PERCENT_MIN = 0.1
SWAP_PERCENT_MAX = 1.5
MAX_SWAP_ATTEMPTS = 5
SECONDS_AFTER_CONFIRMATION = 10
RPC_TIMEOUT = 60
RPC_RETRIES = 3

QUOTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {
                        "components": [
                            {"internalType": "address", "name": "currency0", "type": "address"},
                            {"internalType": "address", "name": "currency1", "type": "address"},
                            {"internalType": "uint24", "name": "fee", "type": "uint24"},
                            {"internalType": "int24", "name": "tickSpacing", "type": "int24"},
                            {"internalType": "contract IHooks", "name": "hooks", "type": "address"},
                        ],
                        "internalType": "struct PoolKey",
                        "name": "poolKey",
                        "type": "tuple",
                    },
                    {"internalType": "bool", "name": "zeroForOne", "type": "bool"},
                    {"internalType": "uint128", "name": "exactAmount", "type": "uint128"},
                    {"internalType": "bytes", "name": "hookData", "type": "bytes"},
                ],
                "internalType": "struct IV4Quoter.QuoteExactSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]
UNIVERSAL_ROUTER_ABI = [
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

CAMPAIGN_URLS = [
    "https://app.arkada.gg/en/campaign/soneium-score-seventh-uniswap",
]


def _encode_v4_swap_command(
    w3: Web3,
    token_in: str,
    token_out: str,
    amount_in_wei: int,
    recipient: str,
    fee: int,
    tick_spacing: int,
    hooks: str = "0x0000000000000000000000000000000000000000",
) -> tuple[bytes, bytes]:
    """Кодирует команду V4_SWAP для Universal Router (по примеру uniswap.py)."""
    from eth_abi import encode as abi_encode

    command = bytes([0x10])
    currency0 = Web3.to_checksum_address(token_in)
    currency1 = Web3.to_checksum_address(token_out)
    hooks_addr = Web3.to_checksum_address(hooks)
    recipient_addr = Web3.to_checksum_address(recipient)

    actions = bytes([0x06, 0x0b, 0x0e])
    zero_for_one = True
    hook_data = b""
    pool_key_data = (
        abi_encode(["address"], [currency0])
        + abi_encode(["address"], [currency1])
        + abi_encode(["uint24"], [fee])
        + abi_encode(["int24"], [tick_spacing])
        + abi_encode(["address"], [hooks_addr])
    )
    hook_data_offset = 5 * 32 + 32 + 32 + 32
    swap_params_encoded = (
        pool_key_data
        + abi_encode(["bool"], [zero_for_one])
        + abi_encode(["uint128"], [amount_in_wei])
        + abi_encode(["uint128"], [0])
        + abi_encode(["uint256"], [hook_data_offset])
        + abi_encode(["uint256"], [len(hook_data)])
        + hook_data
    )
    settle_params_encoded = abi_encode(
        ["address", "uint256", "bool"],
        [currency0, amount_in_wei, True],
    )
    take_params_encoded = abi_encode(
        ["address", "address", "uint256"],
        [currency1, recipient_addr, 0],
    )
    params_array = [swap_params_encoded, settle_params_encoded, take_params_encoded]
    try:
        input_bytes = w3.codec.encode(["bytes", "bytes[]"], [actions, params_array])
    except Exception:
        actions_padded_len = ((len(actions) + 31) // 32) * 32
        actions_padded = actions + b"\x00" * (actions_padded_len - len(actions))
        params_offset = 0x60 + actions_padded_len
        param_offsets = []
        tail_offset = len(params_array) * 32
        current_offset = tail_offset
        for param in params_array:
            param_offsets.append(current_offset)
            param_padded_len = ((len(param) + 31) // 32) * 32
            current_offset += 32 + 32 + param_padded_len
        input_bytes = (
            abi_encode(["uint256"], [0x40])
            + abi_encode(["uint256"], [params_offset])
            + abi_encode(["uint256"], [len(actions)])
            + actions_padded
            + abi_encode(["uint256"], [len(params_array)])
            + b"".join(abi_encode(["uint256"], [o]) for o in param_offsets)
        )
        for param in params_array:
            pad_len = ((len(param) + 31) // 32) * 32 - len(param)
            input_bytes += abi_encode(["uint256"], [32]) + abi_encode(["uint256"], [len(param)]) + param + b"\x00" * pad_len
    return command, input_bytes


def _simulate_swap(
    w3: Web3,
    amount_in_wei: int,
) -> bool:
    """Симуляция свапа через Quoter. Возвращает True при успехе."""
    try:
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_ADDRESS),
            abi=QUOTER_ABI,
        )
        pool_key = (
            Web3.to_checksum_address(NATIVE_ETH_ADDRESS),
            Web3.to_checksum_address(USDCE_ADDRESS),
            FEE_TIER,
            TICK_SPACING,
            Web3.to_checksum_address("0x0000000000000000000000000000000000000000"),
        )
        params = (pool_key, True, amount_in_wei, b"")
        quoter.functions.quoteExactInputSingle(params).call()
        return True
    except Exception as e:
        logger.debug("Симуляция свапа не прошла: {}", e)
        return False


def _execute_one_swap(
    private_key: str,
    amount_in_wei: int,
) -> Optional[str]:
    """
    Выполняет один свап ETH → USDC.e. Возвращает tx hash при успехе и подтверждении, иначе None.
    При обрыве соединения с RPC (ConnectionResetError и т.п.) повторяет до RPC_RETRIES раз.
    """
    retriable = (ConnectionError, OSError, TimeoutError)

    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(
                Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT})
            )
            if not w3.is_connected():
                if rpc_attempt < RPC_RETRIES:
                    logger.warning("RPC недоступен, повтор через {} с", 5 + (rpc_attempt - 1) * 3)
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                logger.warning("RPC недоступен после {} попыток", RPC_RETRIES)
                return None
            if w3.eth.chain_id != CHAIN_ID:
                logger.warning("Неверный Chain ID: {}", w3.eth.chain_id)
                return None

            account = w3.eth.account.from_key(private_key)
            wallet = account.address

            if not _simulate_swap(w3, amount_in_wei):
                logger.warning("Симуляция свапа не прошла, пропускаем отправку")
                return None

            command, input_bytes = _encode_v4_swap_command(
                w3=w3,
                token_in=NATIVE_ETH_ADDRESS,
                token_out=USDCE_ADDRESS,
                amount_in_wei=amount_in_wei,
                recipient=wallet,
                fee=FEE_TIER,
                tick_spacing=TICK_SPACING,
            )
            inputs_array = [input_bytes]
            deadline = int(time.time()) + 3600
            router = w3.eth.contract(
                address=Web3.to_checksum_address(UNIVERSAL_ROUTER_ADDRESS),
                abi=UNIVERSAL_ROUTER_ABI,
            )
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
                "value": amount_in_wei,
            }
            if max_fee:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = gas_price

            try:
                gas_estimate = router.functions.execute(
                    command, inputs_array, deadline
                ).estimate_gas({**tx_params, "value": amount_in_wei})
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception:
                tx_params["gas"] = 200000

            transaction = router.functions.execute(
                command, inputs_array, deadline
            ).build_transaction(tx_params)
            signed = account.sign_transaction(transaction)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(
                signed, "rawTransaction", None
            )
            if raw_tx is None:
                try:
                    raw_tx = signed["rawTransaction"] or signed["raw_transaction"]
                except (TypeError, KeyError):
                    pass
            if not raw_tx:
                raise ValueError(
                    "Не удалось получить raw_transaction из подписанной транзакции"
                )
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            tx_hex = tx_hash.hex()
            tx_hex_prefixed = tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex
            tx_url = f"https://soneium.blockscout.com/tx/{tx_hex_prefixed}"
            logger.info("Транзакция отправлена: {}", tx_url)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            if receipt["status"] == 1:
                logger.success("Свап подтверждён")
                return tx_hex
            logger.warning("Транзакция не прошла (status=0)")
            return None

        except retriable as e:
            if rpc_attempt < RPC_RETRIES:
                delay = 5 + (rpc_attempt - 1) * 3
                logger.info(
                    "RPC соединение разорвано (попытка {}/{}), повтор через {} с",
                    rpc_attempt,
                    RPC_RETRIES,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.warning("Ошибка свапа (RPC): {}", e)
                return None
        except Exception as e:
            logger.warning("Ошибка свапа: {}", e)
            return None

    return None


def _get_balance_eth(address: str) -> float:
    """Баланс ETH в ETH (синхронно)."""
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return 0.0
    balance_wei = w3.eth.get_balance(Web3.to_checksum_address(address))
    return float(Web3.from_wei(balance_wei, "ether"))


async def run(
    page: Any,
    wallet_address: str,
    confirm_claim_in_rabby: Callable[[Any], Awaitable[None]],
    private_key: str,
) -> None:
    """
    Страница квеста → Verify; при «Quest not completed» — до 5 попыток свапа (0.1–1.5% баланса),
    после успеха: ожидание подтверждения + 10 с, повторный Verify → Claim → подтверждение в кошельке.
    """
    if not CAMPAIGN_URLS:
        return
    url = CAMPAIGN_URLS[0]
    name = url.rstrip("/").split("/")[-1] or url
    if all_quests_already_claimed(wallet_address, [name]):
        logger.success("Квест {}: уже выполнен", name)
        return
    logger.info("Квест Uniswap: {}", name)

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
    logger.info("Квест {}: не выполнен — выполняем свап (до {} попыток ончейна)", name, MAX_SWAP_ATTEMPTS)
    UI_RETRIES_AFTER_SWAP = 5

    balance_eth = await asyncio.to_thread(_get_balance_eth, wallet_address)
    if balance_eth <= 0:
        logger.warning("Баланс ETH 0 — свап невозможен")
        return

    swap_done = False
    for attempt in range(1, MAX_SWAP_ATTEMPTS + 1):
        if not swap_done:
            percent = random.uniform(SWAP_PERCENT_MIN, SWAP_PERCENT_MAX)
            amount_eth = balance_eth * (percent / 100.0)
            amount_wei = int(Web3.to_wei(amount_eth, "ether"))
            if amount_wei <= 0:
                logger.warning("Сумма свапа 0 ({}% от баланса)", percent)
                continue
            logger.info(
                "Попытка свапа {}/{}: {}% от баланса (~{} ETH)",
                attempt,
                MAX_SWAP_ATTEMPTS,
                round(percent, 2),
                round(amount_eth, 6),
            )
            tx_hash = await asyncio.to_thread(_execute_one_swap, private_key, amount_wei)
            if not tx_hash:
                if attempt < MAX_SWAP_ATTEMPTS:
                    delay = random.uniform(5, 15)
                    logger.info("Пауза {:.0f} с перед повтором", delay)
                    await asyncio.sleep(delay)
                continue
            swap_done = True

        logger.info("Ожидание {} с после подтверждения транзакции", SECONDS_AFTER_CONFIRMATION)
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

    logger.warning("Квест {}: не удалось выполнить свап за {} попыток", name, MAX_SWAP_ATTEMPTS)
