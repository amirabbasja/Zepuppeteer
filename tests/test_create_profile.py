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

from zepuppeteer.brave import find_brave, native_command
from zepuppeteer.cli import main
from zepuppeteer.config import read_toml
from zepuppeteer.profile_transfer import atomic_write
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

    async def test_existing_configuration_can_be_previewed_without_changes(self):
        folder = self.root / "existing"
        folder.mkdir(parents=True)
        manifest = folder / "profile.toml"
        original = b'enabled=false\n# Keep existing account settings\n'
        manifest.write_bytes(original)
        with contextlib.redirect_stdout(io.StringIO()):
            result = await createProfile("existing", profiles_dir=self.root, dry_run=True)
        self.assertEqual(result.outputs["user_data_dir"], str(folder / "src"))
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(list(folder.iterdir()), [manifest])

    async def test_symlinked_and_non_directory_profile_paths_are_rejected(self):
        folder = self.root / "existing"
        folder.mkdir(parents=True)
        (self.root / "linked").symlink_to(folder, target_is_directory=True)
        (self.root / "dangling").symlink_to(self.root / "missing", target_is_directory=True)
        for name in ("linked", "dangling"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "symlinked"):
                await createProfile(name, profiles_dir=self.root, dry_run=True)
        (self.root / "file").write_text("keep")
        with self.assertRaisesRegex(ValueError, "Not a profile directory"):
            await createProfile("file", profiles_dir=self.root, dry_run=True)
        (folder / "src").symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            await createProfile("existing", profiles_dir=self.root, dry_run=True)
        (folder / "src").unlink()
        (folder / "profile.toml").symlink_to(self.root / "file")
        with self.assertRaisesRegex(ValueError, "symlinked"):
            await createProfile("existing", profiles_dir=self.root, dry_run=True)

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
                patch(f"{MODULE}.validate_desktop"), \
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

        closed = asyncio.Event()
        handlers["close"] = closed.set
        async def wait_closed():
            self.opened.set()
            await closed.wait()
        self.browser = SimpleNamespace(close=AsyncMock(), wait_closed=wait_closed)
        launch = AsyncMock(return_value=self.browser, side_effect=launch_error)
        with patch("zepuppeteer.runner.launch_browser", launch), \
                patch(f"{MODULE}.validate_desktop"), \
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
                self.assertEqual(launch.call_args.args[1], profile)
                handlers["close"]()
                result = await asyncio.wait_for(task, timeout=3)
                self.assertEqual(result.outputs["manifest_path"], str(profile.manifest_path))
                self.browser.close.assert_awaited_once()
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def finish_setup(self, name):
        async with self.session() as (handlers, launch):
            task = asyncio.create_task(createProfile(name, profiles_dir=self.root))
            try:
                await asyncio.wait_for(self.opened.wait(), timeout=3)
                handlers["close"]()
                result = await asyncio.wait_for(task, timeout=3)
                return result, {"user_data_dir": str(launch.call_args.args[1].user_data_dir), "args": native_command("/brave", launch.call_args.args[1])}
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def test_initializes_existing_account_and_preserves_all_configuration(self):
        folder = self.root / "acc-5"
        folder.mkdir(parents=True)
        manifest = folder / "profile.toml"
        original = (b'# Account entered before browser setup\nenabled = false\ntags = ["test"]\n'
                    b'[settings]\ntimezone = "Asia/Tehran"\n'
                    b'[profile.platforms.colab]\nlogin_username = "private@example.test"\n')
        manifest.write_bytes(original)
        # Setup for one account must not try to resolve other unfinished accounts.
        other = self.root / "unfinished"
        other.mkdir()
        (other / "profile.toml").write_text('[profile]\nbrave_browser_displayName="Missing"\n')
        result, options = await self.finish_setup("acc-5")
        self.assertEqual(manifest.read_bytes(), original)
        self.assertTrue((folder / "src").is_dir())
        self.assertEqual(options["user_data_dir"], str(folder / "src"))
        self.assertEqual(result.outputs["manifest_path"], str(manifest))

    async def test_reopens_existing_browser_data_without_replacing_it(self):
        folder = self.root / "acc-5"
        browser = folder / "src/Profile 2"
        browser.mkdir(parents=True)
        cookies = browser / "Cookies"
        cookies.write_bytes(b"existing saved session")
        manifest = folder / "profile.toml"
        original = b'profile_directory="Profile 2"\n[settings]\nvalue="keep"\n'
        manifest.write_bytes(original)
        for _ in range(2):
            _, options = await self.finish_setup("acc-5")
            self.assertEqual(options["user_data_dir"], str(folder / "src"))
            self.assertIn("--profile-directory=Profile 2", options["args"])
            self.assertEqual(cookies.read_bytes(), b"existing saved session")
            self.assertEqual(manifest.read_bytes(), original)

    async def test_empty_account_folder_uses_managed_src(self):
        folder = self.root / "acc-5"
        folder.mkdir(parents=True)
        _, options = await self.finish_setup("acc-5")
        self.assertTrue((folder / "profile.toml").is_file())
        self.assertEqual(options["user_data_dir"], str(folder / "src"))
        self.assertEqual(discover_profiles(self.root)[0].user_data_dir, folder / "src")

    async def test_invalid_configuration_is_rejected_before_any_setup(self):
        folder = self.root / "acc-5"
        folder.mkdir(parents=True)
        manifest = folder / "profile.toml"
        for contents in ('enabled="false"', '[profile]\nbrave_browser_displayName=""', 'broken=['):
            manifest.write_text(contents)
            with self.subTest(contents=contents), patch(f"{MODULE}.find_brave") as finder:
                with self.assertRaises(ValueError):
                    await createProfile("acc-5", profiles_dir=self.root)
                finder.assert_not_called()
                self.assertEqual(manifest.read_text(), contents)
                self.assertFalse((folder / "src").exists())

    def named_account(self):
        folder = self.root / "acc-5"
        folder.mkdir(parents=True)
        (folder / "profile.toml").write_text('[profile]\nbrave_browser_displayName="AI-acc5"\n')
        home = self.root.parent / "home"
        return folder, home, home / ".config/BraveSoftware/Brave-Browser"

    async def test_named_profile_dry_run_does_not_create_browser_data(self):
        folder, home, brave = self.named_account()
        output = io.StringIO()
        with patch("zepuppeteer.profiles.Path.home", return_value=home), \
                contextlib.redirect_stdout(output):
            result = await createProfile("acc-5", profiles_dir=self.root, dry_run=True)
        self.assertEqual(result.outputs["user_data_dir"], str(brave))
        self.assertEqual(result.outputs["profile_directory"], "Default")
        self.assertEqual(result.outputs["browser_display_name"], "AI-acc5")
        self.assertFalse(home.exists())
        self.assertFalse((folder / "src").exists())

    async def test_creates_named_profile_and_reuses_it_on_subsequent_setup(self):
        folder, home, brave = self.named_account()
        manifest = folder / "profile.toml"
        original = manifest.read_bytes()
        with patch("zepuppeteer.profiles.Path.home", return_value=home):
            _, options = await self.finish_setup("acc-5")
            self.assertEqual(options["user_data_dir"], str(brave))
            self.assertIn("--profile-directory=Default", options["args"])
            profile, = discover_profiles(self.root)
            self.assertEqual(profile.browser_display_name, "AI-acc5")
            self.assertEqual(profile.profile_directory, "Default")
            cookies = brave / "Default/Cookies"
            cookies.write_bytes(b"saved login")
            await self.finish_setup("acc-5")
            self.assertEqual(cookies.read_bytes(), b"saved login")
        state = json.loads((brave / "Local State").read_text())
        self.assertEqual(state["profile"]["info_cache"]["Default"]["name"], "AI-acc5")
        self.assertEqual(json.loads((brave / "Default/Preferences").read_text())["profile"]["name"], "AI-acc5")
        self.assertEqual(manifest.read_bytes(), original)
        self.assertFalse((folder / "src").exists())

    async def test_named_setup_preserves_other_profiles_and_avoids_directory_collisions(self):
        folder, home, brave = self.named_account()
        (brave / "Default").mkdir(parents=True)
        cookies = brave / "Default/Cookies"
        cookies.write_bytes(b"personal session")
        (brave / "Profile 2").mkdir()
        state = {"profile": {"info_cache": {"Default": {"name": "Personal"},
                                            "Profile 1": {"name": "Other"}},
                             "last_used": "Default"}, "other_settings": {"keep": True}}
        (brave / "Local State").write_text(json.dumps(state))
        with patch("zepuppeteer.profiles.Path.home", return_value=home):
            _, options = await self.finish_setup("acc-5")
            self.assertIn("--profile-directory=Profile 3", options["args"])
            self.assertEqual(discover_profiles(self.root)[0].profile_directory, "Profile 3")
        updated = json.loads((brave / "Local State").read_text())
        new_entry = updated["profile"]["info_cache"].pop("Profile 3")
        self.assertEqual(new_entry["name"], "AI-acc5")
        self.assertEqual(updated, state)
        self.assertEqual(cookies.read_bytes(), b"personal session")
        self.assertFalse((folder / "src").exists())

    async def test_named_setup_rejects_open_browser_without_registering_profile(self):
        _, home, brave = self.named_account()
        with patch("zepuppeteer.profiles.Path.home", return_value=home):
            async with self.session():
                with patch(f"{MODULE}.ensure_closed", side_effect=RuntimeError("Brave is running")), \
                        self.assertRaisesRegex(RuntimeError, "Brave is running"):
                    await createProfile("acc-5", profiles_dir=self.root)
        self.assertFalse(brave.exists())

    async def test_failed_named_registration_preserves_state_and_can_be_retried(self):
        _, home, brave = self.named_account()
        (brave / "Default").mkdir(parents=True)
        state_path = brave / "Local State"
        original = b'{"profile":{"info_cache":{"Default":{"name":"Personal"}}},"keep":true}'
        state_path.write_bytes(original)

        def fail_registration(path, data):
            if path == state_path:
                raise OSError("Cannot save browser metadata")
            atomic_write(path, data)

        with patch("zepuppeteer.profiles.Path.home", return_value=home):
            async with self.session():
                with patch(f"{MODULE}.atomic_write", side_effect=fail_registration), \
                        self.assertRaisesRegex(OSError, "Cannot save browser metadata"):
                    await createProfile("acc-5", profiles_dir=self.root)
            self.assertEqual(state_path.read_bytes(), original)
            self.assertFalse((brave / "Profile 1").exists())
            _, options = await self.finish_setup("acc-5")
            self.assertIn("--profile-directory=Profile 1", options["args"])

    async def test_named_setup_rejects_duplicate_names_and_symlinked_state(self):
        _, home, brave = self.named_account()
        brave.mkdir(parents=True)
        state_path = brave / "Local State"
        original = json.dumps({"profile": {"info_cache": {"Default": {"name": "AI-acc5"},
                                                        "Profile 1": {"name": "AI-acc5"}}}})
        state_path.write_text(original)
        with patch("zepuppeteer.profiles.Path.home", return_value=home):
            with self.assertRaisesRegex(ValueError, "Multiple Brave profiles"):
                await createProfile("acc-5", profiles_dir=self.root, dry_run=True)
            self.assertEqual(state_path.read_text(), original)
            state_path.rename(brave / "saved-state")
            state_path.symlink_to(brave / "saved-state")
            with self.assertRaisesRegex(ValueError, "symlinked"):
                await createProfile("acc-5", profiles_dir=self.root, dry_run=True)
            self.assertEqual((brave / "saved-state").read_text(), original)

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
        await self.finish_setup("new")


class CreateProfileCliTests(unittest.TestCase):
    def test_command_dispatches_without_discovering_existing_profiles(self):
        for command in ("createProfile", "create-profile"):
            with self.subTest(command=command), patch(f"{MODULE}.createProfile", new_callable=AsyncMock) as task, \
                    patch("zepuppeteer.cli.discover_profiles", side_effect=AssertionError("No discovery needed")):
                main([command, "account-3", "--profiles-dir", "/tmp/new-profiles", "--dry-run", "--no-install"])
                task.assert_awaited_once_with("account-3", profiles_dir="/tmp/new-profiles", executable=None,
                                             dry_run=True, no_install=True)
