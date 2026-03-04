# -*- coding: utf-8 -*-
"""Квест кампании Soneium Score: переход по URL, Verify / Claim Reward, подтверждение в кошельке."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from loguru import logger

from modules.quests.storage import all_quests_already_claimed, save_completed_quest

CAMPAIGN_URLS = [
    "https://app.arkada.gg/en/campaign/soneium-score-seventh-50",
]


async def run(
    page: Any,
    wallet_address: str,
    confirm_claim_in_rabby: Callable[[Any], Awaitable[None]],
) -> None:
    """Переход по CAMPAIGN_URLS: уже выполнен / Claim Reward / Verify → Claim → подтверждение в кошельке."""
    if not CAMPAIGN_URLS:
        return
    for url in CAMPAIGN_URLS:
        name = url.rstrip("/").split("/")[-1] or url
        if all_quests_already_claimed(wallet_address, [name]):
            logger.success("Квест {}: уже выполнен", name)
            continue
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
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
                continue
            except Exception:
                pass
            claim_btn = page.get_by_role("button", name="Claim Reward")
            try:
                await claim_btn.wait_for(state="visible", timeout=8000)
                logger.info("Квест {}: награда не была заклаймлена — забираем", name)
                await claim_btn.click()
                await confirm_claim_in_rabby(page)
                continue_btn = page.get_by_role("button", name="Continue")
                await continue_btn.wait_for(state="visible", timeout=20000)
                await continue_btn.click()
                logger.success("Квест {}: награда забрана, нажата Continue", name)
                save_completed_quest(wallet_address, name, "reward_claimed")
                continue
            except Exception:
                pass
            verify_btn = page.get_by_role("button", name="Verify")
            try:
                await verify_btn.wait_for(state="visible", timeout=20000)
            except Exception:
                try:
                    await already_done.first.wait_for(state="visible", timeout=3000)
                    logger.success(
                        "Квест {}: уже выполнен ранее (определено после ожидания Verify)",
                        name,
                    )
                    save_completed_quest(wallet_address, name, "already_claimed")
                    go_next = page.get_by_role("button", name="Go to next quest")
                    if await go_next.is_visible():
                        await go_next.click()
                except Exception:
                    logger.warning("Квест {}: кнопка Verify не появилась за 20 с", name)
                continue
            await verify_btn.click()
            completed = page.get_by_text("Quest completed")
            not_completed = page.get_by_text("Quest not completed")
            try:
                await completed.wait_for(state="visible", timeout=15000)
                claim_btn = page.get_by_role("button", name="Claim Reward")
                await claim_btn.wait_for(state="visible", timeout=10000)
                await claim_btn.click()
                await confirm_claim_in_rabby(page)
                continue_btn = page.get_by_role("button", name="Continue")
                await continue_btn.wait_for(state="visible", timeout=20000)
                await continue_btn.click()
                logger.success("Квест {}: награда забрана, нажата Continue", name)
                save_completed_quest(wallet_address, name, "verified_and_claimed")
            except Exception:
                try:
                    await not_completed.wait_for(state="visible", timeout=5000)
                    alert_btn = page.locator("role=alert").get_by_role("button").first
                    if await alert_btn.is_visible():
                        await alert_btn.click()
                    logger.info("Квест {}: ещё не выполнен", name)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Квест {}: {} — {}", name, type(e).__name__, e)
        await asyncio.sleep(1)
