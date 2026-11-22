"""Verify visible Brave startup with disposable saved tabs and cookies."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from zepuppeteer.brave import find_brave, launch_options, prepare_profile_for_launch
from zepuppeteer.models import Profile


BRAVE = find_brave()


@unittest.skipUnless(BRAVE and importlib.util.find_spec("playwright")
                     and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
                     "Requires Brave, Playwright, and a desktop display")
class BlankStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_tabs_are_not_restored_and_cookie_survives(self):
        from playwright.async_api import async_playwright

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-blank-startup-") as temporary:
            root = Path(temporary)
            pages = []
            for name in ("old-one", "old-two"):
                path = root / f"{name}.html"
                path.write_text(f"<title>{name}</title><h1>Previous tab</h1>")
                pages.append(path.as_uri())
            profile = Profile("startup-test", root / "browser")
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(**launch_options(BRAVE, profile))
                try:
                    for url in pages:
                        await (await context.new_page()).goto(url)
                    await context.add_cookies([{"name": "saved-session", "value": "kept",
                                               "url": "https://startup.example.test", "expires": time.time() + 3600}])
                finally:
                    await context.close()

                preferences = profile.user_data_dir / "Default/Preferences"
                data = json.loads(preferences.read_bytes())
                data.setdefault("session", {})["restore_on_startup"] = 1
                data["pinned_tabs"] = [{"url": pages[0]}]
                preferences.write_text(json.dumps(data))

                # Confirm this is a real saved session before suppressing it.
                context = await playwright.chromium.launch_persistent_context(**launch_options(BRAVE, profile))
                try:
                    await asyncio.sleep(0.5)
                    self.assertTrue(any(page.url in pages for page in context.pages))
                finally:
                    await context.close()

                prepare_profile_for_launch(profile)
                context = await playwright.chromium.launch_persistent_context(**launch_options(BRAVE, profile))
                try:
                    await asyncio.sleep(0.5)
                    self.assertEqual([page.url for page in context.pages], ["about:blank"])
                    cookies = await context.cookies("https://startup.example.test")
                    self.assertTrue(any(cookie["name"] == "saved-session" and cookie["value"] == "kept"
                                        for cookie in cookies))
                finally:
                    await context.close()
