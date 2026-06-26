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


async def chat(
    system: str,
    user_text: str,
    image: Optional[bytes] = None,
    pdf: Optional[bytes] = None,
    max_tokens: int = 1200,
    temperature: float = 0.4,
) -> str:
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY не задан — резерв недоступен")

    content: list = [{"type": "text", "text": user_text}]
    if image is not None:
        b64 = base64.b64encode(downscale_image(image)).decode()
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            }
        )
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

    msg = await _client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
    return _strip_think("\n".join(parts))
