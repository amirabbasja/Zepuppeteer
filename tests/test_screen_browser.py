"""Opt-in integration tests operate a disposable visible browser using pixels."""
import os
from pathlib import Path
import tempfile
import unittest

from zepuppeteer.brave import find_brave
from zepuppeteer.models import Profile, Step, Workflow
from zepuppeteer.runner import run_workflow
from zepuppeteer.screen import launch_browser
from zepuppeteer.state import StateStore, read_history
from zepuppeteer.websites.demo import DemoSite
from zepuppeteer.workflows import load_workflow, resolve_tasks


@unittest.skipUnless(os.environ.get("ZE_SCREEN_TESTS") == "1", "Set ZE_SCREEN_TESTS=1 on a disposable X11 desktop")
class ScreenBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_workflow_two_profiles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profiles = [Profile(name, root / name) for name in ("one", "two")]
            workflow = load_workflow("config/workflows/demo.toml")
            with StateStore(root / "runs.sqlite") as state:
                summary = await run_workflow(profiles, find_brave(), workflow, resolve_tasks(workflow), state, root / "artifacts")
            if summary.failed and os.environ.get("ZE_SCREEN_FAILURES"):
                import shutil
                shutil.copytree(summary.artifact_dir, Path(os.environ["ZE_SCREEN_FAILURES"]) / summary.run_id)
            self.assertEqual((summary.status, summary.succeeded), ("success", 2))

    async def test_failed_task_saves_screenshot_archive(self):
        import zipfile
        async def fail(ctx):
            page = await ctx.browser.new_page()
            raise RuntimeError("local failure")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / "runs.sqlite") as state:
                summary = await run_workflow([Profile("one", root / "browser")], find_brave(),
                    Workflow("failure", (Step("fail", "fail"),)), {"fail": fail}, state, root / "artifacts")
            details = read_history(root / "runs.sqlite", run_id=summary.run_id)
            directory = Path(details["steps"][0]["artifact_dir"])
            self.assertEqual(summary.failed, 1)
            self.assertTrue((directory / "page-1.png").is_file())
            with zipfile.ZipFile(directory / "trace.zip") as archive:
                self.assertIn("start.png", archive.namelist())
                self.assertIn("end.png", archive.namelist())

    async def test_saved_local_storage_survives_normal_relaunch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = root / "session.html"
            fixture.write_text('''<title>Session fixture</title><style>body{font:30px sans-serif;padding:50px}button{font:30px sans-serif}</style>
                <button onclick="localStorage.setItem('session','kept');location.reload()">Save session</button>
                <p id="result"></p><script>if(localStorage.getItem('session'))document.querySelector('#result').textContent='Session retained';</script>''')
            profile = Profile("one", root / "browser")
            for first in (True, False):
                browser = await launch_browser(find_brave(), profile)
                try:
                    page = await browser.new_page()
                    await page.goto(fixture.as_uri())
                    if first:
                        await page.click_text("Save session")
                    await page.wait_for_text("Session retained")
                finally:
                    await browser.close()
