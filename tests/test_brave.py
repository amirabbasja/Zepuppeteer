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
    Profile, discover_profiles, find_brave, hide_webdriver, install_brave, launch_options,
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
        options = launch_options("/usr/bin/brave-browser", profiles[1])
        self.assertIn("--profile-directory=Profile 1", options["args"])
        self.assertTrue(options["chromium_sandbox"])
        self.assertNotIn("--no-sandbox", options["args"])
        self.assertIn("--password-store=basic", options["ignore_default_args"])
        self.assertIn("--use-mock-keychain", options["ignore_default_args"])

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
            options = launch_options("/usr/bin/brave-browser", Profile("test", self.root))
            self.assertEqual(os.environ, inherited)
        self.assertEqual(options["env"], {
            **inherited, "GSETTINGS_SCHEMA_DIR": "/usr/share/glib-2.0/schemas",
        })

    @patch("zepuppeteer.brave.platform.system", return_value="Linux")
    @patch.object(Path, "is_file", return_value=False)
    def test_missing_system_schemas_preserves_default_environment(self, *_):
        options = launch_options("/brave", Profile("test", self.root))
        self.assertNotIn("env", options)

    @patch("zepuppeteer.brave.platform.system", return_value="Darwin")
    def test_other_platforms_preserve_default_environment(self, *_):
        options = launch_options("/brave", Profile("test", self.root))
        self.assertNotIn("env", options)

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
    def context(self):
        return SimpleNamespace(on=Mock(), close=AsyncMock(), add_init_script=AsyncMock(), pages=[])

    async def test_browser_setup_continues_after_restored_tab_closes(self):
        closed = Mock(return_value=False)

        async def close_during_evaluation(_):
            closed.return_value = True
            raise RuntimeError("Target page, context or browser has been closed")

        stale_frame = SimpleNamespace(is_detached=Mock(return_value=False),
                                      evaluate=AsyncMock(side_effect=close_during_evaluation))
        live_frame = SimpleNamespace(is_detached=Mock(return_value=False), evaluate=AsyncMock())
        context = self.context()
        context.pages = [SimpleNamespace(is_closed=closed, frames=[stale_frame]),
                         SimpleNamespace(is_closed=Mock(return_value=False), frames=[live_frame])]
        await hide_webdriver(context)
        live_frame.evaluate.assert_awaited_once()

    async def test_browser_setup_ignores_detached_or_navigating_frames(self):
        detached = Mock(side_effect=[False, True])
        vanished = SimpleNamespace(is_detached=detached, evaluate=AsyncMock(side_effect=RuntimeError("Frame detached")))
        navigating = SimpleNamespace(is_detached=Mock(return_value=False), evaluate=AsyncMock(
            side_effect=RuntimeError("Execution context was destroyed, most likely because of a navigation")))
        already_detached = SimpleNamespace(is_detached=Mock(return_value=True), evaluate=AsyncMock())
        context = self.context()
        context.pages = [SimpleNamespace(is_closed=Mock(return_value=False),
                                         frames=[vanished, navigating, already_detached])]
        await hide_webdriver(context)
        already_detached.evaluate.assert_not_awaited()

    async def test_browser_setup_does_not_hide_failure_in_live_frame(self):
        frame = SimpleNamespace(is_detached=Mock(return_value=False),
                                evaluate=AsyncMock(side_effect=RuntimeError("Script failed")))
        context = self.context()
        context.pages = [SimpleNamespace(is_closed=Mock(return_value=False), frames=[frame])]
        with self.assertRaisesRegex(RuntimeError, "Script failed"):
            await hide_webdriver(context)

    async def test_browser_setup_reports_disconnected_browser(self):
        context = self.context()
        context.browser = SimpleNamespace(is_connected=Mock(return_value=False))
        with self.assertRaisesRegex(RuntimeError, "Brave disconnected"):
            await hide_webdriver(context)

    async def test_profiles_run_sequentially_and_each_context_closes(self):
        contexts = [self.context(), self.context()]
        profiles = [Profile(name, Path("/tmp") / name) for name in ["one", "two"]]
        seen = []

        async def task(context, profile):
            if seen:
                contexts[0].close.assert_awaited_once()
            seen.append(profile.name)

        launch = AsyncMock(side_effect=contexts)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        await run_profiles(playwright, profiles, "/brave", task, headless=True)
        self.assertEqual(seen, ["one", "two"])
        for context in contexts:
            context.close.assert_awaited_once()

    async def test_task_failure_closes_context_and_stops_next_profile(self):
        context = self.context()
        launch = AsyncMock(return_value=context)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        task = AsyncMock(side_effect=RuntimeError("Task failed"))
        profiles = [Profile(name, Path("/tmp") / name) for name in ["one", "two"]]
        with self.assertRaisesRegex(RuntimeError, "Task failed"):
            await run_profiles(playwright, profiles, "/brave", task, headless=True)
        context.close.assert_awaited_once()
        launch.assert_awaited_once()

    async def test_browser_setup_failure_closes_context_before_running_task(self):
        context = self.context()
        context.add_init_script.side_effect = RuntimeError("Browser setup failed")
        launch = AsyncMock(return_value=context)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        task = AsyncMock()
        profiles = [Profile(name, Path("/tmp") / name) for name in ["one", "two"]]
        with self.assertRaisesRegex(RuntimeError, "Browser setup failed"):
            await run_profiles(playwright, profiles, "/brave", task, headless=True)
        task.assert_not_awaited()
        context.close.assert_awaited_once()
        launch.assert_awaited_once()

    async def test_cancellation_closes_active_context(self):
        context = self.context()
        launch = AsyncMock(return_value=context)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        task = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await run_profiles(playwright, [Profile("one", Path("/tmp/one"))], "/brave", task, headless=True)
        context.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
