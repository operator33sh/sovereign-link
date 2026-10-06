"""Unit tests for tools/vision_analyze.py — no real HTTP or LLM calls."""

from __future__ import annotations

import base64
import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.vision_analyze import (
    _DEFAULT_PROMPT,
    _RESIZE_THRESHOLD,
    _detect_mime,
    _load_image,
    _resize_if_needed,
    vision_analyze,
)


# ---------------------------------------------------------------------------
# Minimal valid image fixtures
# ---------------------------------------------------------------------------

def _make_jpeg_bytes(size: int = 100) -> bytes:
    """Return a minimal JPEG header + padding to reach `size` bytes."""
    header = b"\xff\xd8\xff\xe0" + b"\x00" * 12  # 16 bytes
    return header + b"\x00" * max(0, size - len(header))


def _make_png_bytes(size: int = 100) -> bytes:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)
    return header


def _make_webp_bytes(size: int = 100) -> bytes:
    return b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * (size - 12)


# ---------------------------------------------------------------------------
# _detect_mime
# ---------------------------------------------------------------------------

class TestDetectMime(unittest.TestCase):
    def test_jpeg(self):
        self.assertEqual(_detect_mime(_make_jpeg_bytes()), "image/jpeg")

    def test_png(self):
        self.assertEqual(_detect_mime(_make_png_bytes()), "image/png")

    def test_webp(self):
        self.assertEqual(_detect_mime(_make_webp_bytes()), "image/webp")

    def test_unsupported_raises(self):
        with self.assertRaises(ValueError):
            _detect_mime(b"\x00\x01\x02\x03unknown")

    def test_riff_non_webp_raises(self):
        data = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 10
        with self.assertRaises(ValueError):
            _detect_mime(data)


# ---------------------------------------------------------------------------
# _resize_if_needed
# ---------------------------------------------------------------------------

