"""Exercise the namespace launcher with local fake browsers and real pipe I/O."""

import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from zepuppeteer.vpn_browser import vpn_executable


@unittest.skipIf(os.geteuid() == 0, "Browser helpers intentionally reject root")
class VPNBrowserWrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-wrapper-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.route = SimpleNamespace(prefix=[], browser_args=["--route-marker"])

    def browser(self, source):
        path = self.root / "fake browser"
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o700)
        return str(path)

    def test_native_browser_receives_route_arguments_and_original_environment(self):
        result = self.root / "result.json"
        browser = self.browser(
            "import json, os, pathlib, sys\n"
            f"pathlib.Path({str(result)!r}).write_text(json.dumps({{'args':sys.argv[1:], 'display':os.environ.get('DISPLAY'), 'uid':os.getuid()}}))\n")
        sanitizer = self.root / "sudo environment sanitizer"
        sanitizer.write_text(f"#!{sys.executable}\nimport os,sys\nos.execve(sys.argv[1],sys.argv[1:],{{}})\n")
        sanitizer.chmod(0o700)
        self.route.prefix = [str(sanitizer)]
        with vpn_executable(browser, self.route) as wrapper:
            process = subprocess.run([wrapper, "--new-window", "argument with spaces"],
                                     env={**os.environ, "DISPLAY": ":77"}, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        data = json.loads(result.read_text())
        self.assertEqual(data["args"], ["--new-window", "argument with spaces", "--route-marker"])
        self.assertEqual(data["display"], ":77")
        self.assertEqual(data["uid"], os.getuid())

    def test_native_browser_status_is_returned(self):
        browser = self.browser("raise SystemExit(7)\n")
        with vpn_executable(browser, self.route) as wrapper:
            process = subprocess.run([wrapper], capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 7)

    def test_namespace_prefix_failure_never_retries_browser_directly(self):
        started = self.root / "unexpected"
        browser = self.browser(f"from pathlib import Path\nPath({str(started)!r}).touch()\n")
        prefix = self.root / "reject namespace"
        prefix.write_text(f"#!{sys.executable}\nraise SystemExit(9)\n")
        prefix.chmod(0o700)
        self.route.prefix = [str(prefix)]
        with vpn_executable(browser, self.route) as wrapper:
            process = subprocess.run([wrapper], capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 9)
        self.assertFalse(started.exists())

    def test_launch_files_are_private_and_removed_after_session(self):
        browser = self.browser("pass\n")
        with vpn_executable(browser, self.route) as wrapper:
            directory = Path(wrapper).parent
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((directory / "launch.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(wrapper).stat().st_mode), 0o700)
        self.assertFalse(directory.exists())

    def test_helper_rejects_incomplete_privilege_drop(self):
        started = self.root / "unexpected"
        browser = self.browser(f"from pathlib import Path\nPath({str(started)!r}).touch()\n")
        with vpn_executable(browser, self.route) as wrapper:
            config = Path(wrapper).parent / "launch.json"
            data = json.loads(config.read_text())
            data["uid"] = os.getuid() + 1
            config.write_text(json.dumps(data))
            process = subprocess.run([wrapper], capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertIn(b"privilege drop failed", process.stderr)
        self.assertFalse(started.exists())

    def test_playwright_pipe_bytes_survive_descriptor_closure_in_prefix(self):
        browser = self.browser("import os\ndata=os.read(3, 8192)\nos.write(4, data)\n")
        # Simulate sudo closing every inherited nonstandard fd before exec.
        prefix = self.root / "close descriptors"
        prefix.write_text(f"#!{sys.executable}\nimport os, sys\nos.closerange(3, 256)\nos.execv(sys.argv[1],sys.argv[1:])\n")
        prefix.chmod(0o700)
        self.route.prefix = [str(prefix)]
        incoming, send = os.pipe()
        receive, outgoing = os.pipe()
        process = None
        try:
            with vpn_executable(browser, self.route) as wrapper:
                shim = (
                    "import fcntl,os,sys; "
                    "a=fcntl.fcntl(int(sys.argv[2]),fcntl.F_DUPFD_CLOEXEC,10); "
                    "b=fcntl.fcntl(int(sys.argv[3]),fcntl.F_DUPFD_CLOEXEC,10); "
                    "os.dup2(a,3,inheritable=True); os.dup2(b,4,inheritable=True); "
                    "os.close(a); os.close(b); "
                    "os.execv(sys.argv[1],[sys.argv[1],'--remote-debugging-pipe'])")
                process = subprocess.Popen([sys.executable, "-c", shim, wrapper, str(incoming), str(outgoing)],
                                           pass_fds=(incoming, outgoing), stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                os.close(incoming)
                incoming = None
                os.close(outgoing)
                outgoing = None
                message = b'{"id":1,"method":"Browser.getVersion"}\x00binary\xff\x00'
                os.write(send, message)
                os.close(send)
                send = None
                ready, _, _ = select.select([receive], [], [], 5)
                self.assertTrue(ready, "Pipe bridge did not respond")
                self.assertEqual(os.read(receive, 8192), message)
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
            for descriptor in (incoming, send, receive, outgoing):
                if descriptor is not None:
                    os.close(descriptor)

    def test_debugging_pipe_missing_descriptors_fails_before_starting_browser(self):
        started = self.root / "unexpected"
        browser = self.browser(f"from pathlib import Path\nPath({str(started)!r}).touch()\n")
        with vpn_executable(browser, self.route) as wrapper:
            process = subprocess.run([wrapper, "--remote-debugging-pipe"], capture_output=True,
                                     close_fds=True, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(started.exists())

    def test_termination_stops_the_user_browser_process_group(self):
        marker = self.root / "pid"
        browser = self.browser(
            "import os,time\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(60)\n")
        with vpn_executable(browser, self.route) as wrapper:
            process = subprocess.Popen([wrapper], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), "Browser did not start")
                browser_pid = int(marker.read_text())
                process.terminate()
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, error)
                with self.assertRaises(ProcessLookupError):
                    os.kill(browser_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
