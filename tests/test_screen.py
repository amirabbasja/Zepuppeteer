"""Recognition, stale-coordinate protection and driver-free launch contracts."""
import asyncio
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zepuppeteer.brave import native_command
from zepuppeteer.models import Profile
from zepuppeteer.screen import (AmbiguousTarget, Box, Desktop, ScreenError, ScreenPage,
                               TargetNotFound, Word, crop_region, image_boxes, launch_browser, read_words, text_boxes)


class RecognitionTests(unittest.TestCase):
    def test_words_match_only_whole_labels_and_preserve_duplicates(self):
        words = [Word(text, Box(x, y, 30, 15), (0, y, 0))
                 for text, x, y in [("File", 10, 10), ("Run", 60, 10), ("all", 100, 10),
                                    ("Profile", 10, 40), ("File", 10, 80)]]
        self.assertEqual(text_boxes(words, "Run all"), [Box(60, 10, 70, 15)])
        self.assertEqual(len(text_boxes(words, "File")), 2)
        self.assertEqual(text_boxes(words, "Pro"), [])
        self.assertEqual(text_boxes(words, re.compile("Run all")), [Box(60, 10, 70, 15)])

    def test_region_validation(self):
        from PIL import Image
        picture = Image.new("RGB", (1000, 800))
        cropped, x, y = crop_region(picture, (0.5, 0.25, 0.4, 0.5))
        self.assertEqual((cropped.size, x, y), ((400, 400), 500, 200))
        for region in ((0, 0, 2, 1), (0, -1, 1, 1), (0, 0, 0, 1), (0, 0, float("nan"), 1)):
            with self.subTest(region=region), self.assertRaises(ValueError):
                crop_region(picture, region)

    def test_template_matching_finds_actual_pixels_and_duplicates(self):
        from PIL import Image
        import numpy as np
        template = Image.fromarray(np.random.default_rng(7).integers(0, 256, (24, 24, 3), dtype=np.uint8))
        picture = Image.new("RGB", (300, 200), "white")
        picture.paste(template, (60, 90))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "icon.png"
            template.save(path)
            self.assertEqual(image_boxes(picture, path), [Box(60, 90, 24, 24)])
            picture.paste(template, (200, 90))
            self.assertEqual(len(image_boxes(picture, path)), 2)
            self.assertEqual(image_boxes(picture, path, (0, 0, 0.5, 1)), [Box(60, 90, 24, 24)])

    def test_ocr_maps_scaled_crop_coordinates_back_to_window(self):
        from PIL import Image
        data = dict(text=["Apply", "noise"], conf=[96, 12], left=[20, 80], top=[30, 20], width=[90, 20],
                    height=[30, 20], block_num=[1, 2], par_num=[1, 1], line_num=[1, 1])
        with patch("pytesseract.image_to_data", return_value=data):
            words = read_words(Image.new("RGB", (1000, 800)), (0.5, 0.25, 0.5, 0.5))
        self.assertEqual(words, [Word("Apply", Box(510, 215, 45, 15), (1, 1, 1))])


