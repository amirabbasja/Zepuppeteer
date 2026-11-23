import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zepuppeteer.cli import main
from zepuppeteer.profiles import discover_profiles, select_profiles
from zepuppeteer.state import read_history


class ProfileSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-profiles-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()

    def account(self, name, metadata="", browser_directory=None):
        folder = self.profiles / name
        (folder / "src").mkdir(parents=True)
        if metadata:
            (folder / "profile.toml").write_text(metadata)
        if browser_directory:
            (folder / "src" / browser_directory).mkdir()
            (folder / "src" / "Local State").write_text("{}")
        return folder

    def test_imported_profile_uses_src_and_its_actual_browser_directory(self):
        folder = self.account("acc-5", '[profile]\nname="Existing display name"\n', "Profile 2")
        profile, = discover_profiles(self.profiles)
        self.assertEqual(profile.name, "acc-5")
        self.assertEqual(profile.user_data_dir, folder / "src")
        self.assertEqual(profile.profile_directory, "Profile 2")

    def native_browser(self, names=None):
        home = self.root / "home"
        brave = home / ".config/BraveSoftware/Brave-Browser"
        names = names or {"Default": "Personal", "Profile 2": "AI-acc5"}
        for directory in names:
            (brave / directory).mkdir(parents=True, exist_ok=True)
        (brave / "Local State").write_text(json.dumps({
            "profile": {"info_cache": {directory: {"name": name} for directory, name in names.items()}}
        }))
        return home, brave

    def test_display_name_selects_installed_brave_instead_of_local_copy(self):
        self.account("acc-5", '[profile]\nname="acc5"\nbrave_browser_displayName="AI-acc5"\n', "Default")
        home, brave = self.native_browser()
        with patch("zepuppeteer.profiles.Path.home", return_value=home):
            profile, = discover_profiles(self.profiles)
        self.assertEqual(profile.name, "acc-5")
        self.assertEqual(profile.user_data_dir, brave)
        self.assertEqual(profile.profile_directory, "Profile 2")
        self.assertEqual(profile.browser_display_name, "AI-acc5")
        self.assertEqual(profile.manifest_path, self.profiles / "acc-5/profile.toml")

    def test_colab_dry_run_resolves_manifest_display_name_before_launch(self):
        self.account("acc-5", '[profile]\nbrave_browser_displayName="AI-acc5"\n')
        home, brave = self.native_browser()
        output = io.StringIO()
        with patch("zepuppeteer.profiles.Path.home", return_value=home), \
                patch("zepuppeteer.cli.find_brave", return_value="/brave"), \
                contextlib.redirect_stdout(output):
            main(["run", "--profiles-dir", str(self.profiles), "--workflow", "colab",
                  "--profile", "acc-5", "--dry-run"])
        profile, = json.loads(output.getvalue())["profiles"]
        self.assertEqual(profile["user_data_dir"], str(brave))
        self.assertEqual(profile["profile_directory"], "Profile 2")
        self.assertEqual(profile["browser_display_name"], "AI-acc5")

    def test_display_name_does_not_fall_back_when_missing_or_duplicated(self):
        self.account("acc-5", '[profile]\nbrave_browser_displayName="AI-acc5"\n', "Default")
        for names, message in [({"Default": "Personal"}, "No Brave profile"),
                               ({"Default": "AI-acc5", "Profile 2": "AI-acc5"}, "Multiple Brave profiles"),
                               ({"Default": "ai-acc5"}, "No Brave profile")]:
            with self.subTest(names=names):
                home, _ = self.native_browser(names)
                with patch("zepuppeteer.profiles.Path.home", return_value=home), \
                        self.assertRaisesRegex(ValueError, message):
                    discover_profiles(self.profiles)

    def test_display_name_requires_existing_unsymlinked_internal_directory(self):
        self.account("acc-5", '[profile]\nbrave_browser_displayName="AI-acc5"\n')
        home, brave = self.native_browser()
        (brave / "Profile 2").rmdir()
        with patch("zepuppeteer.profiles.Path.home", return_value=home):
            with self.assertRaisesRegex(ValueError, "does not exist"):
                discover_profiles(self.profiles)
            (brave / "Profile 2").symlink_to(brave / "Default", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                discover_profiles(self.profiles)

    def test_display_name_configuration_is_validated(self):
        folder = self.account("acc-5")
        for metadata, message in [('[profile]\nbrave_browser_displayName=""', "nonempty string"),
                                  ('[profile]\nbrave_browser_displayName=5', "nonempty string"),
                                  ('profile=[]', "profile must be a table"),
                                  ('profile_directory="Default"\n[profile]\nbrave_browser_displayName="AI-acc5"',
                                   "Use either")]:
            with self.subTest(metadata=metadata):
                (folder / "profile.toml").write_text(metadata)
                with self.assertRaisesRegex(ValueError, message):
                    discover_profiles(self.profiles)

    def test_manifest_initializes_src_without_creating_it_and_preserves_settings(self):
        folder = self.profiles / "new"
        folder.mkdir()
        (folder / "profile.toml").write_text('enabled=false\ntags=["reports"]\n[settings.portal]\nregion="eu"\n')
        profile, = discover_profiles(self.profiles)
        self.assertFalse(profile.enabled)
        self.assertEqual(profile.tags, ("reports",))
        self.assertEqual(profile.settings["portal"]["region"], "eu")
        self.assertEqual(profile.user_data_dir, folder / "src")
        self.assertFalse(profile.user_data_dir.exists())

    def test_adding_metadata_to_flat_profile_keeps_its_browser_data(self):
        folder = self.profiles / "flat"
        (folder / "Default").mkdir(parents=True)
        (folder / "Local State").write_text("{}")
        (folder / "profile.toml").write_text('tags=["flat"]\n')
        self.assertEqual(discover_profiles(self.profiles)[0].user_data_dir, folder)

    def test_group_names_tags_and_enabled_are_intersected_in_discovery_order(self):
        self.account("account10", 'tags=["reports"]\n')
        self.account("account2", 'tags=["reports", "eu"]\n')
        self.account("account1", 'enabled=false\ntags=["reports"]\n')
        groups = self.root / "groups.toml"
        groups.write_text('[groups]\nteam=["account10", "account2", "account1"]\n')
        profiles = discover_profiles(self.profiles)
        selected = select_profiles(profiles, group="team", groups_file=groups, tags=["reports"])
        self.assertEqual([p.name for p in selected], ["account2", "account10"])
        selected = select_profiles(profiles, group="team", groups_file=groups,
                                   names=["account10", "account2"], tags=["eu"])
        self.assertEqual([p.name for p in selected], ["account2"])
        self.assertEqual(len(select_profiles(profiles, include_disabled=True)), 3)

    def test_unknown_group_member_and_empty_selection_fail_clearly(self):
        self.account("one")
        profiles = discover_profiles(self.profiles)
        groups = self.root / "groups.toml"
        groups.write_text('[groups]\nteam=["missing"]\n')
        with self.assertRaisesRegex(ValueError, "Unknown profiles in group"):
            select_profiles(profiles, group="team", groups_file=groups)
        with self.assertRaisesRegex(ValueError, "Unknown group"):
            select_profiles(profiles, group="typo", groups_file=groups)
        with self.assertRaisesRegex(ValueError, "No profiles match"):
            select_profiles(profiles, tags=["missing"])
        with self.assertRaisesRegex(ValueError, "Unknown profiles"):
            select_profiles(profiles, names=["missing"])

    def test_ambiguous_and_symlinked_browser_roots_are_rejected(self):
        folder = self.account("one", browser_directory="Default")
        (folder / "src" / "Profile 1").mkdir()
        with self.assertRaisesRegex(ValueError, "Multiple browser profiles"):
            discover_profiles(self.profiles)
        (folder / "profile.toml").write_text('profile_directory="Profile 1"\n')
        self.assertEqual(discover_profiles(self.profiles)[0].profile_directory, "Profile 1")
        other = self.profiles / "linked"
        other.mkdir()
        (other / "src").symlink_to(folder / "src", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            discover_profiles(self.profiles)

    def test_metadata_types_and_configured_directory_are_validated(self):
        folder = self.account("one")
        for contents, message in [('enabled="false"', "enabled"), ('tags="reports"', "tags"),
                                  ('settings=[]', "settings"), ('profile_directory="../bad"', "profile_directory"),
                                  ('profile_directory="Profile 5"', "does not exist")]:
            with self.subTest(contents=contents):
                (folder / "profile.toml").write_text(contents)
                with self.assertRaisesRegex(ValueError, message):
                    discover_profiles(self.profiles)

    def test_workflow_dry_run_has_no_browser_state_or_artifact_side_effects(self):
        self.account("one", '[settings]\nprivate_value="do-not-print"\n')
        output = io.StringIO()
        state, artifacts = self.root / "state.sqlite", self.root / "artifacts"
        with patch("zepuppeteer.cli.find_brave", return_value=None), \
                patch("zepuppeteer.cli.install_brave") as installer, contextlib.redirect_stdout(output):
            main(["run", "--profiles-dir", str(self.profiles), "--workflow", "demo", "--dry-run",
                  "--state-db", str(state), "--artifacts-dir", str(artifacts)])
        plan = json.loads(output.getvalue())
        self.assertEqual(len(plan["workflow"]["steps"]), 2)
        self.assertNotIn("do-not-print", output.getvalue())
        self.assertFalse(state.exists())
        self.assertFalse(artifacts.exists())
        installer.assert_not_called()

    def test_history_and_profile_listing_do_not_require_a_browser(self):
        self.account("one")
        database = self.root / "missing" / "runs.sqlite"
        self.assertEqual(read_history(database), [])
        self.assertFalse(database.parent.exists())
        with self.assertRaisesRegex(ValueError, "Unknown run"):
            read_history(database, run_id="missing")
        output = io.StringIO()
        with patch("zepuppeteer.cli.find_brave", side_effect=AssertionError("Should not discover Brave")), \
                contextlib.redirect_stdout(output):
            main(["profiles", "--profiles-dir", str(self.profiles)])
        self.assertEqual(json.loads(output.getvalue())[0]["name"], "one")

    def test_cli_failure_has_nonzero_exit_and_a_queryable_history_record(self):
        self.account("one")
        browser = SimpleNamespace(close=AsyncMock(), add_init_script=AsyncMock(), pages=[],
                                  tracing=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()))
        database = self.root / "runs.sqlite"
        with patch("zepuppeteer.runner.launch_browser", AsyncMock(return_value=browser)), \
                patch("zepuppeteer.cli.validate_desktop"), \
                patch("zepuppeteer.cli.find_brave", return_value="/brave"), \
                patch("zepuppeteer.cli.os.getuid", return_value=1000, create=True), \
                patch("zepuppeteer.cli.resolve_tasks", return_value={
                    "demo.fill_form": AsyncMock(side_effect=RuntimeError("Website unavailable")),
                    "demo.verify_result": AsyncMock()}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                main(["run", "--profiles-dir", str(self.profiles), "--workflow", "demo",
                      "--state-db", str(database), "--artifacts-dir", str(self.root / "artifacts")])
        self.assertEqual(failure.exception.code, 1)
        run, = read_history(database)
        self.assertEqual(run["status"], "failed")
        browser.close.assert_awaited_once()

    def test_invalid_workflow_fails_before_browser_or_runtime_files(self):
        self.account("one")
        workflow = self.root / "invalid.toml"
        workflow.write_text('[[steps]]\ntask="demo.fill_form"\nretries=1\n')
        database = self.root / "runs.sqlite"
        with patch("zepuppeteer.cli.find_brave") as discovery, \
                patch("zepuppeteer.cli.install_brave") as installer, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                main(["run", "--profiles-dir", str(self.profiles), "--workflow", str(workflow),
                      "--state-db", str(database)])
        self.assertEqual(failure.exception.code, 1)
        discovery.assert_not_called()
        installer.assert_not_called()
        self.assertFalse(database.exists())
