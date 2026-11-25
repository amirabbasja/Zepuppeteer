import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zepuppeteer.cli import main
from zepuppeteer.tasks.openProfile import openProfile


MODULE = "zepuppeteer.tasks.openProfile"


class OpenProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-open-profile-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "accounts with spaces"
        self.folder = self.root / "acc-5"
        self.browser_data = self.folder / "src"
        (self.browser_data / "Profile 2").mkdir(parents=True)
        (self.folder / "profile.toml").write_text('enabled=false\nprofile_directory="Profile 2"\n')
        self.process = SimpleNamespace(wait=AsyncMock(return_value=0))
        self.spawn = self.enterContext(patch(f"{MODULE}.asyncio.create_subprocess_exec",
                                            AsyncMock(return_value=self.process)))
        self.enterContext(patch(f"{MODULE}.find_brave", return_value="/usr/bin/brave-browser"))
        self.enterContext(patch(f"{MODULE}.os.getuid", return_value=1000, create=True))
        self.enterContext(patch.dict(os.environ, {"DISPLAY": ":0"}))
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))

    async def test_opens_selected_profile_natively_and_preserves_saved_data(self):
        preferences = self.browser_data / "Profile 2/Preferences"
        cookies = self.browser_data / "Profile 2/Cookies"
        preferences.write_bytes(b'{"session":{"restore_on_startup":1}}')
        cookies.write_bytes(b"saved manual login")
        original = {file: file.read_bytes() for file in self.folder.rglob("*") if file.is_file()}
        # An unfinished different account must not prevent manual login here.
        other = self.root / "unfinished"
        other.mkdir()
        (other / "profile.toml").write_text("invalid TOML [")
        result = await openProfile("acc-5", profiles_dir=self.root)
        self.assertEqual(result.status, "success")
        self.assertEqual(self.spawn.call_args.args, (
            "/usr/bin/brave-browser", f"--user-data-dir={self.browser_data}",
            "--profile-directory=Profile 2", "--new-window", "--start-fullscreen"))
        self.process.wait.assert_awaited_once()
        self.assertEqual({file: file.read_bytes() for file in self.folder.rglob("*") if file.is_file()}, original)

    async def test_named_native_profile_uses_installed_browser_data(self):
        (self.folder / "profile.toml").write_text('[profile]\nbrave_browser_displayName="Work account"\n')
        home = self.root.parent / "home"
        native = home / ".config/BraveSoftware/Brave-Browser"
        (native / "Profile 3").mkdir(parents=True)
        (native / "Local State").write_text(json.dumps({
            "profile": {"info_cache": {"Profile 3": {"name": "Work account"}}}}))
        with patch("zepuppeteer.profiles.Path.home", return_value=home):
            result = await openProfile("acc-5", profiles_dir=self.root)
        self.assertEqual(result.outputs["browser_display_name"], "Work account")
        self.assertEqual(self.spawn.call_args.args[1:3], (
            f"--user-data-dir={native}", "--profile-directory=Profile 3"))

    async def test_dry_run_prints_command_without_launching_or_changing_files(self):
        before = {file: file.read_bytes() for file in self.folder.rglob("*") if file.is_file()}
        with patch.dict(os.environ, {}, clear=True):
            await openProfile("acc-5", profiles_dir=self.root, dry_run=True)
        plan = json.loads(self.output.getvalue())
        self.assertEqual(plan["profile_directory"], "Profile 2")
        self.assertIn(f"--user-data-dir={self.browser_data}", plan["command"])
        self.spawn.assert_not_awaited()
        self.assertEqual({file: file.read_bytes() for file in self.folder.rglob("*") if file.is_file()}, before)

    async def test_missing_accounts_or_data_fail_before_launch(self):
        new = self.root / "new"
        new.mkdir()
        (new / "profile.toml").write_text("enabled=true\n")
        for name in ("missing", "new"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                await openProfile(name, profiles_dir=self.root)
        self.assertFalse((self.root / "missing").exists())
        self.assertFalse((new / "src").exists())
        self.spawn.assert_not_awaited()

    async def test_invalid_names_and_symlinked_accounts_fail_before_launch(self):
        (self.root / "linked").symlink_to(self.folder, target_is_directory=True)
        for name in ("", " ", ".", "..", ".hidden", "../acc-5", "a/b", "a\\b", "a\x00b", "linked"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                await openProfile(name, profiles_dir=self.root)
        self.spawn.assert_not_awaited()

    async def test_missing_browser_display_or_root_user_fail_before_launch(self):
        with patch(f"{MODULE}.find_brave", return_value=None), \
                self.assertRaisesRegex(RuntimeError, "Brave is not installed"):
            await openProfile("acc-5", profiles_dir=self.root)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "desktop display"):
            await openProfile("acc-5", profiles_dir=self.root)
        with patch(f"{MODULE}.os.getuid", return_value=0), self.assertRaisesRegex(RuntimeError, "not root"):
            await openProfile("acc-5", profiles_dir=self.root)
        self.spawn.assert_not_awaited()

    async def test_native_browser_failure_is_reported(self):
        self.process.wait.return_value = 7
        with self.assertRaisesRegex(RuntimeError, "Brave exited with status 7"):
            await openProfile("acc-5", profiles_dir=self.root)

    async def test_manual_launch_recovers_previous_studio_locks_but_dry_run_does_not(self):
        lock = self.browser_data / "SingletonLock"
        lock.symlink_to("previous-studio-96984")
        (self.browser_data / "SingletonSocket").symlink_to(self.root / "missing-tmp/SingletonSocket")
        with patch("zepuppeteer.profile_transfer.socket.gethostname", return_value="current-studio"), \
                patch("zepuppeteer.profile_transfer._no_local_browser", return_value=True):
            await openProfile("acc-5", profiles_dir=self.root, dry_run=True)
            self.assertTrue(lock.is_symlink())
            self.spawn.assert_not_awaited()
            await openProfile("acc-5", profiles_dir=self.root)
        self.assertFalse(lock.is_symlink())
        self.spawn.assert_awaited_once()


class OpenProfileCliTests(unittest.TestCase):
    def test_both_command_names_dispatch_without_discovering_unrelated_profiles(self):
        for command in ("openProfile", "open-profile"):
            with self.subTest(command=command), patch(f"{MODULE}.openProfile", new_callable=AsyncMock) as task, \
                    patch("zepuppeteer.cli.discover_profiles", side_effect=AssertionError("No discovery needed")):
                main([command, "acc-5", "--profiles-dir", "/tmp/accounts", "--executable", "/usr/bin/brave",
                      "--dry-run"])
                task.assert_awaited_once_with("acc-5", profiles_dir="/tmp/accounts", executable="/usr/bin/brave",
                                             dry_run=True)
