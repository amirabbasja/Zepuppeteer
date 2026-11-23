from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zepuppeteer.models import NeedsAttention, Profile, TaskContext
from zepuppeteer.tasks.kaggle.open_notebook import run
from zepuppeteer.websites.kaggle import validate_notebook_url
from zepuppeteer.workflows import load_workflow, resolve_tasks


NOTEBOOK_URL = "https://www.kaggle.com/code/test-user/my-notebook/edit"
TASK = "zepuppeteer.tasks.kaggle.open_notebook"


class KaggleConfigurationTests(unittest.TestCase):
    def test_supported_notebook_urls_preserve_queries_and_fragments(self):
        for url in (NOTEBOOK_URL, "https://kaggle.com/code/owner/notebook",
                    "https://www.kaggle.com:443/code/owner/notebook/?scriptVersionId=123#cell-1"):
            with self.subTest(url=url):
                self.assertEqual(validate_notebook_url(f" {url} "), url)

    def test_other_hosts_credentials_and_non_notebook_paths_are_rejected(self):
        for url in (None, 10, "", "http://www.kaggle.com/code/owner/notebook",
                    "https://www.kaggle.com.evil.test/code/owner/notebook",
                    "https://user:password@www.kaggle.com/code/owner/notebook",
                    "https://www.kaggle.com:444/code/owner/notebook", "https://[broken",
                    "https://kaggle.com:invalid/code/owner/notebook", "https://www.kaggle.com/",
                    "https://www.kaggle.com/account/login", "https://www.kaggle.com/code",
                    "https://www.kaggle.com/code/owner/notebook/other",
                    "https://www.kaggle.com/code/owner/../edit"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_notebook_url(url)

    def test_workflow_resolves_the_actual_task(self):
        workflow = load_workflow("config/workflows/kaggle.toml")
        self.assertEqual(workflow.name, "kaggle")
        self.assertEqual(workflow.steps[0].task, "kaggle.open_notebook")
        self.assertIs(resolve_tasks(workflow)["kaggle.open_notebook"], run)


class KaggleTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-kaggle-task-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.page = SimpleNamespace(title=AsyncMock(return_value="My Notebook | Kaggle"), close=AsyncMock(), screenshot=AsyncMock())
        self.browser = SimpleNamespace(new_page=AsyncMock(return_value=self.page))
        self.manifest = self.root / "profile.toml"
        self.write_manifest()
        self.ctx = TaskContext(self.browser, Profile("account-1", self.root / "browser", manifest_path=self.manifest),
                               {}, {}, self.root, "test-run", "kaggle.open_notebook")
        self.site = SimpleNamespace(open_notebook=AsyncMock(return_value={}))
        self.site_patch = patch(f"{TASK}.KaggleSite", return_value=self.site)
        self.site_factory = self.site_patch.start()
        self.addCleanup(self.site_patch.stop)
        self.login_patch = patch(f"{TASK}.check_login", AsyncMock(return_value="logged_in"))
        self.login = self.login_patch.start()
        self.addCleanup(self.login_patch.stop)
        self.runtime_patch = patch(f"{TASK}.check_runtime_connection", AsyncMock(return_value="connected"))
        self.runtime = self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)

    def write_manifest(self, *, username='"test-user"', url=f'"{NOTEBOOK_URL}"', name='"My Notebook"'):
        lines = ["[profile.platforms.kaggle]"]
        for key, value in (("login_username", username), ("notebook_url", url), ("notebook_name", name)):
            if value is not None:
                lines.append(f"{key} = {value}")
        self.manifest.write_text("\n".join(lines), encoding="utf-8")

    async def test_verifies_manifest_account_notebook_and_runtime_then_closes_tab(self):
        result = await run(self.ctx)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.outputs["runtime_status"], "connected")
        self.assertEqual(result.outputs["backend"], "screen")
        self.assertTrue(Path(result.outputs["metadata_path"]).is_file())
        self.page.screenshot.assert_awaited_once()
        self.site_factory.assert_called_once_with(self.page)
        self.site.open_notebook.assert_awaited_once_with(NOTEBOOK_URL)
        self.login.assert_awaited_once_with(self.page, "test-user")
        self.runtime.assert_awaited_once_with(self.page)
        self.page.close.assert_awaited_once()

    async def test_missing_platform_is_skipped_without_opening_a_tab(self):
        for content in ("", '[profile.platforms.colab]\nlogin_username="other@example.test"'):
            with self.subTest(content=content):
                self.manifest.write_text(content)
                self.assertEqual((await run(self.ctx)).status, "skipped")
        no_manifest = replace(self.ctx, profile=replace(self.ctx.profile, manifest_path=None))
        self.assertEqual((await run(no_manifest)).status, "skipped")
        self.browser.new_page.assert_not_awaited()

    async def test_invalid_platform_and_settings_are_rejected_before_opening_a_tab(self):
        self.manifest.write_text('[profile.platforms]\nkaggle="invalid"')
        with self.assertRaisesRegex(ValueError, "TOML table"):
            await run(self.ctx)
        self.write_manifest()
        ctx = replace(self.ctx, profile=replace(self.ctx.profile, settings={"kaggle": "invalid"}))
        with self.assertRaisesRegex(ValueError, "settings.kaggle"):
            await run(ctx)
        self.browser.new_page.assert_not_awaited()

    async def test_required_values_are_validated_before_opening_a_tab(self):
        for values in ({"username": None}, {"username": '"user@example.test"'}, {"username": "42"},
                       {"url": None}, {"url": '"https://wrong.test/"'}, {"name": '" "'}, {"name": "42"}):
            with self.subTest(values=values):
                self.write_manifest(**values)
                with self.assertRaises(ValueError):
                    await run(self.ctx)
        self.browser.new_page.assert_not_awaited()

    async def test_missing_notebook_name_requires_attention_before_opening_a_tab(self):
        self.write_manifest(name=None)
        with self.assertRaisesRegex(NeedsAttention, "notebook_name"):
            await run(self.ctx)
        self.browser.new_page.assert_not_awaited()

    async def test_workflow_params_and_legacy_settings_do_not_override_manifest(self):
        ctx = replace(self.ctx, params={"notebook_url": "https://wrong.test/"},
                      profile=replace(self.ctx.profile, settings={"kaggle": {"notebook_url": "https://wrong.test/"}}))
        await run(ctx)
        self.site.open_notebook.assert_awaited_once_with(NOTEBOOK_URL)

    async def test_unsuitable_login_requires_attention_and_preserves_page(self):
        for status in ("logged_out", "wrong_account", "unknown"):
            with self.subTest(status=status):
                self.login.return_value = status
                with self.assertRaisesRegex(NeedsAttention, status):
                    await run(self.ctx)
        self.runtime.assert_not_awaited()
        self.page.close.assert_not_awaited()

    async def test_wrong_notebook_name_requires_attention_and_preserves_page(self):
        self.page.title.return_value = "Other Notebook | Kaggle"
        with self.assertRaisesRegex(NeedsAttention, "name mismatch"):
            await run(self.ctx)
        self.runtime.assert_not_awaited()
        self.page.close.assert_not_awaited()

    async def test_title_suffixes_and_names_without_suffix_are_supported(self):
        for title in ("My Notebook", "My Notebook | Kaggle", "My Notebook - Kaggle"):
            with self.subTest(title=title):
                self.page.title.return_value = title
                self.assertEqual((await run(self.ctx)).status, "success")

    async def test_unconnected_runtime_requires_attention_and_preserves_page(self):
        for status in ("disconnected", "connecting", "unknown"):
            with self.subTest(status=status):
                self.runtime.return_value = status
                with self.assertRaisesRegex(NeedsAttention, f"runtime is {status}"):
                    await run(self.ctx)
        self.page.close.assert_not_awaited()

    async def test_navigation_failure_preserves_page_for_runner_evidence(self):
        self.site.open_notebook.side_effect = NeedsAttention("Sign-in required")
        with self.assertRaisesRegex(NeedsAttention, "Sign-in"):
            await run(self.ctx)
        self.login.assert_not_awaited()
        self.page.close.assert_not_awaited()
