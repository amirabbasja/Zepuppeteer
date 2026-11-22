"""Real Brave regression coverage; skipped when Brave or Playwright is absent."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from zepuppeteer.brave import Profile, find_brave
from zepuppeteer.cli import run_profiles
from zepuppeteer.models import Step, Workflow
from zepuppeteer.profiles import discover_profiles
from zepuppeteer.runner import run_workflow
from zepuppeteer.state import StateStore, read_history
from zepuppeteer.workflows import load_workflow, resolve_tasks


BRAVE = find_brave()
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


@unittest.skipUnless(BRAVE and HAS_PLAYWRIGHT, "Requires installed Brave and Playwright")
class WebdriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_colab_workflow_launches_the_profile_selected_by_display_name(self):
        from playwright.async_api import async_playwright
        from zepuppeteer.tasks.colab.open_notebook import run as open_notebook

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-named-profile-") as temporary:
            root = Path(temporary)
            brave = root / "home/.config/BraveSoftware/Brave-Browser"
            for directory in ("Default", "Profile 7"):
                (brave / directory).mkdir(parents=True)
            (brave / "Local State").write_text(json.dumps({"profile": {"info_cache": {
                "Default": {"name": "Other"}, "Profile 7": {"name": "AI-acc5"},
            }}}))
            account = root / "profiles/acc-5"
            (account / "src/Default").mkdir(parents=True)
            (account / "profile.toml").write_text(
                '[profile]\nbrave_browser_displayName="AI-acc5"\n'
                '[profile.platforms.colab]\nlogin_username="account@example.test"\n'
                'worker_url="https://colab.research.google.com/drive/named-test"\n')
            with patch("zepuppeteer.profiles.Path.home", return_value=root / "home"):
                profiles = discover_profiles(root / "profiles")

            async def check_and_open(ctx):
                page = await ctx.browser.new_page()
                await page.goto("brave://version")
                self.assertEqual(await page.locator("#profile_path").inner_text(), str(brave / "Profile 7"))
                await page.close()

                async def respond(route):
                    await route.fulfill(content_type="text/html", body='''<!doctype html>
                        <title>Test notebook - Colab</title><div role="menubar">File</div>
                        <main aria-label="Notebook">Notebook</main>
                        <a role="button" aria-label="Google Account: Test (account@example.test)">Account</a>''')

                await ctx.browser.route("https://colab.research.google.com/**", respond)
                return await open_notebook(ctx)

            workflow = Workflow("named-colab", (Step("open", "colab.open_notebook"),))
            with StateStore(root / "runs.sqlite") as state:
                async with async_playwright() as playwright:
                    summary = await run_workflow(
                        playwright, profiles, BRAVE, workflow, {"colab.open_notebook": check_and_open},
                        state, root / "artifacts", headless=True,
                    )
            self.assertEqual((summary.status, summary.succeeded), ("success", 1))

    async def test_demo_workflow_on_two_disposable_managed_profiles(self):
        from playwright.async_api import async_playwright

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-workflow-") as temporary:
            root = Path(temporary)
            for name in ["one", "two"]:
                (root / "profiles" / name / "src").mkdir(parents=True)
            profiles = discover_profiles(root / "profiles")
            workflow = load_workflow("config/workflows/demo.toml")
            with StateStore(root / "runs.sqlite") as state:
                async with async_playwright() as playwright:
                    summary = await run_workflow(playwright, profiles, BRAVE, workflow, resolve_tasks(workflow),
                                                  state, root / "artifacts", headless=True)
            self.assertEqual((summary.status, summary.succeeded), ("success", 2))
            details = read_history(root / "runs.sqlite", run_id=summary.run_id)
            self.assertTrue(all(step["status"] == "success" for step in details["steps"]))
            for step in details["steps"]:
                self.assertTrue(Path(step["outputs"]["report_path"]).is_file())
            self.assertEqual(len({step["outputs"]["report_path"] for step in details["steps"]}), 2)

    async def test_failed_workflow_saves_a_real_screenshot_and_trace(self):
        from playwright.async_api import async_playwright

        async def fail(ctx):
            page = await ctx.browser.new_page()
            await page.set_content("<h1>Failure evidence</h1>")
            raise RuntimeError("Deliberate local failure")

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-evidence-") as temporary:
            root = Path(temporary)
            workflow = Workflow("failure", (Step("fail", "fail"),))
            with StateStore(root / "runs.sqlite") as state:
                async with async_playwright() as playwright:
                    summary = await run_workflow(playwright, [Profile("one", root / "browser")], BRAVE,
                                                  workflow, {"fail": fail}, state, root / "artifacts", headless=True)
            self.assertEqual(summary.failed, 1)
            details = read_history(root / "runs.sqlite", run_id=summary.run_id)
            directory = Path(details["steps"][0]["artifact_dir"])
            evidence = json.loads((directory / "failure.json").read_text())
            self.assertIn("Deliberate local failure", evidence["error"])
            self.assertTrue(any(item.get("screenshot") for item in evidence["pages"]))
            for image in directory.glob("*.png"):
                self.assertTrue(image.read_bytes().startswith(b"\x89PNG"))
            with zipfile.ZipFile(directory / "trace.zip") as trace:
                self.assertIsNone(trace.testzip())
                self.assertTrue(any(name.endswith(".trace") for name in trace.namelist()))

    async def test_webdriver_is_undefined_in_pages_frames_and_popups(self):
        from playwright.async_api import async_playwright

        async def task(context, profile):
            context.set_default_timeout(10_000)
            # Persistent contexts open a startup tab before task execution.
            self.assertTrue(context.pages)
            for page in context.pages:
                for frame in page.frames:
                    self.assertTrue(await frame.evaluate("navigator.webdriver === undefined"))

            async def respond(route):
                html = """<!doctype html><script>
                    window.webdriverWasUndefined = navigator.webdriver === undefined;
                </script>"""
                if route.request.url.endswith("/main"):
                    html += '<iframe src="/frame"></iframe><a href="/popup" target="_blank">Open</a>'
                await route.fulfill(content_type="text/html", body=html)

            await context.route("https://webdriver.test/**", respond)
            page = await context.new_page()
            self.assertTrue(await page.evaluate("navigator.webdriver === undefined"))
            await page.goto("https://webdriver.test/main")
            self.assertEqual(len(page.frames), 2)
            for frame in page.frames:
                self.assertTrue(await frame.evaluate("window.webdriverWasUndefined"))

            await page.reload()
            self.assertTrue(await page.evaluate("window.webdriverWasUndefined"))
            await page.frames[1].goto("https://webdriver.test/frame-next")
            self.assertTrue(await page.frames[1].evaluate("window.webdriverWasUndefined"))

            async with page.expect_popup() as popup_info:
                await page.get_by_role("link", name="Open").click()
            popup = await popup_info.value
            await popup.wait_for_load_state()
            self.assertTrue(await popup.evaluate("window.webdriverWasUndefined"))

            await page.goto("https://webdriver.test/next")
            self.assertTrue(await page.evaluate("window.webdriverWasUndefined"))

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-webdriver-") as directory:
            async with async_playwright() as playwright:
                await run_profiles(
                    playwright, [Profile("webdriver-test", Path(directory))], BRAVE,
                    task, headless=True,
                )


if __name__ == "__main__":
    unittest.main()
