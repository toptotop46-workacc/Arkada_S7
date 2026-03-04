# -*- coding: utf-8 -*-
"""
Квест Soneium Score — NFTs2Me: страница кампании, Verify; при невыполненном квесте —
деплой NFT-коллекции через фабрику NFTs2Me (случайные метаданные), ожидание 10 с,
повторный Verify, затем Claim. Минт не требуется.
"""

from __future__ import annotations

import asyncio
import os
import random
import string
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

# --- Конфиг Soneium / NFTs2Me ---
RPC_URL = get_soneium_rpc_url()
CHAIN_ID = get_soneium_chain_id()
N2M_FACTORY = "0x00000000001594C61dD8a6804da9AB58eD2483ce"
# Селектор initialize008joDSK(string,string,uint256,bytes32,bytes32,bytes)
INIT_SELECTOR = bytes.fromhex("00000001")

# Битовые позиции в packedData (ConsecutiveMinting.sol)
_BITPOS_INIT_COLLECTION_SIZE = 160
_BITPOS_INIT_ROYALTY_FEE = 192
_BITPOS_INIT_MINTING_TYPE = 208
_BITPOS_INIT_PHASE = 216
_BITPOS_INIT_BITMAP = 224
_BITPOS_INIT_RESERVED_TOKENS = 232
BIT5MASK = 0x10  # hasStrings в extraCollectionInformation

MAX_ATTEMPTS = 5
SECONDS_AFTER_CONFIRMATION = 10
RPC_TIMEOUT = 60
RPC_RETRIES = 3

FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "bytes", "name": "collectionInformation", "type": "bytes"},
            {"internalType": "bytes32", "name": "collectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "implementationType", "type": "bytes32"},
        ],
        "name": "createCollectionN2M_000oEFvt",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]

CAMPAIGN_URLS = [
    "https://app.arkada.gg/en/campaign/soneium-score-seventh-nfts2me",
]


