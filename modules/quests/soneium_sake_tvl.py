# -*- coding: utf-8 -*-
"""
Квест Soneium Score — Sake Finance TVL: страница кампании, Verify; при невыполненном квесте —
депозит ETH через WrappedTokenGatewayV3 (0.1–1% баланса), вывод через Pool.withdraw, unwrap WETH → ETH,
ожидание 10 с, повторный Verify, затем Claim.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from web3 import Web3

from modules.quests.constants import (
    PAUSE_MAX,
    PAUSE_MIN,
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

# --- Конфиг Soneium / Sake Finance ---
RPC_URL = get_soneium_rpc_url()
CHAIN_ID = get_soneium_chain_id()
WETH = "0x4200000000000000000000000000000000000006"
GATEWAY = "0x779a0A5686c2835F3DB9dB9EA4d030508E9EB096"
POOL = "0x3C3987A310ee13F7B8cBBe21D97D4436ba5E4B5f"

DEPOSIT_PERCENT_MIN = 0.1
DEPOSIT_PERCENT_MAX = 1.0
MIN_DEPOSIT_ETH = 0.000001
MAX_ATTEMPTS = 5
SECONDS_AFTER_CONFIRMATION = 10
RPC_TIMEOUT = 60
RPC_RETRIES = 3

WETH_ABI = [
    {"inputs": [{"internalType": "uint256", "name": "wad", "type": "uint256"}], "name": "withdraw", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

GATEWAY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "arg0", "type": "address"},
            {"internalType": "address", "name": "onBehalfOf", "type": "address"},
            {"internalType": "uint16", "name": "referralCode", "type": "uint16"},
        ],
        "name": "depositETH",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]

POOL_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "asset", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "address", "name": "to", "type": "address"},
        ],
        "name": "withdraw",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

CAMPAIGN_URLS = [
    "https://app.arkada.gg/en/campaign/soneium-score-seventh-sake-tvl",
]

MAX_UINT256 = 2**256 - 1


def _get_balance_eth(address: str) -> float:
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return 0.0
    balance_wei = w3.eth.get_balance(Web3.to_checksum_address(address))
    return float(Web3.from_wei(balance_wei, "ether"))


def _get_weth_balance_wei(address: str) -> int:
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
    if not w3.is_connected():
        return 0
    weth = w3.eth.contract(address=Web3.to_checksum_address(WETH), abi=WETH_ABI)
    return weth.functions.balanceOf(Web3.to_checksum_address(address)).call()


def _deposit_sake(private_key: str, amount_wei: int) -> Optional[str]:
    """Депозит ETH через Gateway.depositETH(pool, onBehalfOf, 0) с value=amount_wei."""
    retriable = (ConnectionError, OSError, TimeoutError)
    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if rpc_attempt < RPC_RETRIES:
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                return None
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            gateway = w3.eth.contract(address=Web3.to_checksum_address(GATEWAY), abi=GATEWAY_ABI)
            pool_addr = Web3.to_checksum_address(POOL)
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = gas_price, gas_price // 10
            except Exception:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = None, None
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
                gas_estimate = gateway.functions.depositETH(pool_addr, wallet, 0).estimate_gas(
                    {"from": wallet, "value": amount_wei}
                )
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception:
                tx_params["gas"] = 350000
            tx = gateway.functions.depositETH(pool_addr, wallet, 0).build_transaction(tx_params)
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
            logger.info(
                "Транзакция депозита Sake (depositETH) отправлена: https://soneium.blockscout.com/tx/{}",
                tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex,
            )
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            if receipt["status"] == 1:
                logger.info("Депозит Sake подтверждён")
                return tx_hex
            return None
        except retriable:
            if rpc_attempt < RPC_RETRIES:
                time.sleep(5 + (rpc_attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка депозита Sake: {}", e)
            return None
    return None


def _withdraw_sake(private_key: str) -> Optional[str]:
    """Вывод всей позиции: Pool.withdraw(WETH, type(uint256).max, user)."""
    retriable = (ConnectionError, OSError, TimeoutError)
    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if rpc_attempt < RPC_RETRIES:
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                logger.warning("RPC недоступен или неверный chain_id после {} попыток", RPC_RETRIES)
                return None
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            pool = w3.eth.contract(address=Web3.to_checksum_address(POOL), abi=POOL_ABI)
            weth_addr = Web3.to_checksum_address(WETH)
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = gas_price, gas_price // 10
            except Exception:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = None, None
            tx_params = {"chainId": CHAIN_ID, "from": wallet, "nonce": nonce}
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = gas_price
            try:
                gas_estimate = pool.functions.withdraw(weth_addr, MAX_UINT256, wallet).estimate_gas({"from": wallet})
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception:
                tx_params["gas"] = 350000
            tx = pool.functions.withdraw(weth_addr, MAX_UINT256, wallet).build_transaction(tx_params)
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
            logger.info(
                "Транзакция вывода Sake (withdraw) отправлена: https://soneium.blockscout.com/tx/{}",
                tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex,
            )
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            if receipt["status"] == 1:
                logger.info("Вывод Sake подтверждён")
                return tx_hex
            return None
        except retriable:
            if rpc_attempt < RPC_RETRIES:
                time.sleep(5 + (rpc_attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка вывода Sake: {}", e)
            return None
    return None


def _unwrap_all(private_key: str) -> Optional[str]:
    retriable = (ConnectionError, OSError, TimeoutError)
    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT}))
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_ID:
                if rpc_attempt < RPC_RETRIES:
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                return None
            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            weth = w3.eth.contract(address=Web3.to_checksum_address(WETH), abi=WETH_ABI)
            wad = weth.functions.balanceOf(wallet).call()
            if wad <= 0:
                logger.info("Нет WETH для unwrap — пропуск")
                return "skip"
            wad_eth = float(Web3.from_wei(wad, "ether"))
            logger.info("Unwrap WETH: ~{} ETH", f"{wad_eth:.8f}".rstrip("0").rstrip("."))
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            try:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = gas_price, gas_price // 10
            except Exception:
                gas_price = w3.eth.gas_price
                max_fee, max_priority = None, None
            tx_params = {"chainId": CHAIN_ID, "from": wallet, "nonce": nonce}
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = gas_price
            try:
                gas_estimate = weth.functions.withdraw(wad).estimate_gas({"from": wallet})
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception:
                tx_params["gas"] = 60000
            tx = weth.functions.withdraw(wad).build_transaction(tx_params)
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
            logger.info(
                "Транзакция unwrap отправлена: https://soneium.blockscout.com/tx/{}",
                tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex,
            )
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            if receipt["status"] == 1:
                logger.info("Unwrap подтверждён")
                return tx_hex
            return None
        except retriable:
            if rpc_attempt < RPC_RETRIES:
                time.sleep(5 + (rpc_attempt - 1) * 3)
            else:
                return None
        except Exception as e:
            logger.warning("Ошибка unwrap: {}", e)
            return None
    return None


def _do_withdraw_flow(private_key: str) -> bool:
    """Withdraw all из Sake → unwrap WETH. Возвращает True при успехе (или нечего выводить). Пауза 10–30 с между шагами."""
    if _withdraw_sake(private_key) is None:
        return False
    pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
    logger.info("Пауза {:.0f} с", pause)
    time.sleep(pause)
    result = _unwrap_all(private_key)
    return result is not None  # "skip" или tx_hex — ок


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
    logger.info("Квест Sake TVL: {}", name)

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
        "Квест {}: не выполнен — депозит в Sake (до {} попыток ончейна)",
        name,
        MAX_ATTEMPTS,
    )
    UI_RETRIES_AFTER_ONCHAIN = 5

    balance_eth = await asyncio.to_thread(_get_balance_eth, wallet_address)
    if balance_eth <= 0:
        logger.warning("Баланс ETH 0 — депозит невозможен")
        return
    if balance_eth < MIN_DEPOSIT_ETH:
        logger.warning(
            "Баланс {:.8f} ETH меньше минимума депозита {} ETH — депозит невозможен",
            balance_eth,
            MIN_DEPOSIT_ETH,
        )
        return

    on_chain_done = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if not on_chain_done:
            percent = random.uniform(DEPOSIT_PERCENT_MIN, DEPOSIT_PERCENT_MAX)
            amount_eth = balance_eth * (percent / 100.0)
            if amount_eth < MIN_DEPOSIT_ETH:
                amount_eth = MIN_DEPOSIT_ETH
            amount_wei = int(Web3.to_wei(amount_eth, "ether"))
            if amount_wei <= 0:
                continue
            amount_str = f"{amount_eth:.8f}".rstrip("0").rstrip(".")
            logger.info(
                "Попытка ончейна {}/{}: {}% от баланса (~{} ETH)",
                attempt,
                MAX_ATTEMPTS,
                round(percent, 2),
                amount_str,
            )

            if _deposit_sake(private_key, amount_wei) is None:
                if attempt < MAX_ATTEMPTS:
                    delay = random.uniform(5, 15)
                    logger.info("Пауза {:.0f} с перед повтором", delay)
                    await asyncio.sleep(delay)
                continue

            pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
            logger.info("Пауза {:.0f} с", pause)
            await asyncio.sleep(pause)
            withdraw_ok = await asyncio.to_thread(_do_withdraw_flow, private_key)
            if not withdraw_ok:
                logger.warning("Вывод (withdraw + unwrap) не выполнен после депозита")
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(random.uniform(5, 15))
                continue
            on_chain_done = True

        logger.info(
            "Ожидание {} с после подтверждения транзакции",
            SECONDS_AFTER_CONFIRMATION,
        )
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
                logger.success("Квест {}: депозит выполнен, награда забрана", name)
                save_completed_quest(wallet_address, name, "verified_and_claimed")
                return
            except Exception as e:
                logger.warning("После депозита Verify/Claim (попытка UI {}/{}): {}", ui_retry, UI_RETRIES_AFTER_ONCHAIN, e)
                if ui_retry < UI_RETRIES_AFTER_ONCHAIN:
                    await asyncio.sleep(random.uniform(QUEST_UI_RETRY_DELAY_MIN, QUEST_UI_RETRY_DELAY_MAX))

        if on_chain_done:
            logger.warning(
                "Квест {}: ончейн выполнен, но Verify/Claim не удался после {} попыток UI — новых депозитов не делаем",
                name,
                UI_RETRIES_AFTER_ONCHAIN,
            )
            return

    logger.warning(
        "Квест {}: не удалось выполнить ончейн за {} попыток",
        name,
        MAX_ATTEMPTS,
    )
