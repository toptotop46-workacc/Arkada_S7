#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arkada — временный профиль с прокси, импорт ключа в Rabby, переход на app.arkada.gg/en/ecosystem/soneium.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from urllib.parse import urlparse

import requests
from loguru import logger
from playwright.async_api import async_playwright
from web3 import Web3

from modules.quests import soneium_kyo_tvl
from modules.quests import soneium_nfts2me
from modules.quests import soneium_sake_borrow
from modules.quests import soneium_sake_deposit
from modules.quests import soneium_sake_tvl
from modules.quests import soneium_score
from modules.quests import soneium_stargate_tvl
from modules.quests import soneium_uniswap
from modules.quests import soneium_untitled_tvl
from modules.quests import soneium_velodrome
from modules.quests.storage import all_quests_already_claimed, campaign_ids_from_urls

# Имена квестов для --quest (тестирование по отдельности)
QUEST_NAMES = ("score", "uniswap", "stargate_tvl", "stargate", "nfts2me", "untitled_tvl", "velodrome", "kyo_tvl", "sake_tvl", "sake_deposit", "sake_borrow")
QUEST_MODULES: dict[str, tuple[Any, bool]] = {
    "score": (soneium_score, False),
    "uniswap": (soneium_uniswap, True),
    "stargate_tvl": (soneium_stargate_tvl, True),
    "stargate": (soneium_stargate_tvl, True),
    "nfts2me": (soneium_nfts2me, True),
    "untitled_tvl": (soneium_untitled_tvl, True),
    "velodrome": (soneium_velodrome, True),
    "kyo_tvl": (soneium_kyo_tvl, True),
    "sake_tvl": (soneium_sake_tvl, True),
    "sake_deposit": (soneium_sake_deposit, True),
    "sake_borrow": (soneium_sake_borrow, True),
}

# Для компактного логирования порядка квестов (используем канонические имена, без алиасов типа stargate->stargate_tvl)
_MODULE_TO_QUEST_KEY: dict[Any, str] = {}
for _key, (_mod, _needs_pk) in QUEST_MODULES.items():
    if _key == "stargate":
        continue
    _MODULE_TO_QUEST_KEY.setdefault(_mod, _key)


def _randomize_runners_sake_first(
    runners: list[tuple[Any, bool]],
) -> list[tuple[Any, bool]]:
    """Случайный порядок квестов, но Sake Deposit всегда первый (если присутствует)."""
    if len(runners) <= 1:
        return runners
    sake = [r for r in runners if r[0] is soneium_sake_deposit]
    rest = [r for r in runners if r[0] is not soneium_sake_deposit]
    random.shuffle(rest)
    return sake + rest


def _campaign_urls_for_filter(quest_filter: Optional[list[str]]) -> list[str]:
    """Список URL кампаний: все при quest_filter=None, иначе только выбранные квесты."""
    if quest_filter is None:
        return (
            soneium_score.CAMPAIGN_URLS
            + soneium_uniswap.CAMPAIGN_URLS
            + soneium_stargate_tvl.CAMPAIGN_URLS
            + soneium_nfts2me.CAMPAIGN_URLS
            + soneium_untitled_tvl.CAMPAIGN_URLS
            + soneium_velodrome.CAMPAIGN_URLS
            + soneium_kyo_tvl.CAMPAIGN_URLS
            + soneium_sake_tvl.CAMPAIGN_URLS
            + soneium_sake_deposit.CAMPAIGN_URLS
            + soneium_sake_borrow.CAMPAIGN_URLS
        )
    seen: set[Any] = set()
    urls: list[str] = []
    for name in quest_filter:
        key = name if name != "stargate" else "stargate_tvl"
        if key not in QUEST_MODULES or QUEST_MODULES[key][0] in seen:
            continue
        seen.add(QUEST_MODULES[key][0])
        urls.extend(QUEST_MODULES[key][0].CAMPAIGN_URLS)
    return urls

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

ARKADA_URL = "https://app.arkada.gg/en/ecosystem/soneium"
RABBY_EXTENSION_ID = "acmacodkjbdgmoleebolmdjonilkdbch"


def _is_rabby_popup_url(url: str) -> bool:
    """Проверяет, что URL — окно расширения Rabby (notification.html или popup)."""
    if not url or not url.startswith("chrome-extension://"):
        return False
    prefix = f"chrome-extension://{RABBY_EXTENSION_ID}/"
    return url.startswith(prefix) or "notification.html" in url or "popup.html" in url


