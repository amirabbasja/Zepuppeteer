"""Runtime quota parsing uses local resource panels, without contacting Colab."""

import importlib.util
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
import tempfile
import unittest

from zepuppeteer.brave import find_brave, launch_options
from zepuppeteer.models import NeedsAttention, Profile
from zepuppeteer.tasks.colab.open_notebook import getRemainingQuota


BRAVE = find_brave()
SAMPLE = (
    "You are not subscribed. Learn more\n"
    "You currently have zero compute units available. Resources offered free of charge "
    "are not guaranteed. Purchase more units here.\n"
    "At your current usage level, this runtime may last up to 1 hour 40 minutes."
)


@unittest.skipUnless(BRAVE and importlib.util.find_spec("playwright"), "Requires Brave and Playwright")
class ColabQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-quota-")
        self.addCleanup(temporary.cleanup)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch_persistent_context(
            **launch_options(BRAVE, Profile("quota-test", Path(temporary.name) / "browser"), True))
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()

    async def fixture(self, text, *, delay_ms=0):
        await self.page.set_content('''
            <button aria-label="Additional connection options" onclick="
                window.actions.push('options');
                document.querySelector('#menu').hidden=false;">Options</button>
            <div id="menu" hidden>
              <button role="menuitem" onclick="
                  window.actions.push('resources');
                  document.querySelector('#menu').hidden=true;
                  document.querySelector('#panel').hidden=false;
                  setTimeout(() => {
                    document.querySelector('#contents').hidden=false;
                  }, window.contentDelay);">View resources</button>
            </div>
            <colab-resource-display id="panel" role="complementary" hidden>
              <h2>Resources</h2><div id="contents" hidden></div>
            </colab-resource-display>
            <aside hidden>You currently have 999 compute units available.
              At your current usage level, this runtime may last up to 99 hours.</aside>
            <script>window.actions=[];</script>
        ''')
        paragraphs = "".join(f"<p>{escape(line)}</p>" for line in text.splitlines())
        await self.page.locator('#contents').evaluate("(node, html) => node.innerHTML=html", paragraphs)
        await self.page.evaluate("delay => window.contentDelay=delay", delay_ms)

    def assert_estimate(self, result, seconds, before, after):
        self.assertEqual(set(result), {
            "name", "remaining_quota", "time_remaining", "end_timestamp", "end_time",
        })
        self.assertEqual(result["name"], "google_colab")
        self.assertEqual(result["time_remaining"], seconds)
        self.assertIs(type(result["end_timestamp"]), int)
        self.assertGreaterEqual(result["end_timestamp"], int(before.timestamp()) + seconds)
        self.assertLessEqual(result["end_timestamp"], int(after.timestamp()) + seconds)
        end_time = datetime.fromisoformat(result["end_time"])
        self.assertEqual(end_time.utcoffset(), timedelta(hours=3, minutes=30))
        self.assertEqual(int(end_time.timestamp()), result["end_timestamp"])

    async def test_sample_opens_resources_and_parses_zero_units_and_estimate(self):
        await self.fixture(SAMPLE)
        before = datetime.now(timezone.utc)
        result = await getRemainingQuota(self.page)
        after = datetime.now(timezone.utc)
        self.assertEqual(result["remaining_quota"], 0.0)
        self.assert_estimate(result, 6000, before, after)
        self.assertEqual(await self.page.evaluate("window.actions"), ["options", "resources"])
        self.assertTrue(await self.page.locator('#panel').is_visible())

    async def test_decimal_units_and_duration_units(self):
        for duration, seconds in (
            ("1 day 2 hours 3 minutes 4 seconds", 93784),
            ("45 minutes", 2700),
            ("1.5 hours", 5400),
        ):
            with self.subTest(duration=duration):
                await self.fixture(
                    "You currently have 1,234.5 compute units available.\n"
                    f"At your current usage level, this runtime may last up to {duration}.")
                before = datetime.now(timezone.utc)
                result = await getRemainingQuota(self.page)
                after = datetime.now(timezone.utc)
                self.assertEqual(result["remaining_quota"], 1234.5)
                self.assert_estimate(result, seconds, before, after)

    async def test_waits_for_delayed_resource_text(self):
        await self.fixture(SAMPLE, delay_ms=150)
        result = await getRemainingQuota(self.page, timeout=2)
        self.assertEqual(result["remaining_quota"], 0.0)
        self.assertEqual(result["time_remaining"], 6000)

    async def test_split_shadow_text_ignores_notebook_content(self):
        await self.fixture("")
        await self.page.locator('#panel').evaluate('''panel => {
            const cell = document.createElement('div');
            cell.className = 'cell code';
            const output = document.createElement('colab-output');
            cell.append(output);
            output.attachShadow({mode: 'open'}).innerHTML =
                '<p>You currently have 999 compute units available.</p>'
                + '<p>At your current usage level, this runtime may last up to 99 hours.</p>';
            panel.before(cell);
            panel.attachShadow({mode: 'open'}).innerHTML =
                '<h2>Resources</h2>'
                + '<p><span>You currently have </span><b>12.5</b>'
                + '<span> compute units available.</span></p>'
                + '<p><span>At your current usage level, this runtime may last up to 2 hours</span>'
                + '<span> and 5 minutes.</span></p>';
        }''')
        before = datetime.now(timezone.utc)
        result = await getRemainingQuota(self.page)
        after = datetime.now(timezone.utc)
        self.assertEqual(result["remaining_quota"], 12.5)
        self.assert_estimate(result, 7500, before, after)

    async def test_missing_fields_remain_unknown_independently(self):
        await self.fixture("You currently have 0 compute units available.")
        result = await getRemainingQuota(self.page, timeout=0.4)
        self.assertEqual(result, {
            "name": "google_colab", "remaining_quota": 0.0,
            "time_remaining": None, "end_timestamp": None, "end_time": None,
        })

        await self.fixture("At your current usage level, this runtime may last up to 1 minute.")
        before = datetime.now(timezone.utc)
        result = await getRemainingQuota(self.page, timeout=0.4)
        after = datetime.now(timezone.utc)
        self.assertIsNone(result["remaining_quota"])
        self.assert_estimate(result, 60, before, after)

    async def test_unrecognized_resources_raise_instead_of_inventing_values(self):
        await self.fixture("Resources are loading. CPU RAM Disk")
        with self.assertRaises(NeedsAttention):
            await getRemainingQuota(self.page, timeout=0.3)
