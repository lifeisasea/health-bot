"""Маршрутизатор моделей: MiniMax — основная, Claude — резерв.

Логика: пробуем MiniMax. Если он упал/пуст/заблокирован модерацией или не вернул
ожидаемый JSON — и задан ключ Claude — повторяем запрос на Claude.
"""
import logging
from typing import Optional

import claude_client
import minimax_client
from minimax_client import extract_json  # noqa: F401  (ре-экспорт для удобства)

log = logging.getLogger("health-bot.llm")


async def chat(
    system: str,
    user_text: str,
    image: Optional[bytes] = None,
    max_tokens: int = 1200,
    temperature: float = 0.4,
    force_claude: bool = False,
) -> str:
    """Текстовый ответ с авто-резервом на Claude."""
    if force_claude and claude_client.available():
        return await claude_client.chat(system, user_text, image, max_tokens, temperature)

    try:
        return await minimax_client.chat(system, user_text, image, max_tokens, temperature)
    except Exception as e:
        if claude_client.available():
            log.warning("MiniMax не справился (%s) → резерв Claude", e)
            return await claude_client.chat(system, user_text, image, max_tokens, temperature)
        raise


async def chat_json(
    system: str,
    user_text: str,
    image: Optional[bytes] = None,
    max_tokens: int = 1200,
) -> Optional[dict]:
    """Ответ, из которого нужен JSON. Если MiniMax не дал валидный JSON — резерв Claude."""
    try:
        raw = await minimax_client.chat(system, user_text, image, max_tokens, temperature=0.2)
        parsed = extract_json(raw)
        if parsed:
            return parsed
        log.warning("MiniMax вернул не-JSON → пробую резерв")
    except Exception as e:
        log.warning("MiniMax не справился при JSON (%s) → резерв", e)

    if claude_client.available():
        raw = await claude_client.chat(system, user_text, image, max_tokens, temperature=0.2)
        return extract_json(raw)
    return None
