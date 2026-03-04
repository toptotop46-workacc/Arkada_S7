# -*- coding: utf-8 -*-
"""Вывод ETH с MEXC на адрес в L2 (OP/BASE/ARB)."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from typing import Any, Optional

import requests
from loguru import logger

MEXC_BASE = "https://api.mexc.com"
CONFIG_FILENAME = "mexc_api.txt"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
RECV_WINDOW = 15000  # 15 с (макс 60000) — меньше шансов 700003 при расхождении часов
TIMEOUT = 30
TIME_OFFSET_REFRESH_INTERVAL = 300  # обновлять смещение времени раз в 5 минут

# Кэш смещения времени локальной машины относительно сервера MEXC (мс)
_time_offset_ms: Optional[int] = None
_time_offset_updated: float = 0.0

# Маппинг сетей для вывода ETH (netWork в API MEXC)
ETH_NETWORKS = ["ARBITRUM ONE(ARB)", "OPTIMISM(OP)", "BASE"]


def _load_config() -> dict[str, str]:
    """Загружает apiKey и secretKey из mexc_api.txt в корне проекта."""
    path = PROJECT_ROOT / CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    except Exception as e:
        logger.debug("Ошибка чтения {}: {}", CONFIG_FILENAME, e)
        return {}


def _sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest().lower()


def _get_server_time_offset_ms() -> int:
    """Смещение (мс) времени сервера MEXC относительно локального. Возвращает 0 при ошибке."""
    try:
        r = requests.get(f"{MEXC_BASE}/api/v3/time", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        server_ms = int(data.get("serverTime", 0))
        return server_ms - int(time.time() * 1000)
    except Exception as e:
        logger.debug("MEXC время сервера: {}", e)
        return 0


def _timestamp_ms() -> int:
    """Текущий timestamp в мс, скорректированный по времени сервера MEXC (избегаем 700003)."""
    global _time_offset_ms, _time_offset_updated
    now = time.monotonic()
    if _time_offset_ms is None or (now - _time_offset_updated) > TIME_OFFSET_REFRESH_INTERVAL:
        _time_offset_ms = _get_server_time_offset_ms()
        _time_offset_updated = now
    return int(time.time() * 1000) + (_time_offset_ms or 0)


def _signed_request(method: str, endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
    config = _load_config()
    api_key = config.get("apiKey") or config.get("api_key")
    secret = config.get("secretKey") or config.get("secret_key")
    if not api_key or not secret:
        logger.warning("MEXC: не найдены apiKey/secretKey в {}", CONFIG_FILENAME)
        return None
    params = dict(params or {})
    params["timestamp"] = str(_timestamp_ms())
    params["recvWindow"] = str(RECV_WINDOW)
    # Подпись считается по строке в алфавитном порядке; запрос отправляем с той же строкой,
    # иначе MEXC вернёт 700002 (signature not valid)
    query_parts = sorted(params.items())
    query = "&".join(f"{k}={v}" for k, v in query_parts)
    params["signature"] = _sign(secret, query)
    query_with_sig = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    url = f"{MEXC_BASE}{endpoint}"
    headers = {"X-MEXC-APIKEY": api_key}
    try:
        if method.upper() == "GET":
            r = requests.get(f"{url}?{query_with_sig}", headers=headers, timeout=TIMEOUT)
        else:
            r = requests.post(f"{url}?{query_with_sig}", data=None, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        result = r.json()
        # MEXC может вернуть 200 с телом {"code": 7000xx, "msg": "..."}
        if isinstance(result, dict) and "code" in result:
            code = result.get("code")
            if code is not None and code != 0 and str(code) != "0":
                logger.warning("MEXC code={} msg={}", code, result.get("msg", ""))
                return None
        return result
    except requests.RequestException as e:
        logger.warning("MEXC API ошибка: {}", e)
        if hasattr(e, "response") and e.response is not None:
            resp = getattr(e.response, "text", None)
            if resp:
                logger.warning("MEXC ответ: {}", resp[:500])
            try:
                body = e.response.json()
                if isinstance(body, dict) and (body.get("code") or body.get("msg")):
                    logger.warning("MEXC code={} msg={}", body.get("code"), body.get("msg"))
            except Exception:
                pass
        return None


def get_withdraw_networks(coin: str = "ETH") -> list[dict[str, Any]]:
    """Список сетей для вывода монеты (из capital/config/getall). Возвращает networkList для ETH."""
    raw = _signed_request("GET", "/api/v3/capital/config/getall")
    if raw is None:
        return []
    # Поддержка обёртки {"data": [...]} на случай смены формата API
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not data or not isinstance(data, list):
        return []
    for item in data:
        if isinstance(item, dict) and (item.get("coin") or item.get("name")) == coin:
            nets = item.get("networkList") or []
            return [n for n in nets if isinstance(n, dict) and n.get("withdrawEnable") and n.get("netWork")]
    return []


def get_eth_withdraw_networks() -> list[dict[str, Any]]:
    """Сети для вывода ETH с полями netWork, withdrawMin, withdrawFee, withdrawMax."""
    networks = get_withdraw_networks("ETH")
    # Оставляем только OP/BASE/ARB (по netWork)
    allowed = {"ARBITRUM ONE(ARB)", "OPTIMISM(OP)", "BASE", "ARB", "OP"}
    out = []
    for n in networks:
        nw = (n.get("netWork") or n.get("network") or "").upper()
        if not nw:
            continue
        if any(a in nw or nw in a for a in allowed) or nw in ("ARBITRUM", "OPTIMISM"):
            try:
                n["withdrawMin"] = float(n.get("withdrawMin") or 0)
                n["withdrawFee"] = float(n.get("withdrawFee") or 0)
                n["withdrawMax"] = float(n.get("withdrawMax") or 0) if n.get("withdrawMax") not in (None, "") else float("inf")
            except (TypeError, ValueError):
                n["withdrawMin"] = 0.0
                n["withdrawFee"] = 0.0
                n["withdrawMax"] = float("inf")
            out.append(n)
    return out


def withdraw(coin: str, address: str, amount: float, net_work: str) -> Optional[str]:
    """
    Вывод с MEXC. coin (например ETH), address, amount (число), net_work (значение netWork из get_eth_withdraw_networks).
    Возвращает id заявки на вывод или None.
    """
    amount_str = str(round(float(amount), 8))
    params = {
        "coin": coin,
        "address": address,
        "amount": amount_str,
        "netWork": net_work,
    }
    data = _signed_request("POST", "/api/v3/capital/withdraw", params=params)
    if data and isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    return None
