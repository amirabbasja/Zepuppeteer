"""Launch Brave inside a VPN namespace while retaining Playwright's pipe transport.

Sudo closes inherited descriptors, including Chromium's debugging fds 3 and 4.
The unprivileged supervisor relays those bytes through private FIFOs; the helper
opens the browser ends only after the namespace prefix has dropped privileges.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time


_START_TIMEOUT = 15
_STOP_TIMEOUT = 3
_MAX_BUFFER = 4 * 1024 * 1024


def _write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream)


@contextmanager
def vpn_executable(executable, route):
    """Yield a browser executable wrapper for the lifetime of one VPN session."""
    if os.geteuid() == 0:
        raise RuntimeError("Run the VPN browser as your normal desktop user, not root.")
    helper = str(Path(__file__).resolve())
    with tempfile.TemporaryDirectory(prefix="zepuppeteer-vpn-browser-") as temporary:
        directory = Path(temporary)
        config = directory / "launch.json"
        _write_private(config, {
            "executable": str(executable), "prefix": list(route.prefix),
            "browser_args": list(route.browser_args), "uid": os.getuid(), "gid": os.getgid(),
        })
        wrapper = directory / "brave-vpn"
        # Python paths and arguments are literals, never interpolated into a shell.
        wrapper.write_text(
            f"#!{sys.executable}\nimport runpy, sys\n"
            f"sys.argv = [{helper!r}, '--supervise', {str(config)!r}, '--', *sys.argv[1:]]\n"
            f"runpy.run_path({helper!r}, run_name='__main__')\n", encoding="utf-8")
        wrapper.chmod(0o700)
        yield str(wrapper)


def _child(config_path, arguments):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if (os.getuid() != config["uid"] or os.geteuid() != config["uid"]
            or os.getgid() != config["gid"] or os.getegid() != config["gid"]
            or config["uid"] == 0):
        raise RuntimeError("VPN browser privilege drop failed; refusing to start Brave.")
    # Give the user's browser a distinct process group that the supervisor can
    # terminate without attempting to signal any privileged sudo monitor.
    if os.getpgrp() != os.getpid():
        os.setpgid(0, 0)
    _write_private(config["pid_file"], {"pid": os.getpid()})
    if config["pipe"]:
        import fcntl
        # A parent that has already closed its CDP input must yield EOF, not
        # leave this helper blocked waiting for a FIFO writer during shutdown.
        source = os.open(config["input_fifo"], os.O_RDONLY | os.O_NONBLOCK)
        os.set_blocking(source, True)
        target = os.open(config["output_fifo"], os.O_WRONLY)
        # Duplicate above 4 first: opening one FIFO can itself have returned 3/4.
        incoming = fcntl.fcntl(source, fcntl.F_DUPFD_CLOEXEC, 10)
        outgoing = fcntl.fcntl(target, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(source)
        os.close(target)
        try:
            os.dup2(incoming, 3, inheritable=True)
            os.dup2(outgoing, 4, inheritable=True)
        finally:
            os.close(incoming)
            os.close(outgoing)
    _write_private(config["ready_file"], True)
    # Sudo deliberately sanitizes the environment. Restore the original user's
    # desktop/keyring/GTK environment only after confirming privilege drop.
    environment = config["environment"]
    os.execvpe(config["executable"],
              [config["executable"], *arguments, *config["browser_args"]], environment)


def _browser_pid(path):
    try:
        pid = json.loads(path.read_text(encoding="utf-8"))["pid"]
        return pid if isinstance(pid, int) and pid > 1 else None
    except (OSError, ValueError, KeyError):
        return None


def _signal_browser(pid, signum):
    if pid is None:
        return
    try:
        # Child establishes this group before reporting its PID. Rechecking
        # avoids accidentally signalling another process group on stale input.
        if os.getpgid(pid) == pid:
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)
    except ProcessLookupError:
        pass


def _exit_status(code):
    return code if code >= 0 else 128 - code


def _supervise(config_path, arguments):
    pipe = "--remote-debugging-pipe" in arguments
    if pipe:
        # Fail before creating files that might accidentally occupy absent 3/4.
        for descriptor in (3, 4):
            os.fstat(descriptor)
    original = json.loads(Path(config_path).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="session-", dir=Path(config_path).parent) as temporary:
        directory = Path(temporary)
        pid_file = directory / "browser.pid"
        config = {**original, "pipe": pipe, "environment": dict(os.environ),
                  "pid_file": str(pid_file), "ready_file": str(directory / "ready")}
        fifo_in = fifo_out = None
        if pipe:
            config.update(input_fifo=str(directory / "input"), output_fifo=str(directory / "output"))
            os.mkfifo(config["input_fifo"], 0o600)
            os.mkfifo(config["output_fifo"], 0o600)
            # RDWR opens cannot deadlock while sudo/helper starts. The input
            # endpoint is closed on Playwright EOF to deliver EOF to Chromium.
            fifo_in = os.open(config["input_fifo"], os.O_RDWR | os.O_NONBLOCK)
            fifo_out = os.open(config["output_fifo"], os.O_RDWR | os.O_NONBLOCK)
            os.set_blocking(3, False)
            os.set_blocking(4, False)
        runtime = directory / "runtime.json"
        _write_private(runtime, config)
        command = [*original["prefix"], sys.executable, str(Path(__file__).resolve()),
                   "--child", str(runtime), "--", *arguments]
        process = None
        received = []
        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[signum] = signal.signal(signum, lambda number, frame: received.append(number))
        selector = selectors.DefaultSelector()
        child_pid = None
        stop_at = None
        signalled = False
        to_child = bytearray()
        to_parent = bytearray()
        input_open = pipe
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, close_fds=True)
            while True:
                now = time.monotonic()
                child_pid = child_pid or _browser_pid(pid_file)
                if not child_pid and now - started > _START_TIMEOUT:
                    raise RuntimeError("Timed out starting Brave in the VPN namespace.")
                if received and not signalled and child_pid:
                    _signal_browser(child_pid, received[0])
                    stop_at = now + _STOP_TIMEOUT
                    signalled = True
                if stop_at is not None and now >= stop_at:
                    _signal_browser(child_pid, signal.SIGKILL)
                    # The owning namespace session also cleans up processes if
                    # a privileged sudo monitor fails to exit after its child.
                    try:
                        return _exit_status(process.wait(timeout=1))
                    except subprocess.TimeoutExpired:
                        return 128 + (received[0] if received else signal.SIGTERM)
                code = process.poll()
                if not pipe:
                    if code is not None:
                        return _exit_status(code)
                    time.sleep(0.05)
                    continue
                if (not input_open and not to_child and fifo_in is not None
                        and Path(config["ready_file"]).exists()):
                    os.close(fifo_in)
                    fifo_in = None
                # Rebuild a small selector set so each descriptor has exactly
                # the events corresponding to currently buffered data.
                for key in list(selector.get_map().values()):
                    selector.unregister(key.fd)
                if input_open and len(to_child) < _MAX_BUFFER and code is None:
                    selector.register(3, selectors.EVENT_READ, "parent")
                if fifo_in is not None and to_child and code is None:
                    selector.register(fifo_in, selectors.EVENT_WRITE, "child")
                if len(to_parent) < _MAX_BUFFER:
                    selector.register(fifo_out, selectors.EVENT_READ, "browser")
                if to_parent:
                    selector.register(4, selectors.EVENT_WRITE, "output")
                events = selector.select(0.05)
                browser_data = False
                for key, mask in events:
                    try:
                        if key.data == "parent":
                            data = os.read(3, 65536)
                            if data:
                                to_child.extend(data)
                            else:
                                input_open = False
                                stop_at = stop_at or now + _STOP_TIMEOUT
                        elif key.data == "child":
                            written = os.write(fifo_in, to_child)
                            del to_child[:written]
                        elif key.data == "browser":
                            data = os.read(fifo_out, 65536)
                            to_parent.extend(data)
                            browser_data = bool(data)
                        else:
                            written = os.write(4, to_parent)
                            del to_parent[:written]
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        received.append(signal.SIGTERM)
                        input_open = False
                        to_child.clear()
                        to_parent.clear()
                if code is not None and not browser_data and not to_parent:
                    return _exit_status(code)
        finally:
            if process is not None and process.poll() is None:
                child_pid = child_pid or _browser_pid(pid_file)
                _signal_browser(child_pid, signal.SIGTERM)
                try:
                    process.wait(timeout=_STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    _signal_browser(child_pid, signal.SIGKILL)
                    # sudo may still be awaiting its unprivileged helper during
                    # failed startup; terminate only when the OS permits it.
                    try:
                        process.terminate()
                        process.wait(timeout=1)
                    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                        pass
            selector.close()
            for descriptor in (fifo_in, fifo_out):
                if descriptor is not None:
                    os.close(descriptor)
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main():
    if len(sys.argv) < 4 or sys.argv[3] != "--" or sys.argv[1] not in {"--child", "--supervise"}:
        raise RuntimeError("Invalid VPN browser helper invocation.")
    if sys.argv[1] == "--child":
        _child(sys.argv[2], sys.argv[4:])
        return 1
    return _supervise(sys.argv[2], sys.argv[4:])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"VPN browser launch failed: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