TMP_PROFILES_DIR = PROJECT_ROOT / "tmp_profiles"
RABBY_EXTENSION_PATH = PROJECT_ROOT / "Rabby-Wallet-Chrome"

PROXY_CHECK_TIMEOUT = 12
PROXY_CHECK_URLS = (ARKADA_URL,)
# Headless-проверка прокси: ждём появления контента (суммарно до ~35 с)
PROXY_HEADLESS_GOTO_TIMEOUT_MS = 10_000
PROXY_HEADLESS_CONTENT_TIMEOUT_MS = 25_000
PROXY_HEADLESS_CONTENT_TEXT = "Explore more quests"


def load_private_key(key_index: int = 0) -> str:
    """Загружает приватный ключ из keys.txt по индексу."""
    keys_file = PROJECT_ROOT / "keys.txt"
    if not keys_file.exists():
        raise FileNotFoundError(
            f"Файл {keys_file} не найден. Создайте файл и укажите в нём приватные ключи."
        )
    keys = []
    with open(keys_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                if re.match(r"^0x[a-fA-F0-9]{64}$", line):
                    keys.append(line)
                elif re.match(r"^[a-fA-F0-9]{64}$", line):
                    keys.append("0x" + line)
    if not keys:
        raise ValueError(f"В файле {keys_file} не найдено действительных приватных ключей")
    if key_index < 0 or key_index >= len(keys):
        raise ValueError(
            f"Индекс ключа {key_index} вне диапазона (доступно: {len(keys)})"
        )
    return keys[key_index]


def load_all_keys() -> list[str]:
    """Загружает все приватные ключи из keys.txt."""
    keys_file = PROJECT_ROOT / "keys.txt"
    if not keys_file.exists():
        raise FileNotFoundError(f"Файл {keys_file} не найден.")
    keys = []
    with open(keys_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                if re.match(r"^0x[a-fA-F0-9]{64}$", line):
                    keys.append(line)
                elif re.match(r"^[a-fA-F0-9]{64}$", line):
                    keys.append("0x" + line)
    if not keys:
        raise ValueError(f"В файле {keys_file} не найдено действительных приватных ключей")
    return keys


def get_address_for_key_index(key_index: int) -> str:
    """Возвращает EOA-адрес (checksum) для ключа по индексу в keys.txt."""
    private_key = load_private_key(key_index)
    return Web3.to_checksum_address(Web3().eth.account.from_key(private_key).address)


def _extension_id_from_path(extension_path: Path) -> str:
    """
    Вычисляет ID расширения для распакованной папки так же, как Chrome.
    """
    path = extension_path.resolve()
    path_str = str(path)
    if sys.platform == "win32" and len(path_str) >= 2 and path_str[1] == ":":
        path_str = path_str[0].upper() + path_str[1:]
    path_bytes = path_str.encode("utf-16-le") if sys.platform == "win32" else path_str.encode("utf-8")
    digest = hashlib.sha256(path_bytes).digest()[:16]
    hex_str = digest.hex()
    return "".join(chr(ord("a") + int(c, 16)) for c in hex_str)


def _proxy_to_playwright(host: str, port: str, user: str, password: str) -> dict[str, Any]:
    """Собирает один прокси в формате Playwright."""
    out: dict[str, Any] = {"server": f"http://{host}:{port}"}
    if user and password:
        out["username"] = user
        out["password"] = password
    return out


def _proxy_to_requests(host: str, port: str, user: str, password: str) -> dict[str, str]:
    """Собирает один прокси в формате для requests."""
    if user and password:
        proxy_url = f"http://{user}:{password}@{host}:{port}"
    else:
        proxy_url = f"http://{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def _load_proxy_lines() -> list[tuple[str, str, str, str]]:
    """Загружает список прокси из proxy.txt: (host, port, user, password)."""
    proxy_file = PROJECT_ROOT / "proxy.txt"
    if not proxy_file.exists():
        return []
    lines = []
    with open(proxy_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) >= 2:
                host, port = parts[0], parts[1]
                user = (parts[2] or "") if len(parts) > 3 else ""
                password = (parts[3] or "") if len(parts) > 3 else ""
                lines.append((host, port, user, password))
    return lines


def _test_proxy(proxies_dict: dict[str, str]) -> bool:
    """Проверяет, открывается ли через прокси app.arkada.gg (быстрый отсев через requests)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }
    for url in PROXY_CHECK_URLS:
        try:
            r = requests.get(url, headers=headers, proxies=proxies_dict, timeout=PROXY_CHECK_TIMEOUT)
            if not r.ok:
                return False
        except Exception:
            return False
    return True


async def _test_proxy_headless_async(proxy_playwright: dict[str, Any]) -> bool:
    """
    Проверяет прокси в headless-браузере: открывает ARKADA_URL и ждёт появления текста
    «Soneium Score Campaign». Без расширений. Возвращает True только если контент отрисовался.
    """
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy_playwright,
        )
        try:
            page = await browser.new_page()
            await page.goto(
                ARKADA_URL,
                wait_until="domcontentloaded",
                timeout=PROXY_HEADLESS_GOTO_TIMEOUT_MS,
            )
            await page.get_by_text(PROXY_HEADLESS_CONTENT_TEXT).wait_for(
                state="visible",
                timeout=PROXY_HEADLESS_CONTENT_TIMEOUT_MS,
            )
            return True
        finally:
            await browser.close()
    except Exception:
        return False
    finally:
        await pw.stop()


async def get_working_proxy_playwright_async() -> Optional[dict[str, Any]]:
    """
    Перебирает прокси из proxy.txt: быстрый отсев (requests), затем headless-проверка
    с ожиданием контента Arkada. Без proxy.txt возвращает None.
    """
    lines = _load_proxy_lines()
    if not lines:
        return None
    order = list(lines)
    random.shuffle(order)
    for host, port, user, password in order:
        req_proxy = _proxy_to_requests(host, port, user, password)
        if not _test_proxy(req_proxy):
            logger.debug("Прокси недоступен (requests), следующий: {}:{}", host, port)
            continue
        pw_proxy = _proxy_to_playwright(host, port, user, password)
        if await _test_proxy_headless_async(pw_proxy):
            logger.debug("Прокси проверен (headless): {}:{}", host, port)
            return pw_proxy
        logger.debug("Прокси не отрисовал контент (headless), следующий: {}:{}", host, port)
    raise ValueError(
        "Не удалось найти рабочий прокси (проверка app.arkada.gg + headless). "
        "Проверьте proxy.txt или доступность сайта."
    )


def get_working_proxy_playwright() -> Optional[dict[str, Any]]:
    """
    Синхронная обёртка: запускает get_working_proxy_playwright_async().
    Без proxy.txt возвращает None. Если ни один прокси не прошёл проверку — ValueError.
    """
    return asyncio.run(get_working_proxy_playwright_async())


class ArkadaBrowser:
    """Временный профиль Playwright + прокси, импорт кошелька в Rabby, открытие Arkada Soneium."""

    async def _import_wallet_impl(
        self, context: Any, private_key: str, password: str = "Password123"
    ) -> None:
        """Импортирует кошелёк в Rabby (context — BrowserContext от Playwright)."""
        ext_id = _extension_id_from_path(RABBY_EXTENSION_PATH) if RABBY_EXTENSION_PATH.exists() else RABBY_EXTENSION_ID
        setup_url = f"chrome-extension://{ext_id}/index.html#/new-user/guide"

        page = None
        for attempt in range(15):
            for p in context.pages:
                if "chrome-extension://" in p.url and ("rabby" in p.url.lower() or "index.html" in p.url):
                    page = p
                    try:
                        ext_id = urlparse(p.url).netloc or ext_id
                    except Exception:
                        pass
                    setup_url = f"chrome-extension://{ext_id}/index.html#/new-user/guide"
                    break
            if page:
                break
            await asyncio.sleep(0.3)
        if not page:
            logger.warning("Страница расширения не появилась, ищем ID через chrome://extensions/")
            ext_page = await context.new_page()
            try:
                await ext_page.goto("chrome://extensions/", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
                ext_id = await ext_page.evaluate("""
                    () => {
                        const items = document.querySelectorAll('extensions-item');
                        for (const item of items) {
                            const nameEl = item.querySelector('#name') || item.querySelector('[id="name"]') || item.querySelector('.name');
                            const name = nameEl ? nameEl.textContent || '' : '';
                            if (name.toLowerCase().includes('rabby')) {
                                const id = (item.id || item.getAttribute('id') || '').replace('extension-', '');
                                if (id) return id;
                            }
                        }
                        const links = document.querySelectorAll('a[href^="chrome-extension://"]');
                        for (const a of links) {
                            const m = (a.href || '').match(/chrome-extension:\\/\\/([a-p]{32})\\//);
                            if (m && (a.textContent || '').toLowerCase().includes('rabby')) return m[1];
                        }
                        return null;
                    }
                """)
            finally:
                await ext_page.close()
            if ext_id:
                setup_url = f"chrome-extension://{ext_id}/index.html#/new-user/guide"
                logger.info("ID расширения из chrome://extensions/: {}", ext_id)
                page = await context.new_page()
                await page.goto(setup_url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
            else:
                any_page = context.pages[0] if context.pages else await context.new_page()
                cdp = await context.new_cdp_session(any_page)
                try:
                    await cdp.send("Target.createTarget", {"url": setup_url})
                except Exception as e:
                    raise RuntimeError(
                        "Не удалось открыть расширение Rabby. Проверьте папку Rabby-Wallet-Chrome и перезапустите."
                    ) from e
                for _ in range(50):
                    await asyncio.sleep(0.3)
                    for p in context.pages:
                        if "chrome-extension://" in p.url and "#/new-user/guide" in p.url:
                            page = p
                            break
                    if not page:
                        for p in context.pages:
                            if "chrome-extension://" in p.url:
                                page = p
                                break
                    if page:
                        break
                if not page:
                    raise RuntimeError(
                        "Вкладка расширения Rabby не появилась после Target.createTarget. Перезапустите."
                    )
                await asyncio.sleep(2)
        if page and "#/new-user/guide" not in page.url:
            await page.goto(setup_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
        if page:
            await page.reload()
            await asyncio.sleep(2)

        await page.wait_for_selector('span:has-text("I already have an address")', timeout=30000)
        await page.click('span:has-text("I already have an address")')
        await page.wait_for_selector('div.rabby-ItemWrapper-rabby--mylnj7:has-text("Private Key")', timeout=30000)
        await page.click('div.rabby-ItemWrapper-rabby--mylnj7:has-text("Private Key")')
        await page.wait_for_selector("#privateKey", timeout=30000)
        await page.fill("#privateKey", private_key)
        await page.wait_for_selector('button:has-text("Confirm"):not([disabled])', timeout=30000)
        await page.click('button:has-text("Confirm"):not([disabled])')
        await page.wait_for_selector("#password", timeout=30000)
        await page.fill("#password", password)
        await page.press("#password", "Tab")
        await page.keyboard.type(password)
        await page.wait_for_selector('button:has-text("Confirm"):not([disabled])', timeout=30000)
        await page.click('button:has-text("Confirm"):not([disabled])')
        await page.wait_for_selector("text=Imported Successfully", timeout=30000)
        logger.success("Кошелёк импортирован в Rabby")
        await page.close()
        logger.info("Вкладка импорта кошелька закрыта")

    async def _open_arkada(
        self,
        context: Any,
        wallet_address: str = "",
        private_key: str = "",
        quest_filter: Optional[list[str]] = None,
    ) -> None:
        """Открывает https://app.arkada.gg/en/ecosystem/soneium в браузере. quest_filter: только эти квесты (score, uniswap, stargate_tvl), иначе все."""
        page = None
        for p in context.pages:
            if not p.url.startswith("chrome-extension://"):
                page = p
                break
        if not page:
            page = await context.new_page()
        await page.goto(ARKADA_URL, wait_until="load", timeout=60000)
        logger.success("Открыта страница: {}", ARKADA_URL)
        # Кнопка "Sign in" без привязки к header; ждём появления и кликаем
        sign_in_btn = page.get_by_role("button", name="Sign in").first
        await sign_in_btn.wait_for(state="visible", timeout=45000)
        await sign_in_btn.click()
        logger.success('Нажата кнопка "Sign in"')
        # Модалка "Connect a Wallet": выбираем Rabby Wallet по data-testid (RainbowKit)
        rabby_btn = page.locator('[data-testid="rk-wallet-option-io.rabby"]')
        await rabby_btn.wait_for(state="visible", timeout=15000)
        await rabby_btn.click()
        logger.success('Выбран Rabby Wallet в модалке подключения')
        # Окно расширения (notification.html или popup): 1) перезагрузка, 2) клик Connect
        async def _reload_then_connect(popup_page: Any) -> None:
            await popup_page.reload(wait_until="domcontentloaded", timeout=15000)
            logger.success("Окно расширения Rabby перезагружено")
            connect_btn = popup_page.get_by_role("button", name="Connect")
            await connect_btn.wait_for(state="visible", timeout=10000)
            await connect_btn.click()
            logger.success('Нажата кнопка "Connect" в окне Rabby')

        await asyncio.sleep(2)
        popup_page = None
        for p in context.pages:
            if _is_rabby_popup_url(p.url):
                popup_page = p
                break
        if not popup_page:
            try:
                popup_page = await context.wait_for_event(
                    "page",
                    predicate=lambda p: _is_rabby_popup_url(p.url),
                    timeout=12000,
                )
            except Exception:
                pass
        if popup_page:
            await _reload_then_connect(popup_page)
        else:
            logger.warning("Окно расширения Rabby (notification.html/popup) не найдено")

        # После Connect: либо "Create a new account" (новый пользователь), либо сразу "Sign in to Arkada"
        create_btn = page.get_by_role("button", name="Create account")
        try:
            await create_btn.wait_for(state="visible", timeout=8000)
            await create_btn.click()
            logger.success('Нажата кнопка "Create account" в модалке Arkada')
        except Exception:
            logger.info("Модалка Create account не показана (аккаунт уже создан)")

        # В обоих случаях далее — модалка "Sign in to Arkada": кликаем шаг "Sign Message"
        sign_message_row = page.get_by_text("Sign Message").first
        await sign_message_row.wait_for(state="visible", timeout=15000)
        await sign_message_row.click()
        logger.success('Нажат шаг "Sign Message" в модалке Sign in to Arkada')

        # Снова открывается popup кошелька: перезагрузка → Sign → Confirm
        await asyncio.sleep(2)
        popup_page2 = None
        for p in context.pages:
            if _is_rabby_popup_url(p.url):
                popup_page2 = p
                break
        if not popup_page2:
            try:
                popup_page2 = await context.wait_for_event(
                    "page",
                    predicate=lambda p: _is_rabby_popup_url(p.url),
                    timeout=12000,
                )
            except Exception:
                pass
        if popup_page2:
            await popup_page2.reload(wait_until="domcontentloaded", timeout=15000)
            logger.success("Окно расширения Rabby перезагружено (Sign Message)")
            sign_btn = popup_page2.get_by_role("button", name="Sign")
            await sign_btn.wait_for(state="visible", timeout=10000)
            await sign_btn.click()
            logger.success('Нажата кнопка "Sign" в окне Rabby')
            confirm_btn = popup_page2.get_by_role("button", name="Confirm")
            await confirm_btn.wait_for(state="visible", timeout=10000)
            await confirm_btn.click()
            logger.success('Нажата кнопка "Confirm" в окне Rabby')
        else:
            logger.warning("Окно расширения Rabby не найдено для Sign Message")

        # В модалке "Sign in to Arkada" кнопка Sign In станет активной — кликаем
        sign_in_btn_modal = page.get_by_role("button", name="Sign In")
        await sign_in_btn_modal.wait_for(state="visible", timeout=5000)
        await sign_in_btn_modal.click(timeout=20000)
        logger.success('Нажата кнопка "Sign In" в модалке Arkada')

        campaign_urls = _campaign_urls_for_filter(quest_filter)
        total_quests = len(campaign_urls)
        logger.info("Квестов в списке: {}", total_quests)

        if quest_filter is None:
            runners = [
                (soneium_score, False),
                (soneium_uniswap, True),
                (soneium_stargate_tvl, True),
                (soneium_nfts2me, True),
                (soneium_untitled_tvl, True),
                (soneium_velodrome, True),
                (soneium_kyo_tvl, True),
                (soneium_sake_tvl, True),
                (soneium_sake_deposit, True),
                (soneium_sake_borrow, True),
            ]
        else:
            seen: set[Any] = set()
            runners = []
            for name in quest_filter:
                key = name if name != "stargate" else "stargate_tvl"
                if key not in QUEST_MODULES:
                    continue
                mod, needs_pk = QUEST_MODULES[key]
                if mod in seen:
                    continue
                seen.add(mod)
                runners.append((mod, needs_pk))

        runners = _randomize_runners_sake_first(runners)
        # Не шумим в консоли: порядок пишем только в DEBUG (он попадёт в файл логов)
        try:
            order = " -> ".join(_MODULE_TO_QUEST_KEY.get(mod, getattr(mod, "__name__", str(mod))) for mod, _ in runners)
            logger.debug("Порядок квестов: {}", order)
        except Exception:
            pass

        for mod, needs_pk in runners:
            if needs_pk:
                await mod.run(
                    page, wallet_address, self._confirm_claim_in_rabby_popup, private_key
                )
            else:
                await mod.run(page, wallet_address, self._confirm_claim_in_rabby_popup)

    async def _confirm_claim_in_rabby_popup(self, page: Any) -> None:
        """После Claim Reward: найти popup Rabby, перезагрузить, нажать Sign и Confirm."""
        await asyncio.sleep(2)
        context = page.context
        popup_page = None
        for p in context.pages:
            if _is_rabby_popup_url(p.url):
                popup_page = p
                break
        if not popup_page:
            try:
                popup_page = await context.wait_for_event(
                    "page",
                    predicate=lambda p: _is_rabby_popup_url(p.url),
                    timeout=12000,
                )
            except Exception:
                pass
        if not popup_page:
            logger.warning("Popup Rabby не найден для подтверждения Claim")
            return
        await popup_page.reload(wait_until="domcontentloaded", timeout=15000)
        logger.info("Popup кошелька перезагружен (Claim)")
        sign_btn = popup_page.get_by_role("button", name="Sign")
        await sign_btn.wait_for(state="visible", timeout=10000)
        await sign_btn.click()
        logger.success('Нажата кнопка "Sign" в popup Rabby')
        confirm_btn = popup_page.get_by_role("button", name="Confirm")
        await confirm_btn.wait_for(state="visible", timeout=10000)
        await confirm_btn.click()
        logger.success('Нажата кнопка "Confirm" в popup Rabby')

async def _run_one_async(
    manager: ArkadaBrowser,
    key_index: int,
    wallet_password: str,
    use_proxy: bool,
    wait_for_user: bool,
    quest_filter: Optional[list[str]] = None,
) -> bool:
    """Один цикл: временный профиль → импорт кошелька в Rabby → переход на Arkada Soneium. В конце профиль удаляется. quest_filter: только эти квесты, иначе все."""
    private_key = load_private_key(key_index=key_index)
    address = Web3.to_checksum_address(Web3().eth.account.from_key(private_key).address)
    logger.info("Кошелёк: {}", address)

    all_campaign_urls = _campaign_urls_for_filter(quest_filter)
    campaign_ids = campaign_ids_from_urls(all_campaign_urls)
    if campaign_ids and all_quests_already_claimed(address, campaign_ids):
        logger.info("Все квесты уже заклаймлены для кошелька")
        return True

    TMP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profile_dir = TMP_PROFILES_DIR / uuid.uuid4().hex

    if use_proxy:
        logger.info("Проверка прокси (app.arkada.gg)...")
        proxy = await get_working_proxy_playwright_async()
        if proxy:
            logger.info("Профиль создаётся с прокси: {}", proxy["server"])
        else:
            proxy = None
    else:
        proxy = None

    extension_args: list[str] = []
    if RABBY_EXTENSION_PATH.exists() and (RABBY_EXTENSION_PATH / "manifest.json").exists():
        ext_path_abs = str(RABBY_EXTENSION_PATH.resolve())
        extension_args = [
            f"--disable-extensions-except={ext_path_abs}",
            f"--load-extension={ext_path_abs}",
            "--no-first-run",
            "--disable-web-security",
        ]
    else:
        logger.warning("Папка Rabby-Wallet-Chrome не найдена или без manifest.json — расширение Rabby не загружено")

    # Окно уводим за пределы экрана (headed нужен для расширений кошелька)
    window_off_screen = "--window-position=10000,10000"
    launch_args = [*extension_args, window_off_screen]

    launch_kw: dict[str, Any] = {
        "headless": False,
        "locale": "en-US",
        "viewport": {"width": 1200, "height": 800},
        "ignore_default_args": ["--disable-extensions"],
    }
    if proxy:
        launch_kw["proxy"] = proxy
    launch_kw["args"] = launch_args

    playwright = await async_playwright().start()
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            **launch_kw,
        )
        try:
            # Отключаем загрузку картинок для экономии трафика прокси
            async def _block_images(route):
                if route.request.resource_type == "image":
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", _block_images)

            await asyncio.sleep(5)
            await manager._import_wallet_impl(context, private_key, password=wallet_password)
            await manager._open_arkada(
                context,
                wallet_address=address,
                private_key=private_key,
                quest_filter=quest_filter,
            )

            if wait_for_user:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: input("Готово. Закройте браузер или нажмите Enter для остановки профиля.\n"),
                )
        finally:
            try:
                await context.close()
            except Exception as e:
                if "Connection closed" not in str(e) and "Target closed" not in str(e):
                    logger.debug("При закрытии контекста: {}", e)
    finally:
        try:
            await playwright.stop()
        except Exception as e:
            if "Connection closed" not in str(e):
                logger.debug("При остановке Playwright: {}", e)
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
            logger.info("Временный профиль удалён: {}", profile_dir.name)
    return True


def run_one(
    manager: ArkadaBrowser,
    key_index: int = 0,
    wallet_password: str = "Password123",
    use_proxy: bool = True,
    wait_for_user: bool = True,
    quest_filter: Optional[list[str]] = None,
) -> bool:
    """Синхронная обёртка: один цикл для ключа key_index. quest_filter: только эти квесты (score, uniswap, stargate_tvl), иначе все."""
    try:
        return asyncio.run(
            _run_one_async(
                manager,
                key_index,
                wallet_password,
                use_proxy,
                wait_for_user,
                quest_filter,
            )
        )
    except KeyboardInterrupt:
        logger.warning("Прервано пользователем (Ctrl+C)")
        raise


def run() -> None:
    """Точка входа: запуск для первого ключа из keys.txt (с ожиданием пользователя по умолчанию)."""
    logger.remove(0)  # только дефолтный stderr; файл из main.py сохраняем
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
    )
    try:
        all_keys = load_all_keys()
        logger.info("Загружено ключей: {}", len(all_keys))
        if not all_keys:
            logger.error("Нет ключей в keys.txt")
            return
        manager = ArkadaBrowser()
        # Проверка прокси только если есть proxy.txt — иначе запуск без прокси
        use_proxy = (PROJECT_ROOT / "proxy.txt").exists()
        if use_proxy:
            get_working_proxy_playwright()
        run_one(manager, key_index=0, use_proxy=use_proxy, wait_for_user=True)
    except FileNotFoundError as e:
        logger.error(str(e))
        raise SystemExit(1)
    except ValueError as e:
        logger.error(str(e))
        raise SystemExit(1)


