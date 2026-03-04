# -*- coding: utf-8 -*-
"""Хранилище статусов выполненных квестов (completed_quests.json)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPLETED_QUESTS_PATH = PROJECT_ROOT / "completed_quests.json"


def _load() -> dict[str, Any]:
    """Загружает базу. Формат: {"wallets": {"0x...": {"quests": {"campaign_id": "source"}}}}."""
    if not COMPLETED_QUESTS_PATH.exists():
        return {"wallets": {}}
    try:
        data = json.loads(COMPLETED_QUESTS_PATH.read_text(encoding="utf-8"))
        if "wallets" not in data:
            data = {"wallets": {}}
        for addr, wallet in list(data["wallets"].items()):
            q = wallet.get("quests", {})
            if isinstance(q, list):
                data["wallets"][addr]["quests"] = {
                    item["campaign"]: item["source"]
                    for item in q
                    if isinstance(item, dict) and "campaign" in item and "source" in item
                }
            if "updated_at" in data["wallets"][addr]:
                del data["wallets"][addr]["updated_at"]
        return data
    except (json.JSONDecodeError, OSError):
        return {"wallets": {}}


def campaign_ids_from_urls(urls: list[str]) -> list[str]:
    """Из списка URL кампаний возвращает список campaign_id (последний сегмент URL)."""
    return [u.rstrip("/").split("/")[-1] or u for u in urls]


# Любой из этих статусов означает «квест выполнен, награда забрана» — браузер не нужен
QUEST_COMPLETED_STATUSES = ("already_claimed", "verified_and_claimed", "reward_claimed")


def all_quests_already_claimed(wallet_address: str, campaign_ids: list[str]) -> bool:
    """True, если у кошелька для всех указанных кампаний квест завершён (награда забрана)."""
    if not campaign_ids:
        return False
    data = _load()
    quests = data["wallets"].get(wallet_address, {}).get("quests", {})
    if not isinstance(quests, dict):
        quests = {}
    return all(quests.get(c) in QUEST_COMPLETED_STATUSES for c in campaign_ids)


def save_completed_quest(wallet_address: str, campaign_id: str, source: str) -> None:
    """Сохраняет статус квеста для кошелька. Не дублирует запись при source=already_claimed."""
    data = _load()
    if wallet_address not in data["wallets"]:
        data["wallets"][wallet_address] = {"quests": {}}
    quests = data["wallets"][wallet_address].get("quests", {})
    if not isinstance(quests, dict):
        data["wallets"][wallet_address]["quests"] = {}
        quests = {}
    if source == "already_claimed" and quests.get(campaign_id) == "already_claimed":
        return
    data["wallets"][wallet_address]["quests"][campaign_id] = source
    # Резервная копия перед записью
    if COMPLETED_QUESTS_PATH.exists():
        backup_path = PROJECT_ROOT / "completed_quests.json.bak"
        try:
            backup_path.write_text(COMPLETED_QUESTS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    COMPLETED_QUESTS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.debug(
        "Сохранён выполненный квест: {} ({}) для {}",
        campaign_id,
        source,
        wallet_address[:10] + "…",
    )
