from pathlib import Path
import tempfile
import unittest
from zepuppeteer.brave import prepare_profile_for_launch
from zepuppeteer.models import Profile

class StartupTests(unittest.TestCase):
    def test_live_profile_is_never_edited(self):
        import socket
        import os
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
            with self.assertRaises(RuntimeError):
                prepare_profile_for_launch(Profile("one", root))