def run_all(
    wait_for_user_on_last: bool = True,
    quest_filter: Optional[list[str]] = None,
) -> None:
    """Запуск для всех ключей из keys.txt по очереди (каждый в своём временном профиле). quest_filter: только эти квесты, иначе все."""
    logger.remove(0)  # только дефолтный stderr; файл из main.py сохраняем
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
    )
    try:
        all_keys = load_all_keys()
        logger.info("Загружено ключей: {}", len(all_keys))
        if not all_keys:
            logger.error("Нет ключей в keys.txt")
            return
        manager = ArkadaBrowser()
        use_proxy = (PROJECT_ROOT / "proxy.txt").exists()
        if use_proxy:
            get_working_proxy_playwright()
        for i in range(len(all_keys)):
            logger.info("Обработка ключа {}/{}", i + 1, len(all_keys))
            try:
                run_one(
                    manager,
                    key_index=i,
                    use_proxy=use_proxy,
                    wait_for_user=wait_for_user_on_last and (i == len(all_keys) - 1),
                    quest_filter=quest_filter,
                )
            except Exception as e:
                try:
                    address = get_address_for_key_index(i)
                except Exception:
                    address = "?"
                logger.warning(
                    "Ошибка при обработке ключа {}/{}, кошелёк {}: {}: {}",
                    i + 1,
                    len(all_keys),
                    address,
                    type(e).__name__,
                    e,
                )
    except FileNotFoundError as e:
        logger.error(str(e))
        raise SystemExit(1)
    except ValueError as e:
        logger.error(str(e))
        raise SystemExit(1)
