from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from zepuppeteer.models import NeedsAttention
from zepuppeteer.tasks.colab.open_notebook import keepAlive

class KeepAliveTests(unittest.IsolatedAsyncioTestCase):
    def page(self, targets=None):
        return SimpleNamespace(browser=SimpleNamespace(targets=targets or {}), hotkey=AsyncMock(), press=AsyncMock(),
            click_target=AsyncMock(), click_text=AsyncMock(), wait_for_target=AsyncMock(),
            copy_editor_text=AsyncMock(), type_text=AsyncMock())
    async def test_invalid_choices(self):
        for choice in (None, 0, 6, True, "1"):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                await keepAlive(self.page(), task_number=choice)
    async def test_missing_editor_or_saved_signal_never_modifies(self):
        page = self.page()
        with self.assertRaises(NeedsAttention):
            await keepAlive(page, task_number=5)
        page.click_target.assert_not_awaited()
        page.type_text.assert_not_awaited()
    async def test_missing_marker_leaves_source_unchanged(self):
        page = self.page({"colab_first_cell_editor": {"text": "Cell"}, "colab_saved": {"text": "Saved"}})
        page.copy_editor_text.return_value = "print('keep me')"
        with self.assertRaisesRegex(NeedsAttention, "marker"):
            await keepAlive(page, task_number=5)
        page.type_text.assert_not_awaited()
    async def test_preserves_prefix_and_confirms_editor_before_saving(self):
        page = self.page({"colab_first_cell_editor": {"text": "Cell"}, "colab_saved": {"text": "Saved"}})
        source = "print('keep me')\n# Status update Comments\n# old comment"
        async def copied():
            return page.type_text.call_args.args[0] if page.type_text.await_count else source
        page.copy_editor_text.side_effect = copied
        self.assertEqual(await keepAlive(page, task_number=5), 5)
        updated = page.type_text.call_args.args[0]
        self.assertTrue(updated.startswith("print('keep me')\n# Status update Comments\n# Time:"))
        self.assertNotIn("old comment", updated)
        page.wait_for_target.assert_awaited_once_with("colab_saved", timeout=15)
    async def test_insertion_requires_delete_target_before_insert(self):
        page = self.page()
        with self.assertRaises(NeedsAttention):
            await keepAlive(page, task_number=4)
        page.click_text.assert_not_awaited()
