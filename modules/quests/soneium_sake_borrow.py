# -*- coding: utf-8 -*-
"""
Квест Soneium Score — Sake Finance Borrow: страница кампании, Verify.
Если после Verify квест не выполнен — выходим с сообщением, что сначала нужно выполнить квест sake-deposit
(операция borrow уже делается в квесте sake-deposit).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from loguru import logger

from modules.quests.storage import all_quests_already_claimed, save_completed_quest

CAMPAIGN_URLS = [
    "https://app.arkada.gg/en/campaign/soneium-score-seventh-sake-borrow",
]


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
    logger.info("Квест Sake Borrow: {}", name)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
    except Exception as e:
        logger.warning("Квест {}: не удалось открыть страницу — {}", name, e)
        return

    already_done = page.get_by_text("Congratulations").or_(
        page.get_by_role("button", name="Go to next quest")
    )
    try:
        await already_done.first.wait_for(state="visible", timeout=15000)
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
        await continue_btn.wait_for(state="visible", timeout=20000)
        await continue_btn.click()
        logger.success("Квест {}: награда забрана", name)
        save_completed_quest(wallet_address, name, "reward_claimed")
        return
    except Exception:
        pass

    verify_btn = page.get_by_role("button", name="Verify")
    try:
        await verify_btn.wait_for(state="visible", timeout=20000)
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
        await completed.wait_for(state="visible", timeout=12000)
        claim_btn = page.get_by_role("button", name="Claim Reward")
        await claim_btn.wait_for(state="visible", timeout=10000)
        await claim_btn.click()
        await confirm_claim_in_rabby(page)
        continue_btn = page.get_by_role("button", name="Continue")
        await continue_btn.wait_for(state="visible", timeout=20000)
        await continue_btn.click()
        logger.success("Квест {}: награда забрана после Verify", name)
        save_completed_quest(wallet_address, name, "verified_and_claimed")
        return
    except Exception:
        pass

    try:
        await not_completed.wait_for(state="visible", timeout=5000)
        logger.warning(
            "Квест Sake Borrow не выполнен. Сначала выполните квест sake-deposit."
        )
        return
    except Exception:
        pass

    try:
        await already_done.first.wait_for(state="visible", timeout=5000)
        logger.success("Квест {}: уже выполнен (определено по кнопке Go to next quest)", name)
        save_completed_quest(wallet_address, name, "already_claimed")
        go_next = page.get_by_role("button", name="Go to next quest")
        if await go_next.is_visible():
            await go_next.click()
        return
    except Exception:
        pass

    logger.warning("Квест {}: не удалось определить статус после Verify", name)