def _random_string(length: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(random.choices(alphabet, k=length))


def _random_name() -> str:
    return "Col" + _random_string(random.randint(4, 10))


def _random_symbol() -> str:
    return _random_string(random.randint(2, 5), string.ascii_uppercase).upper() or "NFT"


def _random_description() -> str:
    return "Random NFT collection " + _random_string(8)


def _random_base_uri() -> str:
    return "https://" + _random_string(12).lower() + ".example.com/"


def _build_collection_information(owner_address: str) -> bytes:
    """Собирает calldata для initialize008joDSK: случайные name, symbol, description, baseURI."""
    name_ = _random_name()
    symbol_ = _random_symbol()
    mint_price = 0
    base_uri_cid_hash = bytes(32)
    # packedData: owner (160 бит), collection_size (32), royalty (16), minting_type (8), phase (8), bitmap (8), reserved (16)
    owner_int = int(owner_address, 16) & ((1 << 160) - 1)
    collection_size = random.randint(100, 10000)
    royalty = 0
    minting_type = 0  # SEQUENTIAL
    phase = 0  # CLOSED
    bitmap = BIT5MASK  # hasStrings — передаём baseURI и description
    reserved = 0
    packed = (
        (reserved << _BITPOS_INIT_RESERVED_TOKENS)
        | (bitmap << _BITPOS_INIT_BITMAP)
        | (phase << _BITPOS_INIT_PHASE)
        | (minting_type << _BITPOS_INIT_MINTING_TYPE)
        | (royalty << _BITPOS_INIT_ROYALTY_FEE)
        | (collection_size << _BITPOS_INIT_COLLECTION_SIZE)
        | owner_int
    )
    packed_bytes = packed.to_bytes(32, "big")
    # extraCollectionInformation при hasStrings: (bytes32[], string, string)
    base_uri = _random_base_uri()
    description = _random_description()
    extra = abi_encode(
        ["bytes32[]", "string", "string"],
        [[], base_uri, description],
    )
    init_calldata = abi_encode(
        ["string", "string", "uint256", "bytes32", "bytes32", "bytes"],
        [name_, symbol_, mint_price, base_uri_cid_hash, packed_bytes, extra],
    )
    return INIT_SELECTOR + init_calldata


def _build_collection_id(owner_address: str) -> bytes:
    """collectionId: первые 20 байт = owner (требование containsCaller), остальное — случайное."""
    owner_hex = owner_address.lower().replace("0x", "").zfill(40)
    owner_bytes = bytes.fromhex(owner_hex)
    return owner_bytes + os.urandom(12)


def _deploy_collection(private_key: str) -> Optional[str]:
    """Деплой NFT-коллекции через createCollectionN2M_000oEFvt. Возвращает tx hash при успехе."""
    retriable = (ConnectionError, OSError, TimeoutError)

    for rpc_attempt in range(1, RPC_RETRIES + 1):
        try:
            w3 = Web3(
                Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": RPC_TIMEOUT})
            )
            if not w3.is_connected():
                if rpc_attempt < RPC_RETRIES:
                    time.sleep(5 + (rpc_attempt - 1) * 3)
                    continue
                logger.warning("RPC недоступен после {} попыток", RPC_RETRIES)
                return None
            if w3.eth.chain_id != CHAIN_ID:
                return None

            account = w3.eth.account.from_key(private_key)
            wallet = account.address
            factory = w3.eth.contract(
                address=Web3.to_checksum_address(N2M_FACTORY),
                abi=FACTORY_ABI,
            )

            collection_information = _build_collection_information(wallet)
            collection_id = _build_collection_id(wallet)
            implementation_type = bytes(32)

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
                "value": 0,
            }
            if max_fee is not None:
                tx_params["maxFeePerGas"] = max_fee
                tx_params["maxPriorityFeePerGas"] = max_priority
            else:
                tx_params["gasPrice"] = gas_price

            try:
                gas_estimate = factory.functions.createCollectionN2M_000oEFvt(
                    collection_information,
                    collection_id,
                    implementation_type,
                ).estimate_gas({"from": wallet, "value": 0})
                tx_params["gas"] = int(gas_estimate * 1.2)
            except Exception:
                tx_params["gas"] = 500000

            transaction = factory.functions.createCollectionN2M_000oEFvt(
                collection_information,
                collection_id,
                implementation_type,
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
                raise ValueError("Не удалось получить raw_transaction")
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            tx_hex = tx_hash.hex()
            tx_hex_prefixed = tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex
            logger.info(
                "Транзакция createCollection отправлена: {}",
                f"https://soneium.blockscout.com/tx/{tx_hex_prefixed}",
            )
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            if receipt["status"] == 1:
                logger.info("Деплой коллекции подтверждён")
                return tx_hex
            logger.warning("Транзакция createCollection не прошла (status=0)")
            return None

        except retriable as e:
            if rpc_attempt < RPC_RETRIES:
                time.sleep(5 + (rpc_attempt - 1) * 3)
            else:
                logger.warning("Ошибка деплоя (RPC): {}", e)
                return None
        except Exception as e:
            logger.warning("Ошибка деплоя коллекции: {}", e)
            return None

    return None


async def run(
    page: Any,
    wallet_address: str,
    confirm_claim_in_rabby: Callable[[Any], Awaitable[None]],
    private_key: str,
) -> None:
    """
    Страница квеста → Verify; при «Quest not completed» — до 5 попыток деплоя NFT-коллекции
    (случайные метаданные), ожидание 10 с, повторный Verify → Claim.
    """
    if not CAMPAIGN_URLS:
        return
    url = CAMPAIGN_URLS[0]
    name = url.rstrip("/").split("/")[-1] or url
    if all_quests_already_claimed(wallet_address, [name]):
        logger.success("Квест {}: уже выполнен", name)
        return
    logger.info("Квест NFTs2Me: {}", name)

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
        "Квест {}: не выполнен — деплой NFT-коллекции (до {} попыток ончейна)",
        name,
        MAX_ATTEMPTS,
    )
    UI_RETRIES_AFTER_ONCHAIN = 5

    on_chain_done = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if not on_chain_done:
            logger.info("Попытка ончейна {}/{}: деплой коллекции с рандомными метаданными", attempt, MAX_ATTEMPTS)

            tx_hash = await asyncio.to_thread(_deploy_collection, private_key)
            if not tx_hash:
                if attempt < MAX_ATTEMPTS:
                    delay = random.uniform(5, 15)
                    logger.info("Пауза {:.0f} с перед повтором", delay)
                    await asyncio.sleep(delay)
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
                logger.success("Квест {}: деплой выполнен, награда забрана", name)
                save_completed_quest(wallet_address, name, "verified_and_claimed")
                return
            except Exception as e:
                logger.warning("После деплоя Verify/Claim (попытка UI {}/{}): {}", ui_retry, UI_RETRIES_AFTER_ONCHAIN, e)
                if ui_retry < UI_RETRIES_AFTER_ONCHAIN:
                    await asyncio.sleep(random.uniform(QUEST_UI_RETRY_DELAY_MIN, QUEST_UI_RETRY_DELAY_MAX))

        if on_chain_done:
            logger.warning(
                "Квест {}: ончейн выполнен, но Verify/Claim не удался после {} попыток UI — новых деплоев не делаем",
                name,
                UI_RETRIES_AFTER_ONCHAIN,
            )
            return

    logger.warning(
        "Квест {}: не удалось выполнить ончейн за {} попыток",
        name,
        MAX_ATTEMPTS,
    )
