from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from zepuppeteer import profile_transfer as transfer


class StaleBrowserLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-stale-lock-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "browser"
        self.data.mkdir()
        self.socket_dir = self.root / "old-host-tmp"
        self.targets = {
            "SingletonLock": "previous-studio-96984",
            "SingletonSocket": str(self.socket_dir / "SingletonSocket"),
            "SingletonCookie": "123456789",
        }
        for name, target in self.targets.items():
            (self.data / name).symlink_to(target)
        self.enterContext(patch.object(transfer.socket, "gethostname", return_value="current-studio"))
        self.no_browser = self.enterContext(patch.object(transfer, "_no_local_browser", return_value=True))

    def assert_untouched(self):
        for name, target in self.targets.items():
            self.assertEqual(os.readlink(self.data / name), target)
        self.assertFalse(list(self.data.glob("SingletonLocks.backup-*")))

    def test_previous_studio_locks_are_backed_up_and_repeated_recovery_is_noop(self):
        (self.data / "Cookies").write_bytes(b"saved login")
        transfer.recover_stale_browser_locks(self.data)
        transfer.ensure_closed(self.data)
        backup, = self.data.glob("SingletonLocks.backup-*")
        for name, target in self.targets.items():
            self.assertFalse((self.data / name).is_symlink())
            self.assertEqual(os.readlink(backup / name), target)
        transfer.recover_stale_browser_locks(self.data)
        self.assertEqual(list(self.data.glob("SingletonLocks.backup-*")), [backup])
        self.assertEqual((self.data / "Cookies").read_bytes(), b"saved login")

    def test_running_browser_or_unreadable_process_inventory_prevents_recovery(self):
        self.no_browser.return_value = False
        transfer.recover_stale_browser_locks(self.data)
        self.assert_untouched()

    def test_existing_socket_directory_prevents_recovery(self):
        self.socket_dir.mkdir()
        transfer.recover_stale_browser_locks(self.data)
        self.assert_untouched()

    def test_same_host_and_malformed_locks_are_not_recovered(self):
        lock = self.data / "SingletonLock"
        for target in (f"current-studio-{os.getpid()}", "unknown", "previous-studio-0"):
            lock.unlink()
            lock.symlink_to(target)
            transfer.recover_stale_browser_locks(self.data)
            self.assertEqual(os.readlink(lock), target)
            self.assertFalse(list(self.data.glob("SingletonLocks.backup-*")))

    def test_changed_lock_is_not_moved(self):
        def browser_started():
            lock = self.data / "SingletonLock"
            lock.unlink()
            lock.symlink_to("current-studio-12345")
            return True
        self.no_browser.side_effect = browser_started
        transfer.recover_stale_browser_locks(self.data)
        self.assertEqual(os.readlink(self.data / "SingletonLock"), "current-studio-12345")
        self.assertFalse(list(self.data.glob("SingletonLocks.backup-*")))


class TransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-transfer-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.brave = self.root / "brave"
        self.brave.mkdir()
        self.profiles = self.root / "profiles"
        self.local = self.profiles / "acc-5" / "src"
        self.original = {
            "os_crypt": {"test_setting": "retain-browser-global-settings"},
            "profile": {
                "info_cache": {"Default": {"name": "Personal"}, "Profile 2": {"name": "AI-acc5"}},
                "last_used": "Profile 2",
                "last_active_profiles": ["Default", "Profile 2"],
                "profiles_order": ["Default", "Profile 2"],
            },
        }
        self.write_state(self.brave, self.original)
        for folder in ["Default", "Profile 2"]:
            (self.brave / folder).mkdir()
            (self.brave / folder / "Preferences").write_text(f"test data for {folder}")
        (self.brave / "Profile 2" / "Cookies").write_bytes(b"test cookie database")

    def write_state(self, root, state):
        (root / "Local State").write_text(json.dumps(state))

    def run_transfer(self, direction="import", **options):
        return transfer.transfer_profile(direction, "AI-acc5", "acc-5", brave_dir=self.brave,
                                         profiles_dir=self.profiles, **options)

    def test_move_round_trip_preserves_data_and_other_profiles(self):
        result = self.run_transfer()
        self.assertEqual(result["operation"], "move")
        self.assertEqual(result["verified_files"], 2)
        self.assertFalse((self.brave / "Profile 2").exists())
        self.assertEqual((self.local / "Profile 2" / "Cookies").read_bytes(), b"test cookie database")
        local_state = transfer.read_state(self.local)
        self.assertEqual(set(local_state["profile"]["info_cache"]), {"Profile 2"})
        self.assertEqual(local_state["os_crypt"], self.original["os_crypt"])
        self.assertEqual(transfer.read_state(self.brave)["profile"]["last_used"], "Default")
        self.run_transfer("export")
        self.assertFalse((self.local / "Profile 2").exists())
        self.assertEqual((self.brave / "Profile 2" / "Cookies").read_bytes(), b"test cookie database")
        self.assertEqual(transfer.read_state(self.brave), self.original)
        self.assertEqual((self.brave / "Default" / "Preferences").read_text(), "test data for Default")

    def test_copy_preserves_original_files_and_metadata(self):
        original = (self.brave / "Local State").read_bytes()
        self.run_transfer(copy=True)
        self.assertTrue((self.brave / "Profile 2" / "Cookies").is_file())
        self.assertEqual((self.brave / "Local State").read_bytes(), original)
        self.assertFalse(list(self.brave.glob("*.backup-*")))

    def test_duplicate_destination_requires_replace_and_replacement_is_backed_up(self):
        self.run_transfer(copy=True)
        (self.local / "Profile 2" / "Cookies").write_bytes(b"existing destination")
        with self.assertRaisesRegex(FileExistsError, "--replace"):
            self.run_transfer()
        self.assertTrue((self.brave / "Profile 2").is_dir())
        result = self.run_transfer(replace=True)
        backup = Path(result["replaced_profile_backup"])
        self.assertEqual((backup / "Cookies").read_bytes(), b"existing destination")
        self.assertEqual((self.local / "Profile 2" / "Cookies").read_bytes(), b"test cookie database")

    def test_export_avoids_existing_internal_folder_and_preserves_global_settings(self):
        self.run_transfer()
        state = transfer.read_state(self.brave)
        state["profile"]["info_cache"]["Profile 2"] = {"name": "Different account"}
        state["os_crypt"] = {"test_setting": "destination settings"}
        self.write_state(self.brave, state)
        (self.brave / "Profile 2").mkdir()
        (self.brave / "Profile 2" / "keep").write_text("unrelated")
        result = self.run_transfer("export")
        self.assertNotEqual(Path(result["destination"]).name, "Profile 2")
        self.assertEqual((self.brave / "Profile 2" / "keep").read_text(), "unrelated")
        updated = transfer.read_state(self.brave)
        self.assertEqual(updated["os_crypt"], state["os_crypt"])
        self.assertEqual(updated["profile"]["info_cache"]["Profile 2"]["name"], "Different account")

    def test_dry_run_does_not_create_destination(self):
        result = self.run_transfer(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.profiles.exists())
        self.assertTrue((self.brave / "Profile 2").is_dir())

    def test_running_browser_blocks_transfer(self):
        (self.brave / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
        with self.assertRaisesRegex(RuntimeError, "Brave is running"):
            self.run_transfer()
        self.assertFalse(self.profiles.exists())

    def test_path_traversal_and_ambiguous_display_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "folder name"):
            transfer.transfer_profile("import", "AI-acc5", "../escape", brave_dir=self.brave,
                                      profiles_dir=self.profiles)
        duplicate = deepcopy(self.original)
        duplicate["profile"]["info_cache"]["Profile 3"] = {"name": "AI-acc5"}
        self.write_state(self.brave, duplicate)
        with self.assertRaisesRegex(ValueError, "Multiple Brave profiles"):
            self.run_transfer()

    def test_copy_verification_failure_keeps_original(self):
        with patch.object(transfer, "verify_copy", side_effect=RuntimeError("checksum failed")):
            with self.assertRaisesRegex(RuntimeError, "checksum failed"):
                self.run_transfer()
        self.assertTrue((self.brave / "Profile 2" / "Cookies").is_file())
        self.assertEqual(transfer.read_state(self.brave), self.original)
        self.assertFalse((self.local / "Profile 2").exists())

    def test_write_failure_rolls_back_both_sides(self):
        write = transfer.atomic_write
        failed = False

        def fail_destination_once(path, data):
            nonlocal failed
            if path == self.local / "Local State" and not failed:
                failed = True
                raise OSError("simulated disk failure")
            write(path, data)

        with patch.object(transfer, "atomic_write", side_effect=fail_destination_once):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                self.run_transfer()
        self.assertTrue((self.brave / "Profile 2" / "Cookies").is_file())
        self.assertEqual(transfer.read_state(self.brave), self.original)
        self.assertFalse((self.local / "Profile 2").exists())
        self.assertFalse((self.local / "Local State").exists())


if __name__ == "__main__":
    unittest.main()
