import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zepuppeteer.brave import find_brave, launch_options
from zepuppeteer.cli import main
from zepuppeteer.config import read_toml
from zepuppeteer.profiles import discover_profiles
from zepuppeteer.tasks.createProfile import createProfile


MODULE = "zepuppeteer.tasks.createProfile"
BRAVE = find_brave()


class CreateProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-create-profile-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "profiles"

    async def test_dry_run_does_not_create_files_or_start_browser(self):
        output = io.StringIO()
        with patch(f"{MODULE}.find_brave", return_value=None), \
                patch(f"{MODULE}.install_brave") as installer, \
                patch(f"{MODULE}.run_profiles") as runner, contextlib.redirect_stdout(output):
            result = await createProfile("account with spaces", profiles_dir=self.root, dry_run=True)
        self.assertFalse(self.root.exists())
        self.assertEqual(json.loads(output.getvalue())["manifest_path"],
                         str(self.root / "account with spaces/profile.toml"))
        self.assertEqual(result.outputs["profile"], "account with spaces")
        installer.assert_not_called()
        runner.assert_not_called()

    async def test_invalid_names_cannot_escape_or_hide_from_discovery(self):
        for name in ("", " ", ".", "..", ".hidden", "../outside", "/absolute", "a/b", "a\\b", "a\x00b"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "single directory name"):
                await createProfile(name, profiles_dir=self.root, dry_run=True)
        self.assertFalse(self.root.exists())

    async def test_existing_profile_and_symlink_are_never_overwritten(self):
        folder = self.root / "existing"
        folder.mkdir(parents=True)
        manifest = folder / "profile.toml"
        original = b'enabled=false\n# Keep existing account settings\n'
        manifest.write_bytes(original)
        (self.root / "linked").symlink_to(folder, target_is_directory=True)
        (self.root / "dangling").symlink_to(self.root / "missing", target_is_directory=True)
        for name in ("existing", "linked", "dangling"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "already exists"):
                await createProfile(name, profiles_dir=self.root, dry_run=True)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(list(folder.iterdir()), [manifest])

    async def test_native_brave_root_is_rejected_before_creation(self):
        self.root.mkdir()
        (self.root / "Local State").write_text("{}")
        with self.assertRaisesRegex(ValueError, "managed profiles directory"):
            await createProfile("new", profiles_dir=self.root, dry_run=True)
        self.assertFalse((self.root / "new").exists())

    async def test_missing_display_or_browser_does_not_create_profile(self):
        with patch(f"{MODULE}.find_brave", return_value="/brave"), \
                patch(f"{MODULE}.os.getuid", return_value=1000, create=True), \
                patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(RuntimeError, "desktop display"):
            await createProfile("new", profiles_dir=self.root)
        with patch(f"{MODULE}.find_brave", return_value=None), \
                patch(f"{MODULE}.os.getuid", return_value=1000, create=True), \
                patch.dict(os.environ, {"DISPLAY": ":0"}), \
                self.assertRaisesRegex(RuntimeError, "Brave is not installed"):
            await createProfile("new", profiles_dir=self.root, no_install=True)
        self.assertFalse(self.root.exists())

    @contextlib.asynccontextmanager
    async def session(self, *, launch_error=None):
        self.closed = asyncio.Event()
        self.opened = asyncio.Event()
        handlers = {}

        async def setup(**_):
            self.opened.set()

        async def close():
            self.closed.set()

        self.browser = SimpleNamespace(on=lambda event, callback: handlers.update({event: callback}),
                                       close=AsyncMock(side_effect=close),
                                       add_init_script=AsyncMock(side_effect=setup), pages=[],
                                       browser=SimpleNamespace(is_connected=Mock(return_value=True)))
        launch = AsyncMock(return_value=self.browser, side_effect=launch_error)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

        @contextlib.asynccontextmanager
        async def factory():
            yield playwright

        with patch("playwright.async_api.async_playwright", factory), \
                patch(f"{MODULE}.find_brave", return_value="/brave"), \
                patch(f"{MODULE}.os.getuid", return_value=1000, create=True), \
                patch.dict(os.environ, {"DISPLAY": ":0"}), contextlib.redirect_stdout(io.StringIO()):
            yield handlers, launch

    async def test_creates_discoverable_profile_and_waits_for_browser_close(self):
        async with self.session() as (handlers, launch):
            task = asyncio.create_task(createProfile("account-3", profiles_dir=self.root))
            try:
                await asyncio.wait_for(self.opened.wait(), timeout=3)
                self.assertFalse(task.done())
                profile, = discover_profiles(self.root)
                self.assertEqual(profile.name, "account-3")
                self.assertEqual(profile.user_data_dir, self.root / "account-3/src")
                self.assertEqual(read_toml(profile.manifest_path), {"enabled": True, "tags": [], "settings": {}})
                self.assertTrue(profile.user_data_dir.is_dir())
                self.assertFalse(launch.call_args.kwargs["headless"])
                self.assertEqual(launch.call_args.kwargs["user_data_dir"], str(profile.user_data_dir))
                handlers["close"]()
                result = await asyncio.wait_for(task, timeout=3)
                self.assertEqual(result.outputs["manifest_path"], str(profile.manifest_path))
                self.browser.close.assert_awaited_once()
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def test_cancellation_closes_browser_and_keeps_created_profile(self):
        async with self.session():
            task = asyncio.create_task(createProfile("new", profiles_dir=self.root))
            await asyncio.wait_for(self.opened.wait(), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.browser.close.assert_awaited_once()
        self.assertTrue((self.root / "new/profile.toml").is_file())
        self.assertTrue((self.root / "new/src").is_dir())

    async def test_launch_failure_keeps_profile_for_recovery(self):
        async with self.session(launch_error=RuntimeError("Browser failed")):
            with self.assertRaisesRegex(RuntimeError, "Browser failed"):
                await createProfile("new", profiles_dir=self.root)
        self.assertEqual(discover_profiles(self.root)[0].name, "new")


class CreateProfileCliTests(unittest.TestCase):
    def test_command_dispatches_without_discovering_existing_profiles(self):
        for command in ("createProfile", "create-profile"):
            with self.subTest(command=command), patch(f"{MODULE}.createProfile", new_callable=AsyncMock) as task, \
                    patch("zepuppeteer.cli.discover_profiles", side_effect=AssertionError("No discovery needed")):
                main([command, "account-3", "--profiles-dir", "/tmp/new-profiles", "--dry-run", "--no-install"])
                task.assert_awaited_once_with("account-3", profiles_dir="/tmp/new-profiles", executable=None,
                                             dry_run=True, no_install=True)


@unittest.skipUnless(BRAVE and importlib.util.find_spec("playwright"), "Requires Brave and Playwright")
class CreateProfileBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_cookie_and_local_storage_survive_setup_and_workflow_reopen(self):
        from playwright.async_api import async_playwright

        with tempfile.TemporaryDirectory(prefix="zepuppeteer-create-browser-") as temporary:
            root = Path(temporary) / "profiles"
            async with async_playwright() as playwright:
                async def respond(route):
                    await route.fulfill(body="<title>Local login fixture</title>", content_type="text/html")

                async def login_session(_playwright, profiles, executable):
                    # Exercise the manual setup lifecycle with a disposable headless browser.
                    from zepuppeteer.runner import run_profiles

                    async def login(context, profile):
                        await context.route("**/*", respond)
                        page = await context.new_page()
                        await page.goto("https://login.example.test/")
                        await page.evaluate("localStorage.setItem('account', 'saved')")
                        await context.add_cookies([{"name": "login", "value": "saved",
                                                   "url": "https://login.example.test/",
                                                   "expires": time.time() + 3600}])

                    await run_profiles(_playwright, profiles, executable, task=login, headless=True)

                with patch(f"{MODULE}.run_profiles", side_effect=login_session), \
                        patch.dict(os.environ, {"DISPLAY": ":0"}), contextlib.redirect_stdout(io.StringIO()):
                    await createProfile("new", profiles_dir=root, executable=BRAVE, no_install=True)
                profile, = discover_profiles(root)
                browser = await playwright.chromium.launch_persistent_context(**launch_options(BRAVE, profile, True))
                try:
                    cookies = await browser.cookies("https://login.example.test/")
                    self.assertTrue(any(cookie["name"] == "login" and cookie["value"] == "saved" for cookie in cookies))
                    await browser.route("**/*", respond)
                    page = await browser.new_page()
                    await page.goto("https://login.example.test/")
                    state = await browser.storage_state()
                    self.assertIn({"origin": "https://login.example.test",
                                   "localStorage": [{"name": "account", "value": "saved"}]}, state["origins"])
                finally:
                    await browser.close()
