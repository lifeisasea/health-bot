"""Резервный клиент Claude (Anthropic). Используется, когда MiniMax не справился."""
import base64
from typing import Optional

from anthropic import AsyncAnthropic

import config
from minimax_client import downscale_image, _strip_think

_client = (
    AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY) if config.ANTHROPIC_API_KEY else None
)


def available() -> bool:
    return _client is not None


def _img_block(data: bytes, max_side: int) -> dict:
    b64 = base64.b64encode(downscale_image(data, max_side=max_side)).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


async def chat(
    system: str,
    user_text: str,
    image: Optional[bytes] = None,
    images: Optional[list] = None,
    pdf: Optional[bytes] = None,
    max_tokens: int = 1200,
    temperature: float = 0.4,
) -> str:
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY не задан — резерв недоступен")

    content: list = [{"type": "text", "text": user_text}]
    if image is not None:
        content.append(_img_block(image, 1024))
    for im in images or []:  # страницы PDF — крупнее ради читаемости таблиц
        content.append(_img_block(im, 1568))
    if pdf is not None:
        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(pdf).decode(),
                },
            }
        )

    # system как кэшируемый блок: постоянная часть промпта (персона, профиль, анализы)
    # переиспользуется между запросами → повторное чтение в ~10× дешевле (кэш живёт ~5 мин).
    system_block = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    msg = await _client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_block,
        messages=[{"role": "user", "content": content}],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
    return _strip_think("\n".join(parts))