class InputTests(unittest.IsolatedAsyncioTestCase):
    def page(self):
        desktop = SimpleNamespace(capture=Mock(return_value=(object(), Box(30, 40, 1000, 800))), click=Mock())
        return ScreenPage(SimpleNamespace(desktop=desktop, targets={}, target_root=Path("/tmp")))

    async def test_duplicate_text_never_clicks(self):
        page = self.page()
        with patch("zepuppeteer.screen.read_words", return_value=[]), \
                patch("zepuppeteer.screen.text_boxes", return_value=[Box(1, 2, 3, 4), Box(7, 8, 3, 4)]):
            with self.assertRaises(AmbiguousTarget):
                await page.click_text("Apply", timeout=0.01)
        page.browser.desktop.click.assert_not_called()

    async def test_missing_text_never_clicks(self):
        page = self.page()
        with patch("zepuppeteer.screen.read_words", return_value=[]):
            with self.assertRaises(TargetNotFound):
                await page.click_text("Apply", timeout=0)
        page.browser.desktop.click.assert_not_called()

    async def test_target_must_stay_in_place_for_second_capture(self):
        page = self.page()
        target = Box(100, 200, 80, 20)
        with patch("zepuppeteer.screen.read_words", return_value=[]), \
                patch("zepuppeteer.screen.text_boxes", side_effect=[[target], [Box(200, 200, 80, 20)]]):
            with self.assertRaises(TargetNotFound):
                await page.click_text("Apply", timeout=0)
        page.browser.desktop.click.assert_not_called()

    async def test_click_uses_confirmed_window_coordinates(self):
        page = self.page()
        target = Box(100, 200, 80, 20)
        with patch("zepuppeteer.screen.read_words", return_value=[]), \
                patch("zepuppeteer.screen.text_boxes", return_value=[target]):
            await page.click_text("Apply", timeout=0)
        page.browser.desktop.click.assert_called_once_with(target, Box(30, 40, 1000, 800))
        self.assertEqual(page.browser.desktop.capture.call_count, 2)

    async def test_headless_fails_before_launch(self):
        with patch("zepuppeteer.screen.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaisesRegex(ScreenError, "headless"):
                await launch_browser("/brave", Profile("test", Path("/tmp/test")), headless=True)
        spawn.assert_not_called()

    async def test_normal_os_process_is_used_and_closed_after_setup_failure(self):
        process = SimpleNamespace(pid=123, returncode=None, terminate=Mock(), wait=AsyncMock())
        with patch("zepuppeteer.screen.validate_desktop"), \
                patch("zepuppeteer.brave.prepare_profile_for_launch"), \
                patch("zepuppeteer.screen.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn, \
                patch("zepuppeteer.screen.Desktop") as desktop, \
                patch("zepuppeteer.screen.os.killpg") as kill:
            desktop.return_value.wait_for_window = AsyncMock(side_effect=ScreenError("no window"))
            with self.assertRaisesRegex(ScreenError, "no window"):
                await launch_browser("/brave", Profile("test", Path("/tmp/test")))
        args = spawn.call_args.args
        self.assertEqual(args[0], "/brave")
        self.assertIn("--user-data-dir=/tmp/test", args)
        self.assertFalse(any("remote-debugging" in arg or "enable-automation" in arg for arg in args))
        kill.assert_called_once_with(123, __import__("signal").SIGTERM)
        desktop.return_value.close.assert_called_once()

    def test_focus_or_window_move_prevents_input(self):
        desktop = Desktop.__new__(Desktop)
        desktop.gui = Mock()
        desktop.bounds = Mock(return_value=Box(20, 20, 1000, 800))
        with self.assertRaises(ScreenError):
            desktop.click(Box(100, 100, 50, 20), Box(10, 20, 1000, 800))
        desktop.gui.click.assert_not_called()

class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_screenshot_survives_lost_focus_at_failure(self):
        import json
        import zipfile
        from PIL import Image
        from zepuppeteer.screen import ScreenTrace
        desktop = SimpleNamespace(capture=Mock(side_effect=[(Image.new("RGB", (100, 100)), Box(0, 0, 100, 100)), ScreenError("lost focus")]))
        trace = ScreenTrace(SimpleNamespace(desktop=desktop))
        await trace.start()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.zip"
            await trace.stop(path)
            with zipfile.ZipFile(path) as archive:
                self.assertIn("start.png", archive.namelist())
                self.assertEqual(json.loads(archive.read("metadata.json"))["end_capture_error"], "lost focus")
        self.assertIsNone(trace.start_image)

class FieldTests(unittest.IsolatedAsyncioTestCase):
    async def test_fill_verifies_visible_input_through_copy(self):
        page = ScreenPage(SimpleNamespace())
        page.hotkey, page.type_text, page.press = AsyncMock(), AsyncMock(), AsyncMock()
        page.copy_editor_text = AsyncMock(return_value="Hello")
        await page.fill_text("Hello")
        page.type_text.assert_awaited_once_with("Hello")
        page.copy_editor_text.assert_awaited_once()
        page.copy_editor_text.return_value = "Wrong field"
        with self.assertRaises(ScreenError):
            await page.fill_text("Hello")

class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_is_verified_before_enter(self):
        page = ScreenPage(SimpleNamespace())
        page.hotkey, page.fill_text, page.press = AsyncMock(), AsyncMock(), AsyncMock()
        await page.goto("https://example.test/notebook")
        page.fill_text.assert_awaited_once_with("https://example.test/notebook")
        page.press.assert_awaited_once_with("enter")
        page.press.reset_mock()
        page.fill_text.side_effect = ScreenError("wrong address")
        with self.assertRaises(ScreenError):
            await page.goto("https://example.test/other")
        page.press.assert_not_awaited()

class BorderedControlTests(unittest.TestCase):
    def test_clean_ocr_fallback_preserves_coordinate_conversion(self):
        from PIL import Image
        picture = Image.new("RGB", (1000, 800), (238, 238, 238))
        data = dict(text=["Account"], conf=[96], left=[20], top=[30], width=[90], height=[30],
                    block_num=[1], par_num=[1], line_num=[1])
        with patch("pytesseract.image_to_data", return_value=data) as ocr:
            words = read_words(picture, (0.5, 0.25, 0.5, 0.5), scale=1, clean=True)
        self.assertEqual(words[0].box, Box(520, 230, 90, 30))
        self.assertEqual(ocr.call_args.args[0].mode, "L")

class AddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_address_copy_rejects_page_content(self):
        from contextlib import contextmanager
        desktop = Desktop.__new__(Desktop)
        clipboard = SimpleNamespace(paste=Mock(side_effect=["Notebook contents", "https://www.kaggle.com/tester"]))
        @contextmanager
        def temporary_clipboard():
            yield clipboard
        desktop.clipboard = temporary_clipboard
        desktop.hotkey, desktop.press = Mock(), Mock()
        self.assertEqual(await desktop.address(), "https://www.kaggle.com/tester")
        desktop.press.assert_called_once_with("esc")
