"""Fallback providers (request shapes, no network), link sanitising, and real OCR engines."""
from __future__ import annotations

import io
import platform

import pytest

from app.ai.providers import AnthropicProvider, openai_fallback
from app.config import safe_link
from app.ocr import pipeline


def test_anthropic_message_conversion():
    system, conv = AnthropicProvider._convert([
        {"role": "system", "content": "be strict"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "q1"},
        {"role": "user", "content": [{"type": "text", "text": "q2"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
    ])
    assert system == "be strict"
    assert conv[0]["role"] == "user"  # Anthropic conversations must start with a user turn
    assert [m["role"] for m in conv] == ["user", "assistant", "user"]  # consecutive user turns merged
    image = conv[-1]["content"][-1]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}


def test_openai_fallback_body_is_reasoning_model_friendly():
    p = openai_fallback("sk-test", "gpt-5-mini", "https://api.openai.com/v1", 30)
    body = p._body([{"role": "user", "content": "hi"}], 0.7, 500, True, "low", True)
    assert "temperature" not in body and "max_tokens" not in body
    assert body["max_completion_tokens"] == 1500 and body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_object"} and "logprobs" not in body


def test_provider_learns_unsupported_params():
    p = openai_fallback("sk-test", "m", "https://example.com/v1", 30)
    assert p._adapt("Unsupported parameter: 'reasoning_effort'") and not p.send_reasoning_effort
    assert p._adapt("response_format json_object is not supported") and not p.supports_json_mode
    assert not p._adapt("some unrelated error")


@pytest.mark.parametrize("url,expected", [
    ("https://me.vercel.app", "https://me.vercel.app"), ("javascript:alert(1)", ""), ("<your-vercel-url>", ""),
    ("data:text/html,hi", ""), ("", ""), ("http://github.com/u/repo", "http://github.com/u/repo"),
])
def test_safe_link(url, expected):
    assert safe_link(url) == expected


def _code_image():
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 170), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("consola.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 20), "def total(items):", fill="black", font=font)
    draw.text((20, 80), "    return sum(items)", fill="black", font=font)
    return img


@pytest.mark.skipif(pipeline.tesseract_cmd() is None, reason="Tesseract not installed")
def test_tesseract_layer():
    res = pipeline._tesseract(_code_image())
    assert res["ok"] and "total" in res["text"] and res["confidence"] > 0.5
    assert "    return" in res["text"]  # indentation reconstructed from word positions


def test_rapidocr_layer():
    pytest.importorskip("rapidocr_onnxruntime")
    res = pipeline._rapidocr(_code_image())
    assert res["ok"] and "total" in res["text"].replace(" ", "") and res["confidence"] > 0.5


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows OCR only on Windows")
def test_windows_ocr_layer():
    if not pipeline.system_ocr_probe()["available"]:
        pytest.skip("no Windows OCR language pack")
    res = pipeline._windows_ocr(_code_image())
    assert res["ok"] and "total" in res["text"]


def test_load_image_handles_transparency_and_dark_theme():
    from PIL import Image

    rgba = Image.new("RGBA", (50, 20), (0, 0, 0, 0))
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    assert pipeline.load_image(buf.getvalue()).mode == "RGB"
    dark = Image.new("RGB", (200, 50), (10, 10, 10))
    assert pipeline._preprocess(dark).getpixel((5, 5)) > 200  # dark screenshots are inverted for Tesseract
