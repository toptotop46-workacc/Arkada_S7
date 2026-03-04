#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Логирование в файл (до импорта arkada, чтобы логгер был настроен)
def _setup_logging() -> None:
    from loguru import logger
    project_root = Path(__file__).resolve().parent
    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "arkada_{time:YYYY-MM-DD}.log"
    logger.add(
        log_file,
        rotation="1 day",
        retention=14,
        level="DEBUG",
        encoding="utf-8",
    )


def main() -> None:
    _setup_logging()
    from modules.arkada import run_all as arkada_run_all

    parser = argparse.ArgumentParser(
        description="Arkada Soneium: квесты в браузере с Rabby.",
    )
    parser.add_argument(
        "--quest",
        metavar="NAME",
        choices=["score", "uniswap", "stargate_tvl", "stargate", "nfts2me", "untitled_tvl", "velodrome", "kyo_tvl", "sake_tvl", "sake_deposit", "sake_borrow"],
        help="Запустить только указанный квест (score, uniswap, stargate_tvl, nfts2me, untitled_tvl, velodrome, kyo_tvl, sake_tvl, sake_deposit, sake_borrow). Без флага — все квесты.",
    )
    args = parser.parse_args()

    quest_filter: list[str] | None = None
    if args.quest is not None:
        quest_filter = [args.quest]
        # для единообразия в логах и проверках используем stargate_tvl
        if args.quest == "stargate":
            quest_filter = ["stargate_tvl"]

    arkada_run_all(quest_filter=quest_filter)


if __name__ == "__main__":
    main()
