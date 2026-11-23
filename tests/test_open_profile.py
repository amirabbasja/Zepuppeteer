import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zepuppeteer.cli import main
from zepuppeteer.brave import native_command
from zepuppeteer.tasks.openProfile import openProfile


MODULE = "zepuppeteer.tasks.openProfile"


class OpenProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-open-profile-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = self.root / "account with spaces"
        (self.account / "src/Profile 2").mkdir(parents=True)
        self.manifest = self.account / "profile.toml"
        self.metadata = 'enabled=false\nprofile_directory="Profile 2"\n'
        self.manifest.write_text(self.metadata)
        self.cookies = self.account / "src/Profile 2/Cookies"
        self.cookies.write_bytes(b"saved session")

    async def test_preview_loads_only_selected_disabled_account_without_changes(self):
        other = self.root / "unfinished"
        other.mkdir()
        (other / "profile.toml").write_text('[profile]\nbrave_browser_displayName="Missing"\n')
        output = io.StringIO()
        with patch(f"{MODULE}.find_brave", return_value=None), \
                patch(f"{MODULE}.install_brave") as installer, \
                patch(f"{MODULE}.run_profiles") as runner, contextlib.redirect_stdout(output):
            result = await openProfile(self.account.name, profiles_dir=self.root, dry_run=True)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["profile_directory"], "Profile 2")
        self.assertEqual(plan["user_data_dir"], str(self.account / "src"))
        self.assertEqual(result.outputs["profile"], self.account.name)
        self.assertEqual(self.manifest.read_text(), self.metadata)
        self.assertEqual(self.cookies.read_bytes(), b"saved session")
        self.assertEqual(sorted(path.name for path in self.account.iterdir()), ["profile.toml", "src"])
        installer.assert_not_called()
        runner.assert_not_called()

    async def test_named_browser_profile_is_resolved_without_creating_missing_names(self):
        home = self.root / "home"
        brave = home / ".config/BraveSoftware/Brave-Browser"
        (brave / "Profile 3").mkdir(parents=True)
        state = brave / "Local State"
        original = json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "Work account"}}}})
        state.write_text(original)
        metadata = '[profile]\nbrave_browser_displayName="Work account"\n'
        self.manifest.write_text(metadata)
        with patch("zepuppeteer.profiles.Path.home", return_value=home), \
                contextlib.redirect_stdout(io.StringIO()):
            result = await openProfile(self.account.name, profiles_dir=self.root, dry_run=True)
            self.assertEqual(result.outputs["user_data_dir"], str(brave))
            self.assertEqual(result.outputs["profile_directory"], "Profile 3")
            self.manifest.write_text('[profile]\nbrave_browser_displayName="Missing"\n')
            with self.assertRaises(ValueError):
                await openProfile(self.account.name, profiles_dir=self.root, dry_run=True)
        self.assertEqual(state.read_text(), original)
        self.assertEqual(sorted(path.name for path in brave.iterdir()), ["Local State", "Profile 3"])

    async def test_missing_invalid_and_symlinked_accounts_are_not_created(self):
        (self.root / "linked").symlink_to(self.account, target_is_directory=True)
        for name in ("missing", "../outside", ".hidden", "", "linked"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                await openProfile(name, profiles_dir=self.root, dry_run=True)
        self.assertFalse((self.root / "missing").exists())

    @contextlib.asynccontextmanager
    async def session(self):
        self.opened = asyncio.Event()
        handlers = {}

        async def setup(**_):
            self.opened.set()

        closed = asyncio.Event()
        handlers["close"] = closed.set
        async def wait_closed():
            self.opened.set()
            await closed.wait()
        self.browser = SimpleNamespace(close=AsyncMock(), wait_closed=wait_closed)
        launch = AsyncMock(return_value=self.browser)
        with patch("zepuppeteer.runner.launch_browser", launch), \
                patch(f"{MODULE}.validate_desktop"), \
                patch(f"{MODULE}.find_brave", return_value="/brave"), \
                patch(f"{MODULE}.os.getuid", return_value=1000, create=True), \
                patch.dict(os.environ, {"DISPLAY": ":0"}), contextlib.redirect_stdout(io.StringIO()):
            task = asyncio.create_task(openProfile(self.account.name, profiles_dir=self.root))
            try:
                await asyncio.wait_for(self.opened.wait(), timeout=3)
                yield task, handlers, launch
            finally:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def test_browser_stays_open_until_manual_close(self):
        async with self.session() as (task, handlers, launch):
            self.assertFalse(task.done())
            self.browser.close.assert_not_awaited()
            options = {**launch.call_args.kwargs, "user_data_dir": str(launch.call_args.args[1].user_data_dir), "args": native_command("/brave", launch.call_args.args[1])}
            self.assertFalse(options["headless"])
            self.assertEqual(options["user_data_dir"], str(self.account / "src"))
            self.assertIn("--profile-directory=Profile 2", options["args"])
            handlers["close"]()
            result = await asyncio.wait_for(task, timeout=3)
            self.assertEqual(result.status, "success")
            self.browser.close.assert_awaited_once()
        self.assertEqual(self.manifest.read_text(), self.metadata)
        self.assertEqual(self.cookies.read_bytes(), b"saved session")

    async def test_interruption_closes_browser_and_preserves_account(self):
        async with self.session() as (task, _, _):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.browser.close.assert_awaited_once()
        self.assertEqual(self.manifest.read_text(), self.metadata)
        self.assertEqual(self.cookies.read_bytes(), b"saved session")


class OpenProfileCliTests(unittest.TestCase):
    def test_both_commands_dispatch_without_discovering_other_accounts(self):
        for command in ("openProfile", "open-profile"):
            with self.subTest(command=command), patch(f"{MODULE}.openProfile", new_callable=AsyncMock) as task, \
                    patch("zepuppeteer.cli.discover_profiles", side_effect=AssertionError("No discovery needed")):
                main([command, "account-3", "--profiles-dir", "/tmp/profiles", "--executable", "/brave",
                      "--dry-run", "--no-install"])
                task.assert_awaited_once_with("account-3", profiles_dir="/tmp/profiles", executable="/brave",
                                             dry_run=True, no_install=True)


if __name__ == "__main__":
    unittest.main()