class TestResizeIfNeeded(unittest.TestCase):
    def test_small_image_unchanged(self):
        data = _make_jpeg_bytes(100)
        result, mime = _resize_if_needed(data, "image/jpeg")
        self.assertEqual(result, data)
        self.assertEqual(mime, "image/jpeg")

    def test_large_image_resized(self):
        from PIL import Image
        img = Image.new("RGB", (3000, 2000), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        large_bytes = buf.getvalue() * ((_RESIZE_THRESHOLD // len(buf.getvalue())) + 2)
        # Build truly large bytes by repeating — but that won't be valid JPEG.
        # Instead, create a large valid JPEG directly.
        img2 = Image.new("RGB", (4000, 3000), color=(0, 128, 255))
        buf2 = io.BytesIO()
        img2.save(buf2, format="JPEG", quality=99)
        big = buf2.getvalue()
        if len(big) <= _RESIZE_THRESHOLD:
            self.skipTest("Cannot generate >2 MB JPEG in this environment")
        resized, new_mime = _resize_if_needed(big, "image/jpeg")
        self.assertEqual(new_mime, "image/jpeg")
        self.assertLess(len(resized), len(big))

    def test_mime_updated_to_jpeg_after_resize(self):
        from PIL import Image as PILImage
        # Build a PNG that is definitely > 2 MB by creating a large uncompressed image
        img = PILImage.new("RGB", (1000, 1000), color=(0, 128, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=0)
        png_bytes = buf.getvalue()
        # Pad if still under threshold
        if len(png_bytes) <= _RESIZE_THRESHOLD:
            png_bytes = png_bytes + b"\x00" * (_RESIZE_THRESHOLD + 1 - len(png_bytes))

        mock_img = MagicMock()
        mock_img.size = (4000, 3000)
        mock_img.resize.return_value = mock_img
        mock_img.convert.return_value = mock_img

        def fake_save(buf, format, quality):
            buf.write(b"\xff\xd8\xff" + b"\x00" * 10)

        mock_img.save.side_effect = fake_save

        with patch("tools.vision_analyze.Image.open", return_value=mock_img):
            result_bytes, result_mime = _resize_if_needed(png_bytes, "image/png")

        self.assertEqual(result_mime, "image/jpeg")


# ---------------------------------------------------------------------------
# _load_image — source type detection
# ---------------------------------------------------------------------------

class TestLoadImageDetection(unittest.TestCase):
    def test_url_http(self):
        jpeg = _make_jpeg_bytes(200)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content.return_value = [jpeg]
        with patch("requests.get", return_value=mock_resp) as mock_get:
            data, mime = _load_image("http://example.com/img.jpg")
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        self.assertIn("User-Agent", headers)
        self.assertEqual(mime, "image/jpeg")

    def test_url_https(self):
        png = _make_png_bytes(200)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content.return_value = [png]
        with patch("requests.get", return_value=mock_resp):
            data, mime = _load_image("https://example.com/img.png")
        self.assertEqual(mime, "image/png")

    def test_local_file(self):
        import tempfile
        jpeg = _make_jpeg_bytes(200)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
            f.write(jpeg)
            path = f.name
        try:
            data, mime = _load_image(path)
            self.assertEqual(data, jpeg)
            self.assertEqual(mime, "image/jpeg")
        finally:
            os.unlink(path)

    def test_base64_string(self):
        jpeg = _make_jpeg_bytes(200)
        b64 = base64.b64encode(jpeg).decode()
        data, mime = _load_image(b64)
        self.assertEqual(data, jpeg)
        self.assertEqual(mime, "image/jpeg")

    def test_invalid_source_raises(self):
        with self.assertRaises(ValueError):
            _load_image("not-a-url-not-a-file-not-valid-base64!!!")

    def test_url_exceeds_size_limit(self):
        chunk = b"\xff\xd8\xff" + b"\x00" * 65536
        # Simulate chunks totalling > 10 MB
        large_chunks = [chunk] * 160  # 160 * ~65 KB = ~10.4 MB
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content.return_value = iter(large_chunks)
        with patch("requests.get", return_value=mock_resp):
            with self.assertRaises(ValueError, msg="Should raise on oversized download"):
                _load_image("https://example.com/huge.jpg")

    def test_unreachable_url_raises(self):
        import requests as _req
        with patch("requests.get", side_effect=_req.exceptions.ConnectionError("refused")):
            with self.assertRaises(_req.exceptions.ConnectionError):
                _load_image("https://unreachable.invalid/img.jpg")

    def test_unsupported_mime_raises(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bmp") as f:
            f.write(b"BM" + b"\x00" * 50)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                _load_image(path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# vision_analyze tool handler (full flow, mocked LLM)
# ---------------------------------------------------------------------------

class TestVisionAnalyzeTool(unittest.TestCase):
    def _mock_load_and_call(self, analysis: str = "A red circle on white background."):
        jpeg = _make_jpeg_bytes(200)
        b64 = base64.b64encode(jpeg).decode()

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content.return_value = [jpeg]

        with patch("requests.get", return_value=mock_resp), \
             patch("tools.vision_analyze._call_vision_llm", return_value=analysis) as mock_llm:
            result = vision_analyze({"image_source": "https://example.com/img.jpg"})

        mock_llm.assert_called_once()
        return result

    def test_returns_analysis_string(self):
        result = self._mock_load_and_call("A red circle.")
        self.assertEqual(result, "A red circle.")

    def test_default_prompt_used_when_none_given(self):
        jpeg = _make_jpeg_bytes(200)
        with patch("tools.vision_analyze._load_image", return_value=(jpeg, "image/jpeg")), \
             patch("tools.vision_analyze._call_vision_llm", return_value="ok") as mock_llm:
            vision_analyze({"image_source": "https://example.com/img.jpg"})
        _, _, prompt_arg = mock_llm.call_args[0]
        self.assertEqual(prompt_arg, _DEFAULT_PROMPT)

    def test_custom_prompt_forwarded(self):
        jpeg = _make_jpeg_bytes(200)
        with patch("tools.vision_analyze._load_image", return_value=(jpeg, "image/jpeg")), \
             patch("tools.vision_analyze._call_vision_llm", return_value="ok") as mock_llm:
            vision_analyze({"image_source": "https://example.com/img.jpg", "prompt": "Is there a cat?"})
        _, _, prompt_arg = mock_llm.call_args[0]
        self.assertEqual(prompt_arg, "Is there a cat?")

    def test_missing_image_source_returns_error(self):
        result = vision_analyze({})
        self.assertIn("Error", result)
        self.assertIn("image_source", result)

    def test_load_error_returns_error_string(self):
        with patch("tools.vision_analyze._load_image", side_effect=ValueError("bad url")):
            result = vision_analyze({"image_source": "https://bad"})
        self.assertIn("Error", result)
        self.assertIn("bad url", result)

    def test_llm_error_returns_error_string(self):
        jpeg = _make_jpeg_bytes(200)
        with patch("tools.vision_analyze._load_image", return_value=(jpeg, "image/jpeg")), \
             patch("tools.vision_analyze._call_vision_llm", side_effect=RuntimeError("model down")):
            result = vision_analyze({"image_source": "https://example.com/img.jpg"})
        self.assertIn("Error", result)


if __name__ == "__main__":
    unittest.main()
