"""Run-all readiness tests simulate cell execution locally; no notebook code runs."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from zepuppeteer.brave import find_brave, launch_options
from zepuppeteer.models import NeedsAttention, Profile
from zepuppeteer.websites.colab import ColabSite


BRAVE = find_brave()


@unittest.skipUnless(BRAVE and importlib.util.find_spec("playwright"), "Requires Brave and Playwright")
class ColabExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-run-all-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch_persistent_context(
            **launch_options(BRAVE, Profile("execution-test", self.root / "browser"), True))
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()

    async def fixture(self, script="", *, active="", code_cells=True):
        cells = '''<div class="cell code" id="setup"><colab-run-button></colab-run-button></div>
            <div class="cell code" id="main-job"><colab-run-button></colab-run-button>
                <div class="output">Saved output from a previous run</div></div>
            <div class="cell text" id="trailing-markdown">Notes</div>''' if code_cells else ""
        html = f'''<title>Test - Colab</title>{cells}
            <div id="dialogs"></div>
            <button onclick="window.clicks++; {script}">Run all Run all cells in notebook</button>
            <script>
                window.clicks = 0;
                for (const host of document.querySelectorAll('colab-run-button')) {{
                    host.attachShadow({{mode: 'open'}}).innerHTML = '<div class="cell-execution {active}">[ ]</div>';
                }}
                window.indicator = id => document.getElementById(id).querySelector('colab-run-button')
                    .shadowRoot.querySelector('.cell-execution');
            </script>'''

        async def respond(route):
            await route.fulfill(content_type="text/html", body=html)

        await self.page.route("https://colab.research.google.com/**", respond)
        await self.page.goto("https://colab.research.google.com/drive/local-test")

    async def test_waits_for_last_code_cell_running_not_earlier_cell_or_queue(self):
        await self.fixture('''indicator('setup').classList.add('running');
            indicator('main-job').classList.add('pending');
            setTimeout(() => {
                indicator('setup').classList.remove('running');
                indicator('main-job').classList.replace('pending', 'running');
            }, 100);''')
        cell_id = await ColabSite(self.page).run_all_and_wait_for_last_cell(timeout_seconds=2)
        self.assertEqual(cell_id, "main-job")
        self.assertEqual(await self.page.evaluate("window.clicks"), 1)
        self.assertEqual(await self.page.locator("#main-job colab-run-button .running").count(), 1)

    async def test_queued_last_cell_is_not_reported_as_started(self):
        await self.fixture("indicator('setup').classList.add('running'); indicator('main-job').classList.add('pending');")
        with self.assertRaisesRegex(NeedsAttention, "did not start executing"):
            await ColabSite(self.page).run_all_and_wait_for_last_cell(timeout_seconds=0.1)
        self.assertEqual(await self.page.evaluate("window.clicks"), 1)
        self.assertFalse(self.page.is_closed())

    async def test_saved_output_is_not_evidence_of_new_execution(self):
        await self.fixture()
        with self.assertRaisesRegex(NeedsAttention, "did not start executing"):
            await ColabSite(self.page).run_all_and_wait_for_last_cell(timeout_seconds=0.1)

    async def test_running_or_pending_work_is_not_started_again(self):
        for state in ("running", "pending"):
            with self.subTest(state=state):
                await self.fixture(active=state)
                with self.assertRaisesRegex(NeedsAttention, "already has running or queued"):
                    await ColabSite(self.page).run_all_and_wait_for_last_cell(timeout_seconds=1)
                self.assertEqual(await self.page.evaluate("window.clicks"), 0)

    async def test_blocking_dialog_requires_attention(self):
        await self.fixture("document.getElementById('dialogs').innerHTML = '<div role=dialog>Input required</div>';")
        with self.assertRaisesRegex(NeedsAttention, "showing a dialog"):
            await ColabSite(self.page).run_all_and_wait_for_last_cell(timeout_seconds=2)

    async def test_no_code_cells_requires_attention(self):
        await self.fixture(code_cells=False)
        with self.assertRaisesRegex(NeedsAttention, "no rendered code cells"):
            await ColabSite(self.page).run_all_and_wait_for_last_cell(timeout_seconds=1)
        self.assertEqual(await self.page.evaluate("window.clicks"), 0)

    async def test_invalid_job_start_timeout_is_rejected(self):
        await self.fixture()
        for timeout in (0, -1, True, "300", float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await ColabSite(self.page).run_all_and_wait_for_last_cell(timeout_seconds=timeout)
        self.assertEqual(await self.page.evaluate("window.clicks"), 0)
