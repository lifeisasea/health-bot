"""Маршрутизатор моделей.

Основная модель задаётся в config.PRIMARY_MODEL ("claude" = Sonnet, или "minimax").
Вторая используется как резерв, если основная упала/пуста/не дала JSON.
"""
import logging
from typing import Optional

import claude_client
import config
import minimax_client
from minimax_client import extract_json  # noqa: F401  (ре-экспорт для удобства)

log = logging.getLogger("health-bot.llm")


def _order(force_claude: bool = False) -> list:
    """Порядок провайдеров: [основной, резервный]. Недоступный Claude выкидываем."""
    if force_claude:
        return ["claude"] if claude_client.available() else ["minimax"]
    primary = "claude" if config.PRIMARY_MODEL == "claude" else "minimax"
    order = [primary, "minimax" if primary == "claude" else "claude"]
    return [p for p in order if p != "claude" or claude_client.available()]


async def _call(provider, system, user_text, image, max_tokens, temperature):
    if provider == "claude":
        return await claude_client.chat(
            system, user_text, image=image, max_tokens=max_tokens, temperature=temperature
        )
    return await minimax_client.chat(system, user_text, image, max_tokens, temperature)


async def chat(
    system: str,
    user_text: str,
    image: Optional[bytes] = None,
    max_tokens: int = 1200,
    temperature: float = 0.4,
    force_claude: bool = False,
) -> str:
    """Текстовый ответ: основная модель, при сбое — резерв."""
    order = _order(force_claude)
    last = None
    for i, prov in enumerate(order):
        try:
            return await _call(prov, system, user_text, image, max_tokens, temperature)
        except Exception as e:
            last = e
            if i + 1 < len(order):
                log.warning("%s не справился (%s) → резерв %s", prov, e, order[i + 1])
    raise last if last else RuntimeError("нет доступных моделей")


async def chat_json(
    system: str,
    user_text: str,
    image: Optional[bytes] = None,
    max_tokens: int = 1200,
) -> Optional[dict]:
    """Ответ, из которого нужен JSON. Если основная не дала валидный JSON — резерв."""
    for i, prov in enumerate(_order()):
        try:
            raw = await _call(prov, system, user_text, image, max_tokens, 0.2)
            parsed = extract_json(raw)
            if parsed:
                return parsed
            log.warning("%s вернул не-JSON", prov)
        except Exception as e:
            log.warning("%s не справился при JSON (%s)", prov, e)
    return None


async def extract_labs(text: str, image=None, pdf=None) -> Optional[dict]:
    """Распознать показатели анализа. Приоритет у Claude (зрение/док-ты точнее; PDF только он)."""
    from prompts import lab_extraction_prompt

    sys = lab_extraction_prompt()
    if claude_client.available():
        raw = await claude_client.chat(sys, text, image=image, pdf=pdf, max_tokens=4000, temperature=0.1)
        return extract_json(raw)
    if pdf is not None:
        return None  # PDF без Claude не разобрать
    raw = await minimax_client.chat(sys, text, image=image, max_tokens=4000, temperature=0.1)
    return extract_json(raw)
