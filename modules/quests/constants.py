# -*- coding: utf-8 -*-
"""Общие константы для квестов: паузы между шагами, RPC, сеть."""

from __future__ import annotations

from pathlib import Path

# Паузы между ончейн-шагами (секунды)
PAUSE_MIN = 10
PAUSE_MAX = 30
# Короткая пауза после апрувов (секунды)
PAUSE_SHORT_MIN = 1
PAUSE_SHORT_MAX = 3

# UI Verify/Claim на app.arkada.gg: таймауты и паузы (уменьшают Locator.wait_for/click timeout)
# После page.goto даём SPA и бэкенду время подгрузить статус квеста
QUEST_UI_WAIT_AFTER_GOTO_SEC = 5
# Таймауты ожидания элементов (мс)
QUEST_UI_VERIFY_TIMEOUT_MS = 30_000
QUEST_UI_QUEST_COMPLETED_TIMEOUT_MS = 25_000
QUEST_UI_CLAIM_TIMEOUT_MS = 15_000
QUEST_UI_CONTINUE_TIMEOUT_MS = 25_000
QUEST_UI_ALREADY_DONE_TIMEOUT_MS = 15_000
# Пауза между повторными попытками Verify/Claim (секунды), чтобы бэкенд успел проиндексировать
QUEST_UI_RETRY_DELAY_MIN = 10
QUEST_UI_RETRY_DELAY_MAX = 20

# RPC и сеть Soneium (можно переопределить через config.json)
_CONFIG_ROOT = Path(__file__).resolve().parents[2]


def _load_config() -> dict:
    """Загружает config.json из корня проекта. Пустой dict при отсутствии."""
    config_path = _CONFIG_ROOT / "config.json"
    if not config_path.exists():
        return {}
    try:
        import json
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_soneium_rpc_url() -> str:
    """RPC URL для Soneium. По умолчанию publicnode, можно задать в config.json (soneium_rpc_url)."""
    return _load_config().get("soneium_rpc_url") or "https://soneium-rpc.publicnode.com"


def get_soneium_chain_id() -> int:
    """Chain ID Soneium. По умолчанию 1868, можно задать в config.json (soneium_chain_id)."""
    val = _load_config().get("soneium_chain_id")
    return int(val) if val is not None else 1868
