"""Browser launchers must hold the selected VPN until the browser has exited."""

import asyncio
from contextlib import asynccontextmanager, contextmanager, redirect_stdout
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zepuppeteer.brave import profile_network
from zepuppeteer.models import NeedsAttention, Profile, Step, TaskResult, Workflow
from zepuppeteer.runner import run_profiles, run_workflow
from zepuppeteer.state import StateStore
from zepuppeteer.tasks.openProfile import openProfile


class BrowserVPNLaunchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-launch-vpn-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "account"
        (self.folder / "src").mkdir(parents=True)
        self.manifest = self.folder / "profile.toml"
        self.manifest.write_text('[profile]\nuse_vpn=true\n')
        self.profile = Profile("account", self.folder / "src", manifest_path=self.manifest)
        self.route = SimpleNamespace(check_connection=AsyncMock(return_value=10), browser_args=[])
        self.events = []
        self.browser = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: self.events.append("browser closed")),
            add_init_script=AsyncMock(), pages=[],
            tracing=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()))
        self.launch = AsyncMock(return_value=self.browser)
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=self.launch))
        self.state = StateStore(self.root / "state.sqlite")
        self.addCleanup(self.state.connection.close)
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch("zepuppeteer.runner.notify_attention", AsyncMock()))

    @asynccontextmanager
    async def network(self, profile, executable, *, platforms=None):
        self.events.append("VPN connected")
        self.platforms = platforms
        try:
            yield "/private/vpn-wrapper", self.route
        finally:
            self.events.append("VPN closed")

    async def workflow(self, task):
        return await run_workflow(self.playwright, [self.profile], "/brave",
            Workflow("test", (Step("work", "colab.work"),)), {"colab.work": task},
            self.state, self.root / "artifacts", headless=True, trace=False)

    async def test_workflow_uses_namespace_wrapper_and_closes_browser_before_vpn(self):
        async def task(ctx):
            self.assertIs(ctx.browser._zepuppeteer_vpn_route, self.route)
            self.assertEqual(self.events, ["VPN connected"])
            return TaskResult()
        with patch("zepuppeteer.runner.profile_network", self.network):
            result = await self.workflow(task)
        self.assertEqual(result.status, "success")
        self.assertEqual(self.platforms, {"colab"})
        self.assertEqual(self.launch.call_args.kwargs["executable_path"], "/private/vpn-wrapper")
        self.assertTrue(self.launch.call_args.kwargs["chromium_sandbox"])
        self.assertEqual(self.events, ["VPN connected", "browser closed", "VPN closed"])

    async def test_vpn_start_failure_never_launches_unprotected_browser(self):
        @asynccontextmanager
        async def unavailable(*args, **kwargs):
            raise NeedsAttention("VPN unavailable")
            yield
        task = AsyncMock(return_value=TaskResult())
        with patch("zepuppeteer.runner.profile_network", unavailable):
            result = await self.workflow(task)
        self.assertEqual(result.status, "failed")
        self.launch.assert_not_awaited()
        task.assert_not_awaited()

    async def test_browser_launch_failure_releases_vpn_without_retrying_direct(self):
        self.launch.side_effect = RuntimeError("Browser launch failed")
        with patch("zepuppeteer.runner.profile_network", self.network):
            result = await self.workflow(AsyncMock())
        self.assertEqual(result.status, "failed")
        self.launch.assert_awaited_once()
        self.assertEqual(self.events, ["VPN connected", "VPN closed"])

    async def test_cancellation_closes_browser_before_vpn(self):
        entered = asyncio.Event()
        async def task(ctx):
            entered.set()
            await asyncio.Event().wait()
        with patch("zepuppeteer.runner.profile_network", self.network):
            running = asyncio.create_task(self.workflow(task))
            await asyncio.wait_for(entered.wait(), 2)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertEqual(self.events, ["VPN connected", "browser closed", "VPN closed"])

    async def test_legacy_and_profile_setup_launches_use_the_same_vpn(self):
        async def task(context, profile):
            self.assertIs(context._zepuppeteer_vpn_route, self.route)
        self.browser.on = lambda *_: None
        with patch("zepuppeteer.runner.profile_network", self.network):
            await run_profiles(self.playwright, [self.profile], "/brave", task, headless=True)
        self.assertEqual(self.events, ["VPN connected", "browser closed", "VPN closed"])
        self.assertEqual(self.launch.call_args.kwargs["executable_path"], "/private/vpn-wrapper")

    async def test_manual_login_waits_for_browser_before_releasing_vpn(self):
        async def closed():
            self.events.append("browser closed")
            return 0
        process = SimpleNamespace(wait=AsyncMock(side_effect=closed))
        with patch("zepuppeteer.tasks.openProfile.profile_network", self.network), \
                patch("zepuppeteer.tasks.openProfile.find_brave", return_value="/brave"), \
                patch("zepuppeteer.tasks.openProfile.os.getuid", return_value=1000), \
                patch.dict("os.environ", {"DISPLAY": ":0"}), \
                patch("zepuppeteer.tasks.openProfile.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            await openProfile("account", profiles_dir=self.root)
        self.assertEqual(spawn.call_args.args[0], "/private/vpn-wrapper")
        self.assertEqual(self.events, ["VPN connected", "browser closed", "VPN closed"])

    async def test_running_browser_root_is_rejected_before_allocating_tunnel(self):
        with patch("zepuppeteer.brave.ensure_closed", side_effect=RuntimeError("Brave is running")), \
                patch("zepuppeteer.vpn.browser_vpn") as start:
            with self.assertRaisesRegex(NeedsAttention, "close all Brave windows"):
                async with profile_network(self.profile, "/brave"):
                    self.fail("A running non-VPN instance must never be reused")
        start.assert_not_called()

    async def test_foreign_host_lock_reports_actual_cause_before_allocating_tunnel(self):
        lock = self.profile.user_data_dir / "SingletonLock"
        lock.symlink_to("previous-studio-96984")
        with patch("zepuppeteer.profile_transfer.socket.gethostname", return_value="current-studio"), \
                patch("zepuppeteer.vpn.browser_vpn") as start:
            with self.assertRaises(NeedsAttention) as caught:
                async with profile_network(self.profile, "/brave"):
                    self.fail("An unknown lock owner must block VPN launch")
        self.assertIn("previous-studio-96984", str(caught.exception))
        self.assertIn("current host: current-studio", str(caught.exception))
        self.assertIn("verify Brave is closed there", str(caught.exception))
        self.assertEqual(lock.readlink(), Path("previous-studio-96984"))
        start.assert_not_called()

    async def test_orphaned_studio_locks_are_recovered_before_vpn_start(self):
        root = self.profile.user_data_dir
        (root / "SingletonLock").symlink_to("previous-studio-96984")
        (root / "SingletonSocket").symlink_to(self.root / "missing-tmp/SingletonSocket")

        @asynccontextmanager
        async def start(profile, *, platforms=None):
            self.assertFalse((root / "SingletonLock").is_symlink())
            self.events.append("VPN connected")
            yield None

        with patch("zepuppeteer.profile_transfer.socket.gethostname", return_value="current-studio"), \
                patch("zepuppeteer.profile_transfer._no_local_browser", return_value=True), \
                patch("zepuppeteer.vpn.browser_vpn", start):
            async with profile_network(self.profile, "/brave"):
                self.assertEqual(self.events, ["VPN connected"])

    async def test_network_context_keeps_wrapper_and_route_alive_until_exit(self):
        @asynccontextmanager
        async def start(profile, *, platforms=None):
            self.events.append("VPN connected")
            try:
                yield self.route
            finally:
                self.events.append("VPN closed")
        @contextmanager
        def wrapper(executable, route):
            self.events.append("wrapper created")
            try:
                yield "/private/vpn-wrapper"
            finally:
                self.events.append("wrapper removed")
        with patch("zepuppeteer.vpn.browser_vpn", start), \
                patch("zepuppeteer.vpn_browser.vpn_executable", wrapper):
            async with profile_network(self.profile, "/brave") as (executable, route):
                self.assertIs(route, self.route)
                self.assertEqual(executable, "/private/vpn-wrapper")
                self.assertEqual(self.events, ["VPN connected", "wrapper created"])
        self.assertEqual(self.events, ["VPN connected", "wrapper created", "wrapper removed", "VPN closed"])

    async def test_new_vpn_requirement_during_startup_still_rejects_running_browser(self):
        @asynccontextmanager
        async def start(profile, *, platforms=None):
            yield self.route
        with patch("zepuppeteer.vpn.vpn_required", return_value=False), \
                patch("zepuppeteer.vpn.browser_vpn", start), \
                patch("zepuppeteer.brave.ensure_closed", side_effect=RuntimeError("Brave is running")):
            with self.assertRaisesRegex(NeedsAttention, "close all Brave windows"):
                async with profile_network(self.profile, "/brave"):
                    self.fail("VPN routing may not reuse a browser after a configuration change")
