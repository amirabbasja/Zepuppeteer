"""Colab DOM fixtures are intercepted locally; these tests never contact Google."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from zepuppeteer.brave import Profile, find_brave, launch_options
from zepuppeteer.models import NeedsAttention, TaskContext
from zepuppeteer.tasks.colab.open_notebook import check_login, run
from zepuppeteer.websites.colab import ColabSite, DEFAULT_NOTEBOOK_URL


BRAVE = find_brave()
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None

EDITOR = """<!doctype html><html><head><title>Example notebook - Colab</title></head>
<body><div role="menubar"><button>File</button></div>
<main aria-label="Notebook">Example notebook</main>
<a href="https://accounts.google.com/ServiceLogin">Sign in</a>{dialog}</body></html>"""


@unittest.skipUnless(BRAVE and HAS_PLAYWRIGHT, "Requires installed Brave and Playwright")
class ColabBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-colab-browser-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile("colab-test", self.root / "browser")
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch_persistent_context(
            **launch_options(BRAVE, self.profile, True))
        self.addAsyncCleanup(self.browser.close)

    async def serve(self, html, status=200):
        async def respond(route):
            await route.fulfill(status=status, content_type="text/html; charset=utf-8", body=html)
        await self.browser.route("**/*", respond)

    async def test_login_states_from_real_page_controls(self):
        page = await self.browser.new_page()
        cases = [
            ('<a role="button" aria-label="Google Account: Test (account@example.test)">Account</a>', "logged_in"),
            ('<button aria-label="Google Account: Other (other@example.test)">Account</button>', "wrong_account"),
            ('<a href="https://accounts.google.com/">Sign in</a>', "logged_out"),
            ('<button>Sign in</button>', "logged_out"),
            ('<main>Loading</main>', "unknown"),
        ]
        for html, expected in cases:
            with self.subTest(expected=expected):
                await page.set_content(html)
                self.assertEqual(await check_login(page, "account@example.test", timeout=0.1), expected)
        await self.serve("<main>Google sign-in</main>")
        await page.goto("https://accounts.google.com/ServiceLogin")
        self.assertEqual(await check_login(page, "account@example.test"), "logged_out")

    async def test_google_login_pages_are_logged_out_in_any_language(self):
        page = await self.browser.new_page()
        cases = [
            ("en", "Sign in", "Use your Google Account", "Forgot email?",
             "Not your computer? Use Guest mode to sign in privately. Learn more about using Guest mode"),
            ("fa", "ورود", "از حساب Google خود استفاده کنید", "ایمیل را فراموش کرده‌اید؟", ""),
            ("es", "Iniciar sesión", "Usa tu cuenta de Google", "¿Has olvidado tu correo electrónico?", ""),
            ("ja", "ログイン", "Google アカウントを使用", "メールアドレスを忘れた場合", ""),
        ]
        for locale, title, subtitle, forgot_email, guest_mode in cases:
            with self.subTest(locale=locale):
                await self.serve(f'''<!doctype html><html lang="{locale}"><main>
                    <h1>{title}</h1><p>{subtitle}</p>
                    <input id="identifierId" name="identifier" type="email">
                    <button>{forgot_email}</button><p>{guest_mode}</p>
                    </main></html>''')
                await page.goto(f"https://accounts.google.com/v3/signin/identifier?hl={locale}")
                self.assertEqual(await check_login(page, "account@example.test", timeout=0.1), "logged_out")

    async def test_public_signed_out_notebook_saves_real_artifacts(self):
        await self.serve(EDITOR.format(dialog=""))
        directory = self.root / "artifacts"
        directory.mkdir()
        ctx = TaskContext(self.browser, self.profile, {}, {}, directory, "run", "open")
        before = len(self.browser.pages)
        result = await run(ctx)
        self.assertEqual(result.outputs["title"], "Example notebook - Colab")
        self.assertEqual(result.outputs["notebook_url"], DEFAULT_NOTEBOOK_URL)
        self.assertEqual(json.loads(Path(result.outputs["metadata_path"]).read_text())["profile"], "colab-test")
        self.assertTrue(Path(result.outputs["screenshot_path"]).read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(len(self.browser.pages), before)

    async def test_delayed_localized_editor_is_awaited(self):
        await self.serve("""<!doctype html><title>Google Colab</title><body>
            <script>setTimeout(() => {
                document.title = 'دفتر - Colab';
                document.body.innerHTML = '<div role="menubar"><button>ملف</button></div><main aria-label="دفتر">محتوى</main>';
            }, 100);</script></body>""")
        page = await self.browser.new_page()
        details = await ColabSite(page).open_notebook(DEFAULT_NOTEBOOK_URL)
        self.assertEqual(details["title"], "دفتر - Colab")

    async def test_google_sign_in_redirect_requires_manual_attention(self):
        async def respond(route):
            if route.request.url.startswith("https://colab.research.google.com/"):
                # A fresh client-side navigation lets the next request be routed
                # locally too; HTTP redirect chains can bypass subsequent handlers.
                await route.fulfill(content_type="text/html", body="""<!doctype html>
                    <script>location.replace('https://accounts.google.com/ServiceLogin');</script>""")
            else:
                await route.fulfill(content_type="text/html", body="<h1>Sign in</h1>")
        await self.browser.route("**/*", respond)
        page = await self.browser.new_page()
        with self.assertRaisesRegex(NeedsAttention, "sign-in"):
            await ColabSite(page, timeout_ms=3000).open_notebook(DEFAULT_NOTEBOOK_URL)

    async def test_permission_dialog_cannot_be_mistaken_for_a_loaded_notebook(self):
        await self.serve(EDITOR.format(dialog='<div role="dialog">You need access. Request access.</div>'))
        page = await self.browser.new_page()
        with self.assertRaisesRegex(NeedsAttention, "permission"):
            await ColabSite(page).open_notebook(DEFAULT_NOTEBOOK_URL)

    async def test_unrecognized_localized_dialog_requires_attention(self):
        await self.serve(EDITOR.format(dialog='<div role="dialog">Veuillez confirmer votre choix.</div>'))
        page = await self.browser.new_page()
        with self.assertRaisesRegex(NeedsAttention, "dialog"):
            await ColabSite(page).open_notebook(DEFAULT_NOTEBOOK_URL)

    async def test_http_error_is_a_failure(self):
        await self.serve("<h1>Temporarily unavailable</h1>", status=503)
        page = await self.browser.new_page()
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            await ColabSite(page).open_notebook(DEFAULT_NOTEBOOK_URL)

    async def test_an_editor_shell_alone_is_not_success(self):
        await self.serve("<title>Google Colab</title><div role='menubar'>File</div><main>Loading...</main>")
        page = await self.browser.new_page()
        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            await ColabSite(page, timeout_ms=100).open_notebook(DEFAULT_NOTEBOOK_URL)
