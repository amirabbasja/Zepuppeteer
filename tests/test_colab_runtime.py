"""Runtime connection flows use local fixtures instead of allocating real runtimes."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zepuppeteer.brave import find_brave, launch_options
from zepuppeteer.models import NeedsAttention, Profile, TaskContext
from zepuppeteer.tasks.colab.open_notebook import check_runtime_connection, run


BRAVE = find_brave()
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


@unittest.skipUnless(BRAVE and HAS_PLAYWRIGHT, "Requires installed Brave and Playwright")
class ColabRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-runtime-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch_persistent_context(
            **launch_options(BRAVE, Profile("runtime-test", self.root / "browser"), True))
        self.addAsyncCleanup(self.browser.close)

    async def test_runtime_states_in_shadow_toolbar(self):
        page = await self.browser.new_page()
        cases = [
            ('<button aria-label="Connected to Python 3 backend">Connected</button>', "connected"),
            ('<button><span>RAM</span><span>Disk</span></button>', "connected"),
            ('<button>Connect</button>', "disconnected"),
            ('<button>Reconnect</button>', "disconnected"),
            ('<button>Connecting...</button><span>RAM</span><span>Disk</span>', "connecting"),
            ('<button>Restarting...</button>', "restarting"),
            ('<button>Restarting...</button><span>RAM</span><span>Disk</span>', "restarting"),
            ('<button>Connect</button><span>RAM</span><span>Disk</span>', "disconnected"),
            ('<button>Help</button>', "unknown"),
            ('<span hidden>RAM</span><span hidden>Disk</span>', "unknown"),
        ]
        for html, expected in cases:
            with self.subTest(expected=expected, html=html):
                await page.set_content('<p>RAM Disk Connected</p><colab-connect-button></colab-connect-button>')
                await page.locator("colab-connect-button").evaluate(
                    "(host, html) => { host.attachShadow({mode: 'open'}).innerHTML = html; }", html)
                status = await check_runtime_connection(page, timeout=0.1)
                if expected == "unknown":
                    self.assertTrue(status.startswith("unknown: "), status)
                    diagnostics = json.loads(status.removeprefix("unknown: "))
                    self.assertEqual(diagnostics["url"], page.url)
                    self.assertEqual(diagnostics["toolbar_count"], 1)
                    self.assertEqual(diagnostics["toolbar_visible"], "hidden" not in html)
                    if "Help" in html:
                        self.assertIn("Help", diagnostics["toolbar_snapshot"])
                else:
                    self.assertEqual(status, expected)
        await page.set_content("<p>RAM Disk Connected</p>")
        status = await check_runtime_connection(page, timeout=0.1)
        self.assertTrue(status.startswith("unknown: "), status)
        diagnostics = json.loads(status.removeprefix("unknown: "))
        self.assertEqual(diagnostics["toolbar_count"], 0)
        self.assertFalse(diagnostics["toolbar_visible"])

    async def test_waits_for_runtime_that_is_already_connecting(self):
        page = await self.browser.new_page()
        await page.set_content('<colab-connect-button><button>Connecting...</button></colab-connect-button>')
        await page.evaluate('''() => setTimeout(() => {
            document.querySelector('colab-connect-button').innerHTML =
                '<button aria-label="Connected to Python 3 backend">Connected</button>';
        }, 50)''')
        self.assertEqual(await check_runtime_connection(page, timeout=2), "connected")

    async def task_fixture(self, toolbar):
        manifest = self.root / "profile.toml"
        manifest.write_text('[profile.platforms.colab]\nlogin_username="account@example.test"\n'
                            'notebook_url="https://colab.research.google.com/drive/test"\n'
                            'notebook_name="test.ipynb"\nauto_connect=true\n')
        html = ('<title>test.ipynb - Colab</title><div role="menubar">File</div>'
                '<main>Notebook</main><button aria-label="Google Account: Test (account@example.test)">Account</button>'
                f'<colab-connect-button>{toolbar}</colab-connect-button>'
                '<div class="cell code" id="main-job"><colab-run-button>'
                '<div class="cell-execution">Run cell</div></colab-run-button></div>'
                '<button onclick="document.querySelector(\'.cell-execution\').classList.add(\'running\')">'
                'Run all</button>')

        async def respond(route):
            await route.fulfill(content_type="text/html", body=html)

        await self.browser.route("**/*", respond)
        profile = Profile("runtime-test", self.root / "browser", manifest_path=manifest)
        return TaskContext(self.browser, profile, {}, {}, self.root, "test", "open")

    async def test_connected_task_succeeds(self):
        ctx = await self.task_fixture('<button><span>RAM</span><span>Disk</span></button>')
        result = await run(ctx)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.outputs, {"job_started": True, "last_cell_id": "main-job"})

    def connection_controls(self, *, connects=True):
        finish = """setTimeout(() => {
            document.getElementById('connect-main').hidden = true;
            document.getElementById('resources').hidden = false;
        }, 50);""" if connects else ""
        return f'''
            <button id="connect-main">Connect</button>
            <button aria-label="Additional connection options" onclick="
                recordConnectionAction('options');
                document.getElementById('hosted-menu').hidden = false;">Options</button>
            <div role="menu" id="hosted-menu" hidden>
                <button role="menuitem" onclick="
                    recordConnectionAction('hosted');
                    document.getElementById('hosted-menu').hidden = true;
                    document.getElementById('connect-main').textContent = 'Connecting...';
                    {finish}">Connect to a hosted runtime: T4</button>
            </div>
            <span id="resources" hidden><span>RAM</span><span>Disk</span></span>
        '''

    async def test_disconnected_task_connects_through_hosted_runtime_menu(self):
        actions = []
        await self.browser.expose_binding("recordConnectionAction", lambda source, action: actions.append(action))
        ctx = await self.task_fixture(self.connection_controls())

        async def quick_check(page, timeout=15):
            return await check_runtime_connection(page, timeout=0.1 if timeout == 15 else 2)

        with patch("zepuppeteer.tasks.colab.open_notebook.check_runtime_connection", side_effect=quick_check):
            result = await run(ctx)
        self.assertEqual(result.status, "success")
        self.assertEqual(actions, ["options", "hosted"])

    async def test_connection_timeout_keeps_page_for_failure_evidence(self):
        actions = []
        await self.browser.expose_binding("recordConnectionAction", lambda source, action: actions.append(action))
        ctx = await self.task_fixture(self.connection_controls(connects=False))

        async def quick_check(page, timeout=15):
            return await check_runtime_connection(page, timeout=0.1)

        with patch("zepuppeteer.tasks.colab.open_notebook.check_runtime_connection", side_effect=quick_check):
            with self.assertRaisesRegex(NeedsAttention, "runtime is connecting"):
                await run(ctx)
        self.assertEqual(actions, ["options", "hosted"])
        page = self.browser.pages[-1]
        self.assertFalse(page.is_closed())
        self.assertTrue(await page.get_by_role("button", name="Connecting...", exact=True).is_visible())

    async def test_already_connecting_task_waits_without_requesting_another_runtime(self):
        ctx = await self.task_fixture('<button>Connecting...</button>')
        checks = 0

        async def progressing_check(page, timeout=15):
            nonlocal checks
            checks += 1
            if checks == 1:
                return "connecting"
            await page.locator("colab-connect-button").evaluate(
                "host => { host.innerHTML = '<span>RAM</span><span>Disk</span>'; }")
            return await check_runtime_connection(page, timeout=1)

        with patch("zepuppeteer.tasks.colab.open_notebook.check_runtime_connection", side_effect=progressing_check):
            result = await run(ctx)
        self.assertEqual(result.status, "success")
        self.assertEqual(checks, 2)
