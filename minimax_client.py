"""Обёртка над MiniMax (OpenAI-совместимый API).

- Чистит ответ от блока рассуждений <think>...</think>.
- Поддерживает изображения (фото еды, сканы анализов).
- Картинки ужимаются перед отправкой ради экономии токенов.
"""
import base64
import io
import json
import re
from typing import Optional

from openai import AsyncOpenAI
from PIL import Image

import config

_client = AsyncOpenAI(api_key=config.MINIMAX_API_KEY, base_url=config.MINIMAX_BASE_URL)


class ProviderError(Exception):
    """MiniMax не смог дать содержательный ответ (пусто/модерация/ошибка)."""

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def downscale_image(data: bytes, max_side: int = 1024) -> bytes:
    """Ужать картинку до max_side по большей стороне, отдать JPEG."""
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return data  # если не вышло — отправим как есть


def pdf_to_images(pdf_bytes: bytes, max_pages: int = 10, dpi: int = 150) -> list:
    """Отрендерить страницы PDF в JPEG-картинки (для надёжного OCR сканов)."""
    try:
        import fitz  # pymupdf
    except Exception:
        return []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []
    out = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        try:
            out.append(page.get_pixmap(dpi=dpi).tobytes("jpeg"))
        except Exception:
            continue
    return out


def _image_part(data: bytes) -> dict:
    b64 = base64.b64encode(downscale_image(data)).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


async def chat(
    system: str,
    user_text: str,
    image: Optional[bytes] = None,
    max_tokens: int = 1200,
    temperature: float = 0.4,
) -> str:
    """Один запрос к модели. Вернуть чистый текст ответа."""
    content: list | str
    if image is not None:
        content = [{"type": "text", "text": user_text}, _image_part(image)]
    else:
        content = user_text

    resp = await _client.chat.completions.create(
        model=config.MINIMAX_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    choice = resp.choices[0]
    text = _strip_think(choice.message.content)
    if not text:
        # пустой ответ или срабатывание модерации — повод уйти на резерв
        raise ProviderError(f"empty/blocked (finish_reason={choice.finish_reason})")
    return text


def extract_json(text: str) -> Optional[dict]:
    """Вытащить первый JSON-объект из ответа модели."""
    text = _strip_think(text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
