"""Kaggle DOM fixtures are intercepted locally; these tests never contact Kaggle."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zepuppeteer.brave import find_brave, launch_options
from zepuppeteer.models import NeedsAttention, Profile, TaskContext
from zepuppeteer.tasks.kaggle.open_notebook import check_login, check_runtime_connection, run
from zepuppeteer.websites.kaggle import KaggleSite


BRAVE = find_brave()
NOTEBOOK_URL = "https://www.kaggle.com/code/test-user/my-notebook"
EDITOR = '''<!doctype html><title>My Notebook | Kaggle</title>
<nav aria-label="Main menu, search and your account">
<a href="/test-user">Your Profile</a></nav>
<button>Save Version</button><div class="jp-Notebook">Notebook</div>
<button>Draft Session: Running</button>'''


@unittest.skipUnless(BRAVE and importlib.util.find_spec("playwright"), "Requires installed Brave and Playwright")
class KaggleBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-kaggle-browser-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile("kaggle-test", self.root / "browser")
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch_persistent_context(
            **launch_options(BRAVE, self.profile, True))
        self.addAsyncCleanup(self.browser.close)

    async def serve(self, html, status=200):
        async def respond(route):
            await route.fulfill(status=status, content_type="text/html; charset=utf-8", body=html)
        await self.browser.route("**/*", respond)

    async def test_login_status_uses_account_profile_instead_of_notebook_author(self):
        page = await self.browser.new_page()
        cases = [
            ('<a href="/TEST-USER">Your Profile</a>', "logged_in"),
            ('<a href="https://www.kaggle.com/other-user">Your Profile</a>', "wrong_account"),
            ('<a href="/account/login">Login</a>', "logged_out"),
            ('<a href="/account/login">Sign In</a>', "logged_out"),
            ('<a href="https://evil.test/test-user">Your Profile</a>', "unknown"),
            ('<a href="/code/test-user/notebook">Your Profile</a>', "unknown"),
            ("", "unknown"),
        ]
        for account, expected in cases:
            with self.subTest(expected=expected, account=account):
                await self.serve('<nav aria-label="Main menu, search and your account">' + account + '</nav>'
                                 '<a href="/test-user">Go to profile for test-user</a>')
                await page.goto(NOTEBOOK_URL)
                self.assertEqual(await check_login(page, "test-user", timeout=0.1), expected)

    async def test_account_menu_is_opened_for_verification_and_dismissed(self):
        await self.serve('''<nav aria-label="Main menu, search and your account">
            <button aria-label="Your profile" onclick="document.querySelector('[role=menu]').hidden=false">Avatar</button>
            </nav><div role="menu" hidden><a role="menuitem" href="/test-user">Your Profile</a></div>
            <script>document.addEventListener('keydown', e => {
                if (e.key === 'Escape') document.querySelector('[role=menu]').hidden = true;
            });</script>''')
        page = await self.browser.new_page()
        await page.goto(NOTEBOOK_URL)
        self.assertEqual(await check_login(page, "test-user", timeout=2), "logged_in")
        self.assertFalse(await page.get_by_role("menu").is_visible())

    async def test_sign_in_redirect_returns_logged_out(self):
        await self.serve("<h1>Login</h1>")
        page = await self.browser.new_page()
        await page.goto("https://www.kaggle.com/account/login")
        self.assertEqual(await check_login(page, "test-user"), "logged_out")

    async def test_session_states_and_stale_indicators(self):
        page = await self.browser.new_page()
        for controls, expected in (
            ("<button>Draft Session: Running</button>", "connected"),
            ("<button>Session Active</button>", "connected"),
            ("<button>Stop Session</button>", "connected"),
            ("<button>Start Session</button>", "disconnected"),
            ("<button>Draft Session: Stopped</button>", "disconnected"),
            ("<button>Draft Session: Starting...</button><button>Session Running</button>", "connecting"),
            ("<button>Draft Session: Adding data</button>", "connecting"),
            ("<button>Start Session</button><button>Session Running</button>", "disconnected"),
            ("<button hidden>Session Running</button>", "unknown"),
            ("<button disabled>Stop Session</button>", "unknown"),
            ("<p>Session Running. CPU RAM Disk.</p><button>Run All</button>", "unknown"),
        ):
            with self.subTest(controls=controls):
                await page.set_content(controls)
                self.assertEqual(await check_runtime_connection(page, timeout=0.1), expected)

    async def test_waits_for_an_already_starting_session(self):
        page = await self.browser.new_page()
        await page.set_content('''<button>Draft Session: Starting...</button>
            <script>setTimeout(() => document.querySelector('button').textContent = 'Session Running', 50);</script>''')
        self.assertEqual(await check_runtime_connection(page, timeout=2), "connected")

    async def test_session_menu_is_read_without_selecting_an_action(self):
        page = await self.browser.new_page()
        for action, expected in (("Stop Session", "connected"), ("Start Session", "disconnected")):
            with self.subTest(action=action):
                await page.set_content('''<button aria-label="Session options"
                    onclick="document.querySelector('[role=menu]').hidden=false">Options</button>
                    <div role="menu" hidden><button role="menuitem"
                    onclick="document.querySelector('#action').textContent='clicked'">''' + action + '''</button></div>
                    <p id="action">untouched</p><script>document.addEventListener('keydown', e => {
                        if (e.key === 'Escape') document.querySelector('[role=menu]').hidden=true;
                    });</script>''')
                self.assertEqual(await check_runtime_connection(page, timeout=0.6), expected)
                self.assertEqual(await page.locator("#action").inner_text(), "untouched")
                self.assertFalse(await page.get_by_role("menu").is_visible())

    async def test_viewer_url_opens_existing_editor_and_preserves_query_and_fragment(self):
        await self.serve(EDITOR)
        page = await self.browser.new_page()
        result = await KaggleSite(page).open_notebook(NOTEBOOK_URL + "/?scriptVersionId=123#cell-1")
        self.assertEqual(result["notebook_url"], NOTEBOOK_URL + "/edit?scriptVersionId=123#cell-1")
        self.assertEqual(result["title"], "My Notebook | Kaggle")

    async def test_delayed_editor_is_awaited(self):
        await self.serve('''<title>Kaggle</title><body><script>setTimeout(() => {
            document.title = 'My Notebook | Kaggle';
            document.body.innerHTML = '<button>Save Version</button><div class="monaco-editor">Editor</div>';
        }, 50);</script></body>''')
        page = await self.browser.new_page()
        result = await KaggleSite(page).open_notebook(NOTEBOOK_URL + "/edit")
        self.assertEqual(result["title"], "My Notebook | Kaggle")
        self.assertEqual(result["notebook_url"], NOTEBOOK_URL + "/edit")

    async def test_published_viewer_cannot_be_mistaken_for_an_editor(self):
        await self.serve('<title>My Notebook | Kaggle</title><h1>My Notebook</h1><main>Published output</main>')
        page = await self.browser.new_page()
        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            await KaggleSite(page, timeout_ms=100).open_notebook(NOTEBOOK_URL)

    async def test_redirect_to_sign_in_requires_manual_attention(self):
        await self.serve('''<script>
            if (!location.pathname.startsWith('/account/')) location.replace('/account/login');
        </script><h1>Sign in</h1>''')
        page = await self.browser.new_page()
        with self.assertRaisesRegex(NeedsAttention, "sign-in"):
            await KaggleSite(page, timeout_ms=500).open_notebook(NOTEBOOK_URL)

    async def test_blocking_dialogs_require_attention(self):
        for dialog in ("You need access. Request access.", "Verify your account.", "Veuillez confirmer votre choix."):
            with self.subTest(dialog=dialog):
                await self.serve(EDITOR + f'<div role="dialog">{dialog}</div>')
                page = await self.browser.new_page()
                with self.assertRaises(NeedsAttention):
                    await KaggleSite(page).open_notebook(NOTEBOOK_URL)

    async def test_http_errors_distinguish_access_from_failure(self):
        for status, error in ((401, NeedsAttention), (403, NeedsAttention), (404, RuntimeError), (503, RuntimeError)):
            with self.subTest(status=status):
                await self.serve("<h1>Unavailable</h1>", status=status)
                page = await self.browser.new_page()
                with self.assertRaisesRegex(error, f"HTTP {status}"):
                    await KaggleSite(page).open_notebook(NOTEBOOK_URL)

    async def task_context(self, html):
        manifest = self.root / "profile.toml"
        manifest.write_text('[profile.platforms.kaggle]\nlogin_username="test-user"\n'
                            f'notebook_url="{NOTEBOOK_URL}"\nnotebook_name="My Notebook"\n')
        await self.serve(html)
        profile = Profile("kaggle-test", self.root / "browser", manifest_path=manifest)
        return TaskContext(self.browser, profile, {}, {}, self.root, "test", "open")

    async def test_task_succeeds_and_closes_only_its_notebook_tab(self):
        ctx = await self.task_context(EDITOR)
        before = len(self.browser.pages)
        result = await run(ctx)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.outputs, {})
        self.assertEqual(len(self.browser.pages), before)

    async def test_disconnected_task_leaves_page_for_failure_evidence(self):
        ctx = await self.task_context(EDITOR.replace("Draft Session: Running", "Start Session"))

        async def quick_check(page):
            return await check_runtime_connection(page, timeout=0.1)

        with patch("zepuppeteer.tasks.kaggle.open_notebook.check_runtime_connection", side_effect=quick_check):
            with self.assertRaisesRegex(NeedsAttention, "runtime is disconnected"):
                await run(ctx)
        self.assertFalse(self.browser.pages[-1].is_closed())
