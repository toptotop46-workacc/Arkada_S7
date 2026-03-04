# Квесты Arkada: хранилище статусов и раннеры по кампаниям.

from modules.quests.storage import (
    all_quests_already_claimed,
    campaign_ids_from_urls,
    save_completed_quest,
)

__all__ = [
    "all_quests_already_claimed",
    "campaign_ids_from_urls",
    "save_completed_quest",
]
