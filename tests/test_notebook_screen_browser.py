"""Opt-in tests run the actual site tasks against local, visible UI fixtures.

Only URL routing is adapted to local files. All navigation, account-menu clicks,
OCR, runtime checks, notebook actions and evidence capture use the real screen.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from zepuppeteer.brave import find_brave, native_command
from zepuppeteer.models import Profile, TaskContext
from zepuppeteer.screen import launch_browser
from zepuppeteer.tasks.colab.open_notebook import run as run_colab
from zepuppeteer.tasks.kaggle.open_notebook import run as run_kaggle

FIXTURES = Path(__file__).parent / "fixtures" / "notebooks"


class LocalPage:
    """Map service URLs to local files; all interaction still uses the screen."""
    def __init__(self, page):
        self.page = page
        self.requested_url = None

    def __getattr__(self, name):
        return getattr(self.page, name)

    async def goto(self, url):
        self.requested_url = url
        service = "colab" if urlsplit(url).hostname == "colab.research.google.com" else "kaggle"
        await self.page.goto((FIXTURES / f"{service}.html").resolve().as_uri())

    async def current_url(self):
        actual = urlsplit(await self.page.current_url())
        filename = Path(actual.path).name
        if filename in {"colab.html", "kaggle.html"}:
            self.page.url = self.requested_url
        elif filename == "kaggle-profile.html":
            self.page.url = "https://www.kaggle.com/tester"
        else:
            raise RuntimeError(f"Browser did not reach a local notebook fixture: {actual.geturl()}")
        return self.page.url


@unittest.skipUnless(os.environ.get("ZE_SCREEN_TESTS") == "1", "Set ZE_SCREEN_TESTS=1 on a disposable X11 desktop")
class NotebookBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 5

    async def execute(self, platform, data, targets, task):
        with tempfile.TemporaryDirectory(prefix="zepuppeteer-notebook-screen-") as folder:
            root = Path(folder)
            manifest = root / "profile.toml"
            manifest.write_text(f"[profile.platforms.{platform}]\n" + "\n".join(f"{key} = {json.dumps(value)}" for key, value in data.items()))
            profile = Profile("fixture", root / "browser", settings={"screen": {"targets": targets}}, manifest_path=manifest)
            # Disposable fixtures have no credentials. Avoid the real desktop
            # keyring and first-run setup on the independent X11 test display.
            def fixture_command(*args, **kwargs):
                command = native_command(*args, **kwargs)
                return command[:-1] + ["--password-store=basic", "--no-first-run"] + command[-1:]
            with patch("zepuppeteer.brave.native_command", side_effect=fixture_command):
                browser = await launch_browser(find_brave(), profile)
            original_new_page = browser.new_page
            async def new_page():
                return LocalPage(await original_new_page())
            browser.new_page = new_page
            ctx = TaskContext(browser, profile, {}, {}, root / "artifacts", "fixture-run", f"{platform}.open_notebook")
            try:
                result = await asyncio.wait_for(task(ctx), 150)
                self.assertEqual(result.status, "success")
                self.assertEqual(result.outputs["runtime_status"], "connected")
                self.assertTrue(result.outputs["account_verified"])
                self.assertTrue(Path(result.outputs["screenshot_path"]).read_bytes().startswith(b"\x89PNG"))
                self.assertEqual(json.loads(Path(result.outputs["metadata_path"]).read_text()), result.outputs)
                self.assertFalse(browser.pages)
                return result
            except BaseException as error:
                print(f"{platform} fixture failure: {type(error).__name__}: {error}", flush=True)
                output = os.environ.get("ZE_SCREEN_FAILURES")
                if output:
                    dest = Path(output) / f"{platform}-{root.name}"
                    dest.mkdir(parents=True, exist_ok=True)
                    for index, page in enumerate(browser.pages):
                        await page.screenshot(path=str(dest / f"page-{index}.png"))
                    if ctx.artifact_dir.exists():
                        shutil.copytree(ctx.artifact_dir, dest / "artifacts")
                raise
            finally:
                await browser.close()

    async def test_colab_account_connection_run_all_and_final_cell(self):
        targets = {
            "google_account": {"text": "Account", "region": [0.65, 0.08, 0.35, 0.16]},
            "google_active_account": {"region": [0.5, 0.2, 0.5, 0.27]},
            "colab_runtime": {"region": [0.45, 0.17, 0.35, 0.13]},
            "colab_connection_options": {"text": "Options", "region": [0.75, 0.15, 0.25, 0.18]},
            "colab_connect_hosted": {"text": "Connect to a hosted runtime", "region": [0.4, 0.18, 0.6, 0.28]},
            "colab_notebook_idle": {"text": "Ready to run", "region": [0, 0.17, 0.6, 0.14]},
            "colab_last_cell_anchor": {"text": "Main job cell", "region": [0, 0.4, 1, 0.6]},
            "colab_last_cell_running": {"text": "Executing main job", "region": [0, 0.4, 1, 0.6]},
        }
        result = await self.execute("colab", {"login_username": "tester@example.com", "notebook_url": "https://colab.research.google.com/drive/fixture",
            "notebook_name": "Screen Notebook", "auto_connect": True, "job_start_timeout_seconds": 20}, targets, run_colab)
        self.assertTrue(result.outputs["job_started"])
        self.assertEqual(result.outputs["action"], "run_all")

    async def test_kaggle_account_profile_and_session_menu(self):
        targets = {
            "kaggle_account": {"text": "Account", "region": [0.65, 0.08, 0.35, 0.16]},
            "kaggle_profile": {"text": "Your Profile", "region": [0.5, 0.18, 0.5, 0.35]},
            "kaggle_runtime": {"region": [0.5, 0.15, 0.5, 0.17]},
            "kaggle_session_options": {"text": "Session options", "region": [0.5, 0.15, 0.5, 0.17]},
            "kaggle_session_status": {"region": [0.5, 0.2, 0.5, 0.3]},
        }
        await self.execute("kaggle", {"login_username": "tester", "notebook_url": "https://www.kaggle.com/code/tester/fixture/edit",
            "notebook_name": "Screen Notebook"}, targets, run_kaggle)
