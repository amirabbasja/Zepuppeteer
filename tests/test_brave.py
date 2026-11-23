import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zepuppeteer.brave import (
    Profile, discover_profiles, find_brave, install_brave, native_command, browser_environment,
    prepare_profile_for_launch, run_command,
)
from zepuppeteer.cli import load_task, main, run_profiles


class BraveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zepuppeteer-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_isolated_profiles_are_sorted_and_unrelated_entries_ignored(self):
        for name in ["account10", "account2", ".hidden"]:
            (self.root / name).mkdir()
        (self.root / "notes.txt").touch()
        (self.root / "linked-profile").symlink_to(self.root / "account2", target_is_directory=True)
        profiles = discover_profiles(self.root)
        self.assertEqual([p.name for p in profiles], ["account2", "account10"])
        self.assertEqual(profiles[0].user_data_dir, self.root / "account2")
        self.assertIsNone(profiles[0].profile_directory)

    def test_native_layout_shares_root_and_selects_actual_profiles(self):
        (self.root / "Local State").write_text("{}")
        for name in ["Default", "Profile 10", "Profile 1", "ShaderCache"]:
            (self.root / name).mkdir()
        profiles = discover_profiles(self.root)
        self.assertEqual([p.profile_directory for p in profiles], ["Default", "Profile 1", "Profile 10"])
        self.assertTrue(all(p.user_data_dir == self.root for p in profiles))
        options = native_command("/usr/bin/brave-browser", profiles[1])
        self.assertIn("--profile-directory=Profile 1", options)
        self.assertFalse(any("automation" in flag or "remote-debugging" in flag for flag in options))
        self.assertNotIn("--no-sandbox", options)

    def test_empty_or_incorrect_directory_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "No profiles found"):
            discover_profiles(self.root)
        (self.root / "Preferences").write_text("{}")
        with self.assertRaisesRegex(ValueError, "parent user-data directory"):
            discover_profiles(self.root)

    @patch("zepuppeteer.brave.platform.system", return_value="Linux")
    @patch.object(Path, "is_file", return_value=True)
    def test_browser_uses_system_schemas_without_changing_parent_environment(self, *_):
        inherited = {
            "GSETTINGS_SCHEMA_DIR": "/old-environment/share/glib-2.0/schemas",
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "PATH": "/custom/bin:/usr/bin",
        }
        with patch.dict(os.environ, inherited, clear=True):
            options = browser_environment()
            self.assertEqual(os.environ, inherited)
        self.assertEqual(options, {
            **inherited, "GSETTINGS_SCHEMA_DIR": "/usr/share/glib-2.0/schemas",
        })

    @patch("zepuppeteer.brave.platform.system", return_value="Linux")
    @patch.object(Path, "is_file", return_value=False)
    def test_missing_system_schemas_preserves_default_environment(self, *_):
        options = browser_environment()
        self.assertEqual(options, dict(os.environ))

    @patch("zepuppeteer.brave.platform.system", return_value="Darwin")
    def test_other_platforms_preserve_default_environment(self, *_):
        options = browser_environment()
        self.assertEqual(options, dict(os.environ))

    def test_invalid_executable_does_not_fall_back(self):
        with self.assertRaisesRegex(ValueError, "missing or not executable"):
            find_brave("/does-not-exist/brave")

    def test_blank_startup_preserves_other_preferences_and_creates_backup_once(self):
        directory = self.root / "Profile 2"
        directory.mkdir()
        original = json.dumps({"session": {"restore_on_startup": 1, "startup_urls": ["https://example.test"]},
                               "profile": {"exit_type": "Crashed", "name": "AI-acc5"},
                               "pinned_tabs": [{"url": "https://example.test"}],
                               "other_setting": {"keep": True}}).encode()
        preferences = directory / "Preferences"
        preferences.write_bytes(original)
        cookies = directory / "Cookies"
        cookies.write_bytes(b"existing session cookies")
        profile = Profile("acc-5", self.root, "Profile 2")
        prepare_profile_for_launch(profile)
        data = json.loads(preferences.read_bytes())
        self.assertEqual(data["session"]["restore_on_startup"], 5)
        self.assertEqual(data["profile"], {"exit_type": "Normal", "name": "AI-acc5"})
        self.assertEqual(data["other_setting"], {"keep": True})
        self.assertEqual(data["pinned_tabs"], [])
        self.assertEqual(cookies.read_bytes(), b"existing session cookies")
        prepare_profile_for_launch(profile)
        self.assertEqual((directory / "Preferences.before-zepuppeteer-startup").read_bytes(), original)

    def test_blank_startup_does_not_create_profile_data(self):
        prepare_profile_for_launch(Profile("new", self.root / "new"))
        self.assertFalse((self.root / "new").exists())

    def test_command_failure_is_propagated(self):
        with self.assertRaises(subprocess.CalledProcessError):
            run_command([sys.executable, "-c", "raise SystemExit(7)"])

    @patch("zepuppeteer.brave.platform.system", return_value="Linux")
    @patch("zepuppeteer.brave.os.getuid", return_value=1000, create=True)
    @patch.object(Path, "read_text", return_value='ID=ubuntu\nID_LIKE=debian\n')
    def test_installer_uses_official_repository(self, *_):
        calls = []
        install_brave(calls.append)
        self.assertTrue(all(command[0] == "sudo" for command in calls))
        base = "https://brave-browser-apt-release.s3.brave.com"
        self.assertTrue(any(f"{base}/brave-browser-archive-keyring.gpg" in c for c in calls))
        self.assertTrue(any(f"{base}/brave-browser.sources" in c for c in calls))
        self.assertEqual(calls[-1], ["sudo", "apt-get", "install", "-y", "brave-browser"])
        failed = Mock(side_effect=RuntimeError("APT failed"))
        with self.assertRaisesRegex(RuntimeError, "APT failed"):
            install_brave(failed)
        failed.assert_called_once()

    @patch("zepuppeteer.brave.platform.system", return_value="Linux")
    @patch.object(Path, "read_text", return_value='ID=fedora\n')
    def test_installer_rejects_unsupported_linux(self, *_):
        command = Mock()
        with self.assertRaisesRegex(RuntimeError, "supports Ubuntu/Debian"):
            install_brave(command)
        command.assert_not_called()

    def test_dry_run_has_no_side_effects(self):
        folder = self.root / "account with spaces"
        folder.mkdir()
        output = io.StringIO()
        with patch("zepuppeteer.cli.find_brave", return_value=None), \
                patch("zepuppeteer.cli.install_brave") as installer, \
                contextlib.redirect_stdout(output):
            main(["--profiles-dir", str(self.root), "--dry-run"])
        result = json.loads(output.getvalue())
        self.assertIn("will be installed", result["executable"])
        self.assertEqual(result["profiles"][0]["name"], "account with spaces")
        self.assertEqual(list(folder.iterdir()), [])
        installer.assert_not_called()

    def test_invalid_task_is_rejected(self):
        path = self.root / "task.py"
        path.write_text("def run(context, profile): pass\n")
        with self.assertRaisesRegex(ValueError, "async def run"):
            load_task(str(path))

    def test_async_task_loads(self):
        path = self.root / "task.py"
        path.write_text("async def run(context, profile): return profile.name\n")
        task = load_task(str(path))
        self.assertEqual(asyncio.run(task(None, Profile("test", self.root))), "test")


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_run_sequentially_and_close(self):
        profiles = [Profile(name, Path("/tmp") / name) for name in ("one", "two")]
        contexts = [SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())]
        seen = []
        async def task(context, profile):
            if seen:
                contexts[0].close.assert_awaited_once()
            seen.append(profile.name)
        with patch("zepuppeteer.runner.launch_browser", AsyncMock(side_effect=contexts)):
            await run_profiles(profiles, "/brave", task)
        self.assertEqual(seen, ["one", "two"])
        for context in contexts:
            context.close.assert_awaited_once()

    async def test_failure_or_cancellation_closes_and_stops(self):
        for failure in (RuntimeError("failed"), asyncio.CancelledError()):
            context = SimpleNamespace(close=AsyncMock())
            with patch("zepuppeteer.runner.launch_browser", AsyncMock(return_value=context)) as launch:
                with self.assertRaises(type(failure)):
                    await run_profiles([Profile("one", Path("/tmp/one")), Profile("two", Path("/tmp/two"))],
                                       "/brave", AsyncMock(side_effect=failure))
                launch.assert_awaited_once()
                context.close.assert_awaited_once()
