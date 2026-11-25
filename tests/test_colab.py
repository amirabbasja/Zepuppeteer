from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zepuppeteer.models import NeedsAttention, Profile, TaskContext
from zepuppeteer.tasks.colab.open_notebook import run
from zepuppeteer.websites.colab import DEFAULT_NOTEBOOK_URL, validate_notebook_url
from zepuppeteer.workflows import load_workflow, resolve_tasks


class ColabConfigurationTests(unittest.TestCase):
    def test_supported_urls_preserve_notebook_paths_queries_and_fragments(self):
        for url in [DEFAULT_NOTEBOOK_URL,
                    "https://colab.research.google.com/drive/notebook-id?authuser=1#scrollTo=cell-id",
                    "https://colab.research.google.com/github/org/repo/blob/main/example.ipynb"]:
            with self.subTest(url=url):
                self.assertEqual(validate_notebook_url(url), url)

    def test_invalid_urls_are_rejected(self):
        for url in [None, 10, "", "http://colab.research.google.com/drive/id",
                    "https://accounts.google.com/", "https://colab.research.google.com.evil.test/",
                    "https://user:password@colab.research.google.com/drive/id",
                    "https://colab.research.google.com:444/drive/id", "https://[broken",
                    "https://colab.research.google.com:invalid/drive/id"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_notebook_url(url)

    def test_workflow_resolves_the_actual_task(self):
        workflow = load_workflow("config/workflows/colab.toml")
        self.assertEqual(workflow.steps[0].task, "colab.open_notebook")
        self.assertIs(resolve_tasks(workflow)["colab.open_notebook"], run)


class ColabTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-colab-task-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        def screenshot(*, path, timeout):
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.page = SimpleNamespace(url=DEFAULT_NOTEBOOK_URL,
                                    screenshot=AsyncMock(side_effect=screenshot), close=AsyncMock())
        self.browser = SimpleNamespace(new_page=AsyncMock(return_value=self.page))
        self.ctx = TaskContext(self.browser, Profile("account-1", self.root / "browser"), {}, {}, self.root,
                               "test-run", "colab.open_notebook")

    def manifest_context(self):
        manifest = self.root / "profile.toml"
        manifest.write_text('[profile.platforms.colab]\nlogin_username="account@example.test"\n'
                            'worker_url="https://colab.research.google.com/drive/private-notebook"\n')
        return replace(self.ctx, profile=replace(self.ctx.profile, manifest_path=manifest))

    async def test_manifest_login_uses_created_page_and_configured_account(self):
        ctx = self.manifest_context()
        site = SimpleNamespace(open_notebook=AsyncMock(return_value={"title": "Notebook"}))
        with patch("zepuppeteer.tasks.colab.open_notebook.ColabSite", return_value=site), \
                patch("zepuppeteer.tasks.colab.open_notebook.check_login", AsyncMock(return_value="logged_in")) as login, \
                patch("zepuppeteer.tasks.colab.open_notebook.time.sleep"):
            result = await run(ctx)
        self.assertEqual(result.status, "success")
        login.assert_awaited_once_with(self.page, "account@example.test")
        site.open_notebook.assert_awaited_once_with("https://colab.research.google.com/drive/private-notebook")

    async def test_unsuitable_login_requires_attention_and_preserves_page(self):
        ctx = self.manifest_context()
        for status in ("logged_out", "wrong_account", "unknown"):
            with self.subTest(status=status), \
                    patch("zepuppeteer.tasks.colab.open_notebook.ColabSite") as site, \
                    patch("zepuppeteer.tasks.colab.open_notebook.check_login", AsyncMock(return_value=status)):
                site.return_value.open_notebook = AsyncMock(return_value={})
                with self.assertRaisesRegex(NeedsAttention, status):
                    await run(ctx)
        self.page.close.assert_not_awaited()
        self.page.screenshot.assert_not_awaited()

    async def test_google_login_redirect_is_logged_out_before_editor_loads(self):
        for locale in ("en", "fa", "es", "ja"):
            for ctx in (self.ctx, self.manifest_context()):
                with self.subTest(locale=locale, expected_account=bool(ctx.profile.manifest_path)):
                    self.page.url = f"https://accounts.google.com/v3/signin/identifier?hl={locale}"
                    error = NeedsAttention("Colab requires Google sign-in or consent.")
                    site = SimpleNamespace(open_notebook=AsyncMock(side_effect=error))
                    with patch("zepuppeteer.tasks.colab.open_notebook.ColabSite", return_value=site), \
                            patch("zepuppeteer.tasks.colab.open_notebook.check_login", AsyncMock()) as login:
                        with self.assertRaisesRegex(NeedsAttention, "logged_out") as raised:
                            await run(ctx)
                    self.assertIs(raised.exception.__cause__, error)
                    login.assert_not_awaited()
        self.page.close.assert_not_awaited()
        self.page.screenshot.assert_not_awaited()

    async def test_other_attention_errors_are_not_reported_as_logged_out(self):
        for url in (DEFAULT_NOTEBOOK_URL, "https://consent.google.com/",
                    "https://accounts.google.com.example.test/"):
            with self.subTest(url=url):
                self.page.url = url
                error = NeedsAttention("Manual attention required.")
                site = SimpleNamespace(open_notebook=AsyncMock(side_effect=error))
                with patch("zepuppeteer.tasks.colab.open_notebook.ColabSite", return_value=site):
                    with self.assertRaises(NeedsAttention) as raised:
                        await run(self.ctx)
                self.assertIs(raised.exception, error)

    async def test_url_precedence_and_artifact_contents(self):
        configured = "https://colab.research.google.com/drive/profile-notebook"
        override = "https://colab.research.google.com/drive/workflow-notebook"
        profile = replace(self.ctx.profile, settings={"colab": {"notebook_url": configured}})
        cases = [(self.ctx, DEFAULT_NOTEBOOK_URL),
                 (replace(self.ctx, profile=profile), configured),
                 (replace(self.ctx, profile=profile, params={"notebook_url": override}), override)]
        for ctx, expected in cases:
            with self.subTest(expected=expected):
                site = SimpleNamespace(open_notebook=AsyncMock(return_value={
                    "notebook_url": expected, "title": "Example notebook - Colab"}))
                with patch("zepuppeteer.tasks.colab.open_notebook.ColabSite", return_value=site):
                    result = await run(ctx)
                self.assertEqual(result.status, "success")
                site.open_notebook.assert_awaited_once_with(expected)
                self.assertEqual(result.outputs["notebook_url"], expected)
                metadata = json.loads(Path(result.outputs["metadata_path"]).read_text())
                self.assertEqual(metadata["profile"], "account-1")
                self.assertEqual(metadata["requested_url"], expected)
                self.assertTrue(Path(result.outputs["screenshot_path"]).read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(self.page.close.await_count, len(cases))

    async def test_failures_leave_the_page_for_runner_evidence(self):
        site = SimpleNamespace(open_notebook=AsyncMock(side_effect=NeedsAttention("Sign in required")))
        with patch("zepuppeteer.tasks.colab.open_notebook.ColabSite", return_value=site):
            with self.assertRaises(NeedsAttention):
                await run(self.ctx)
        self.page.close.assert_not_awaited()
        self.page.screenshot.assert_not_awaited()

    async def test_invalid_configuration_fails_before_opening_a_tab(self):
        cases = [replace(self.ctx, params={"notebook_url": "https://wrong.test/"}),
                 replace(self.ctx, params={"run_all": True}),
                 replace(self.ctx, profile=replace(self.ctx.profile, settings={"colab": "invalid"}))]
        for ctx in cases:
            with self.subTest(ctx=ctx), self.assertRaises(ValueError):
                await run(ctx)
        self.browser.new_page.assert_not_awaited()
