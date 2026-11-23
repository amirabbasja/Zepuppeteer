"""VPN startup and fail-closed checks use local mocks; no live VPN is changed."""

import asyncio
import base64
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zepuppeteer.models import NeedsAttention, Profile, TaskContext
from zepuppeteer.tasks.colab.open_notebook import run
from zepuppeteer.vpn import VPNConnection, connect_vpn, _command, _wireguard_outbound


class ColabVPNGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "profile.toml"
        self.write_manifest("true")
        self.page = SimpleNamespace(title=AsyncMock(return_value="test.ipynb - Colab"), close=AsyncMock(),
                                    click_target=AsyncMock(), click_text=AsyncMock(), screenshot=AsyncMock())
        self.browser = SimpleNamespace(new_page=AsyncMock(return_value=self.page))
        self.connection = SimpleNamespace(profile_name="test", heartbeat=AsyncMock())
        self.ctx = TaskContext(self.browser, Profile("test", self.root, manifest_path=self.manifest),
                               {}, {}, self.root, "run", "open", vpn=self.connection)
        self.site = SimpleNamespace(open_notebook=AsyncMock(return_value={}),
                                    run_all_and_wait_for_last_cell=AsyncMock(return_value="last"))
        for name, replacement in (
            ("ColabSite", Mock(return_value=self.site)),
            ("check_login", AsyncMock(return_value="logged_in")),
            ("check_runtime_connection", AsyncMock(side_effect=["disconnected", "connected"])),
            ("asyncio.sleep", AsyncMock()),
        ):
            mock = patch(f"zepuppeteer.tasks.colab.open_notebook.{name}", replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def write_manifest(self, use_vpn):
        self.manifest.write_text(
            '[profile.platforms.colab]\nlogin_username="test@example.test"\n'
            'notebook_url="https://colab.research.google.com/drive/test"\n'
            'notebook_name="test.ipynb"\nauto_connect=true\n'
            + (f'use_vpn={use_vpn}\n' if use_vpn is not None else ""))

    async def test_connected_vpn_is_checked_before_tab_and_before_run_all(self):
        events = []
        self.connection.heartbeat.side_effect = lambda: events.append("heartbeat")
        self.browser.new_page.side_effect = lambda: (events.append("page"), self.page)[1]
        self.site.run_all_and_wait_for_last_cell.side_effect = lambda **kwargs: (events.append("run"), "last")[1]
        result = await run(self.ctx)
        self.assertEqual(result.status, "success")
        self.assertEqual(events, ["heartbeat", "page", "heartbeat", "run"])

    async def test_missing_connection_or_failed_heartbeat_never_opens_colab(self):
        for connection in (None, SimpleNamespace(profile_name="other"),
                           SimpleNamespace(profile_name="test", heartbeat=AsyncMock(
                               side_effect=NeedsAttention("VPN has no heartbeat")))):
            with self.subTest(connection=connection), self.assertRaises(NeedsAttention):
                await run(replace(self.ctx, vpn=connection))
        self.browser.new_page.assert_not_awaited()
        self.site.open_notebook.assert_not_awaited()

    async def test_lost_heartbeat_prevents_run_all(self):
        self.connection.heartbeat.side_effect = [None, NeedsAttention("VPN has no heartbeat")]
        with self.assertRaisesRegex(NeedsAttention, "heartbeat"):
            await run(self.ctx)
        self.site.run_all_and_wait_for_last_cell.assert_not_awaited()

    async def test_false_or_absent_flag_does_not_require_vpn(self):
        for flag in ("false", None):
            self.write_manifest(flag)
            with patch("zepuppeteer.tasks.colab.open_notebook.check_runtime_connection",
                       AsyncMock(side_effect=["disconnected", "connected"])):
                self.assertEqual((await run(replace(self.ctx, vpn=None))).status, "success")
        self.connection.heartbeat.assert_not_awaited()

    async def test_non_boolean_flag_is_rejected_before_tab(self):
        self.write_manifest('"false"')
        with self.assertRaisesRegex(ValueError, "use_vpn must be a boolean"):
            await run(self.ctx)
        self.browser.new_page.assert_not_awaited()


class VPNHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_without_transport_never_probes_directly(self):
        with patch("zepuppeteer.vpn.vpn._probe", AsyncMock()) as probe:
            with self.assertRaises(NeedsAttention):
                await VPNConnection("test", "/curl").heartbeat()
        probe.assert_not_awaited()

    async def test_lost_heartbeat_cancels_active_browser_work(self):
        connection = VPNConnection("test", "/curl")
        cancelled = asyncio.Event()

        async def browser_work():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(connection, "heartbeat", AsyncMock(side_effect=NeedsAttention("no heartbeat"))):
            with self.assertRaisesRegex(NeedsAttention, "heartbeat"):
                await connection.run(browser_work(), heartbeat_interval=0.001)
        self.assertTrue(cancelled.is_set())

    async def test_dead_proxy_or_failed_https_probe_requires_attention(self):
        for code, probe_error in ((1, None), (None, RuntimeError("secret credentials"))):
            connection = VPNConnection("test", "/curl", process=SimpleNamespace(returncode=code),
                                       proxy={"server": "http://127.0.0.1:1", "username": "u", "password": "p"})
            with patch("zepuppeteer.vpn.vpn._probe", AsyncMock(side_effect=probe_error)) as probe:
                with self.assertRaisesRegex(NeedsAttention, "no heartbeat") as error:
                    await connection.heartbeat()
                self.assertNotIn("secret", str(error.exception))
                if code is not None:
                    probe.assert_not_awaited()

    async def test_heartbeat_uses_authenticated_proxy_and_never_host_interface(self):
        process = SimpleNamespace(returncode=None)
        proxy = {"server": "http://127.0.0.1:1234", "username": "user", "password": "password"}
        connection = VPNConnection("test", "/curl", process=process, proxy=proxy)
        with patch("zepuppeteer.vpn.vpn._probe", AsyncMock(return_value=10)) as probe:
            await connection.heartbeat()
        self.assertEqual(probe.call_args.kwargs["proxy"],
                         ("http://127.0.0.1:1234", "user:password"))
        self.assertIsNone(probe.call_args.kwargs.get("interface"))
        self.assertFalse(probe.call_args.kwargs.get("prefix"))

    async def test_proxy_without_live_process_and_process_without_proxy_never_probe(self):
        for kwargs in ({"proxy": {"server": "http://127.0.0.1:1"}},
                       {"process": SimpleNamespace(returncode=None)}):
            with self.subTest(kwargs=kwargs), patch("zepuppeteer.vpn.vpn._probe", AsyncMock()) as probe:
                with self.assertRaises(NeedsAttention):
                    await VPNConnection("test", "/curl", **kwargs).heartbeat()
                probe.assert_not_awaited()

    async def test_proxy_exit_during_https_probe_requires_attention(self):
        process = SimpleNamespace(returncode=None)
        connection = VPNConnection("test", "/curl", process=process,
                                   proxy={"server": "http://127.0.0.1:1", "username": "u", "password": "p"})

        async def probe(*args, **kwargs):
            process.returncode = 1
            return 10

        with patch("zepuppeteer.vpn.vpn._probe", AsyncMock(side_effect=probe)):
            with self.assertRaisesRegex(NeedsAttention, "disconnected"):
                await connection.heartbeat()


class WireGuardOutboundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "wg.conf"
        self.private_key = base64.b64encode(bytes(range(32))).decode()
        self.public_key = base64.b64encode(bytes(range(32, 64))).decode()
        self.preshared_key = base64.b64encode(bytes(range(64, 96))).decode()

    def write_config(self, *, dual_stack=False, dns=None, endpoint="127.0.0.1:51820", extra=""):
        self.path.write_text(
            f"[Interface]\nPrivateKey = {self.private_key}\n"
            + ("Address = 10.0.0.2/32, fd00::2/128\n" if dual_stack else "Address = 10.0.0.2/32\n")
            + (f"DNS = {dns}\n" if dns else "")
            + "MTU = 1330\nTable = off\nPostUp = never-run-this-hook\n"
            + f"[Peer]\nPublicKey = {self.public_key}\nPresharedKey = {self.preshared_key}\n"
            + f"Endpoint = {endpoint}\nPersistentKeepalive = 25\n"
            + ("AllowedIPs = 0.0.0.0/0, ::/0\n" if dual_stack else "AllowedIPs = 0.0.0.0/0\n")
            + extra)

    async def test_converts_provider_config_and_routes_dns_inside_userspace_tunnel(self):
        self.write_config(dual_stack=True, dns="8.8.8.8, 2001:4860:4860::8888",
                          endpoint="[2001:db8::1]:51820")
        original = self.path.read_bytes()
        outbound, secrets = await _wireguard_outbound(self.path)
        self.assertEqual(outbound["protocol"], "wireguard")
        settings = outbound["settings"]
        self.assertIs(settings["noKernelTun"], True)
        self.assertEqual(settings["secretKey"], self.private_key)
        self.assertEqual(settings["address"], ["10.0.0.2", "fd00::2"])
        self.assertEqual(settings["mtu"], 1330)
        self.assertEqual(settings["remoteDNS"], ["8.8.8.8", "2001:4860:4860::8888"])
        self.assertEqual(settings["peers"], [{
            "publicKey": self.public_key, "preSharedKey": self.preshared_key,
            "endpoint": "[2001:db8::1]:51820", "keepAlive": 25,
            "allowedIPs": ["0.0.0.0/0", "::/0"],
        }])
        self.assertTrue({self.private_key, self.public_key, self.preshared_key}.issubset(secrets))
        self.assertNotIn("never-run-this-hook", json.dumps(outbound))
        self.assertEqual(self.path.read_bytes(), original)

    async def test_ipv4_only_provider_config_is_accepted_with_tunnel_dns_defaults(self):
        self.write_config()
        outbound, _ = await _wireguard_outbound(self.path)
        self.assertEqual(outbound["settings"]["address"], ["10.0.0.2"])
        self.assertEqual(outbound["settings"]["peers"][0]["allowedIPs"], ["0.0.0.0/0"])
        self.assertEqual(outbound["settings"]["remoteDNS"], ["1.1.1.1", "1.0.0.1"])
        self.assertIs(outbound["settings"]["noKernelTun"], True)

    async def test_repeated_allowed_ips_lines_preserve_both_address_families(self):
        self.write_config(extra="AllowedIPs = ::/0\n")
        outbound, _ = await _wireguard_outbound(self.path)
        self.assertEqual(outbound["settings"]["peers"][0]["allowedIPs"], ["0.0.0.0/0", "::/0"])

    async def test_explicit_keepalive_off_is_accepted(self):
        for value in ("off", "OFF"):
            with self.subTest(value=value):
                self.write_config()
                self.path.write_text(self.path.read_text().replace("PersistentKeepalive = 25",
                                                                 f"PersistentKeepalive = {value}"))
                outbound, _ = await _wireguard_outbound(self.path)
                self.assertEqual(outbound["settings"]["peers"][0]["keepAlive"], 0)

    async def test_incomplete_peer_is_rejected_instead_of_using_xray_defaults(self):
        fields = {"PublicKey": self.public_key, "Endpoint": "127.0.0.1:51821",
                  "AllowedIPs": "192.0.2.0/24"}
        for missing in fields:
            with self.subTest(missing=missing):
                self.write_config(extra="[Peer]\n" + "".join(
                    f"{key} = {value}\n" for key, value in fields.items() if key != missing))
                with self.assertRaisesRegex(RuntimeError, "Invalid WireGuard configuration") as error:
                    await _wireguard_outbound(self.path)
                self.assertNotIn(self.private_key, str(error.exception))
                self.assertNotIn(self.public_key, str(error.exception))

    async def test_endpoint_hostname_is_resolved_before_proxy_startup(self):
        self.write_config(endpoint="provider.example.test:51820")
        with patch("zepuppeteer.vpn.vpn._resolve_wireguard_endpoint",
                   AsyncMock(return_value="192.0.2.1")) as resolve:
            outbound, _ = await _wireguard_outbound(self.path)
        resolve.assert_awaited_once_with("provider.example.test", 51820, 15)
        self.assertEqual(outbound["settings"]["peers"][0]["endpoint"], "192.0.2.1:51820")

    async def test_invalid_key_and_endpoint_do_not_disclose_config_secrets(self):
        for old, invalid in ((self.private_key, "invalid-private-secret"),
                             ("127.0.0.1:51820", "user:private-secret@provider.example.test:51820")):
            with self.subTest(invalid=invalid):
                self.write_config()
                self.path.write_text(self.path.read_text().replace(old, invalid))
                with self.assertRaises((ValueError, RuntimeError)) as error:
                    await _wireguard_outbound(self.path)
                self.assertNotIn("private-secret", str(error.exception))
                self.assertNotIn(self.private_key, str(error.exception))


class VPNLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "profile.toml"
        self.profile = Profile("test", self.root, manifest_path=self.manifest)

    def wireguard_profile(self):
        self.manifest.write_text('[profile.vpn]\nvpn_protocol="wireguard"\nwireguard_file="wg.conf"\n')
        key = base64.b64encode(bytes(range(32))).decode()
        public_key = base64.b64encode(bytes(range(32, 64))).decode()
        (self.root / "wg.conf").write_text(
            f"[Interface]\nPrivateKey = {key}\nAddress = 10.0.0.2/32\n"
            "DNS = 8.8.8.8\nMTU = 1330\nPostUp = never-run-this-hook\n"
            + f"[Peer]\nPublicKey = {public_key}\nEndpoint = 127.0.0.1:51820\n"
            "AllowedIPs = 0.0.0.0/0\n")

    @contextmanager
    def proxy_runtime(self, *, code=None, probe_error=None, validation_error=None):
        process = SimpleNamespace(returncode=code)
        writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        paths, configs, required, redactions = [], [], [], []

        async def validate(command, **kwargs):
            self.assertEqual(command[:3], ["/xray", "run", "-test"])
            path = Path(command[-1])
            paths.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            config = json.loads(path.read_text())
            configs.append(config)
            redactions.append(kwargs.get("redact", ()))
            if validation_error is not None:
                raise validation_error
            return 0, ""

        def executable(name):
            required.append(name)
            self.assertIn(name, ("xray", "curl"), "Browser VPN must not require host networking tools")
            return "/" + name

        with ExitStack() as stack:
            listener = stack.enter_context(patch("zepuppeteer.vpn.socket.socket"))
            listener.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 1234)
            stack.enter_context(patch("zepuppeteer.vpn.vpn._required", side_effect=executable))
            commands = stack.enter_context(patch("zepuppeteer.vpn.vpn._command", AsyncMock(side_effect=validate)))
            launch = stack.enter_context(patch("zepuppeteer.vpn.asyncio.create_subprocess_exec",
                                               AsyncMock(return_value=process)))
            opening = stack.enter_context(patch("zepuppeteer.vpn.asyncio.open_connection",
                                                AsyncMock(return_value=(None, writer))))
            stop = stack.enter_context(patch("zepuppeteer.vpn.vpn._stop", AsyncMock()))
            probe = stack.enter_context(patch("zepuppeteer.vpn.vpn._probe",
                                              AsyncMock(return_value=10, side_effect=probe_error)))
            privileges = stack.enter_context(patch("zepuppeteer.vpn.vpn._wireguard_privileges", AsyncMock()))
            yield SimpleNamespace(process=process, paths=paths, configs=configs, commands=commands,
                                  launch=launch, opening=opening, stop=stop, probe=probe,
                                  privileges=privileges, required=required, redactions=redactions)

    async def test_wireguard_uses_authenticated_loopback_proxy_without_host_network_changes(self):
        self.wireguard_profile()
        original = (self.root / "wg.conf").read_bytes()
        with self.proxy_runtime() as runtime:
            async with connect_vpn(self.profile) as connection:
                self.assertEqual(connection.proxy["server"], "http://127.0.0.1:1234")
                self.assertTrue(connection.proxy["username"])
                self.assertTrue(connection.proxy["password"])
                self.assertIs(connection.process, runtime.process)
                self.assertTrue(runtime.paths[0].exists())
                inbound = runtime.configs[0]["inbounds"][0]
                self.assertEqual(inbound["listen"], "127.0.0.1")
                self.assertEqual(inbound["protocol"], "http")
                accounts = inbound["settings"].get("accounts", inbound["settings"].get("users"))
                self.assertEqual(accounts, [{"user": connection.proxy["username"],
                                             "pass": connection.proxy["password"]}])
                outbounds = runtime.configs[0]["outbounds"]
                self.assertEqual(len(outbounds), 1)
                self.assertEqual(outbounds[0]["protocol"], "wireguard")
                self.assertIs(outbounds[0]["settings"]["noKernelTun"], True)
                self.assertEqual(outbounds[0]["settings"]["remoteDNS"], ["8.8.8.8"])
                self.assertIn(connection.proxy["password"], runtime.redactions[0])
                self.assertIn(outbounds[0]["settings"]["secretKey"], runtime.redactions[0])
                self.assertEqual(runtime.probe.call_args.kwargs["proxy"],
                                 (connection.proxy["server"],
                                  f'{connection.proxy["username"]}:{connection.proxy["password"]}'))
                runtime.stop.assert_not_awaited()
            runtime.stop.assert_awaited_once_with(runtime.process)
            runtime.privileges.assert_not_awaited()
            self.assertEqual(set(runtime.required), {"curl", "xray"})
            self.assertTrue(all(call.args[0][0] == "/xray" for call in runtime.commands.call_args_list))
        self.assertFalse(runtime.paths[0].exists())
        self.assertEqual((self.root / "wg.conf").read_bytes(), original)

    async def test_wireguard_proxy_is_cleaned_after_work_failure_and_cancellation(self):
        self.wireguard_profile()
        for failure in (RuntimeError("notebook failure"), asyncio.CancelledError()):
            with self.subTest(failure=failure), self.proxy_runtime() as runtime:
                with self.assertRaises(type(failure)) as raised:
                    async with connect_vpn(self.profile):
                        raise failure
                self.assertIs(raised.exception, failure)
                runtime.stop.assert_awaited_once_with(runtime.process)
                self.assertFalse(runtime.paths[0].exists())

    async def test_failed_initial_heartbeat_stops_proxy_and_keeps_diagnostics_secret(self):
        self.wireguard_profile()
        with self.proxy_runtime(probe_error=RuntimeError("secret private credentials")) as runtime:
            with self.assertRaisesRegex(NeedsAttention, "heartbeat") as error:
                async with connect_vpn(self.profile):
                    self.fail("Missing heartbeat must prevent browser launch")
            self.assertNotIn("secret", str(error.exception))
            runtime.stop.assert_awaited_once_with(runtime.process)
            self.assertFalse(runtime.paths[0].exists())

    async def test_proxy_exit_and_startup_cancellation_clean_temporary_config(self):
        self.wireguard_profile()
        with self.proxy_runtime(code=1) as runtime:
            with self.assertRaises(NeedsAttention):
                async with connect_vpn(self.profile):
                    self.fail("An exited proxy must prevent browser launch")
            runtime.stop.assert_awaited_once_with(runtime.process)
            self.assertFalse(runtime.paths[0].exists())
        with self.proxy_runtime() as runtime:
            runtime.opening.side_effect = asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                async with connect_vpn(self.profile):
                    self.fail("Cancelled startup must prevent browser launch")
            runtime.stop.assert_awaited_once_with(runtime.process)
            self.assertFalse(runtime.paths[0].exists())

    async def test_cancellation_during_process_spawn_still_reaps_the_child(self):
        self.wireguard_profile()
        started, release = asyncio.Event(), asyncio.Event()
        with self.proxy_runtime() as runtime:
            async def spawn(*args, **kwargs):
                started.set()
                await release.wait()
                return runtime.process

            async def connect():
                async with connect_vpn(self.profile):
                    self.fail("Cancelled startup must prevent browser launch")

            runtime.launch.side_effect = spawn
            operation = asyncio.create_task(connect())
            try:
                await asyncio.wait_for(started.wait(), 1)
                operation.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
            finally:
                release.set()
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            runtime.stop.assert_awaited_once_with(runtime.process)
            self.assertFalse(runtime.paths[0].exists())

    async def test_invalid_proxy_config_never_launches_and_removes_private_file(self):
        self.wireguard_profile()
        with self.proxy_runtime(validation_error=RuntimeError("invalid configuration")) as runtime:
            with self.assertRaises(NeedsAttention):
                async with connect_vpn(self.profile):
                    self.fail("Invalid proxy config must prevent launch")
            runtime.launch.assert_not_awaited()
            self.assertFalse(runtime.paths[0].exists())

    async def test_startup_command_reports_stderr_with_keys_redacted(self):
        self.wireguard_profile()
        secret = base64.b64encode(bytes(range(32))).decode()

        @asynccontextmanager
        async def failing_tunnel(*args):
            await _command(
                [sys.executable, "-c", "import sys; sys.stderr.write(sys.argv[1]); sys.exit(1)",
                 f"WireGuard outbound: invalid configuration\nPrivateKey = {secret}"],
                label="Validate WireGuard proxy configuration", redact=(secret,))
            yield

        with patch("zepuppeteer.vpn.vpn._required", return_value="/curl"), \
                patch("zepuppeteer.vpn._wireguard", failing_tunnel):
            with self.assertRaises(NeedsAttention) as error:
                async with connect_vpn(self.profile):
                    self.fail("Failed command must prevent browser launch")
        self.assertIn("Validate WireGuard proxy configuration failed (exit 1)", str(error.exception))
        self.assertIn("WireGuard outbound: invalid configuration", str(error.exception))
        self.assertNotIn(secret, str(error.exception))
        self.assertIn("[redacted]", str(error.exception))

    async def test_connection_is_retained_until_scope_exits_and_failure_is_sanitized(self):
        self.manifest.write_text('[profile.vpn]\nvpn_protocol="wireguard"\nwireguard_file="wg.conf"\n')
        events = []
        connection = SimpleNamespace(heartbeat=AsyncMock())

        @asynccontextmanager
        async def tunnel(*args):
            events.append("connected")
            try:
                yield connection
            finally:
                events.append("disconnected")

        with patch("zepuppeteer.vpn._wireguard", tunnel), patch("zepuppeteer.vpn.vpn._required", return_value="/curl"):
            async with connect_vpn(self.profile) as actual:
                self.assertIs(actual, connection)
                connection.heartbeat.assert_awaited_once()
                self.assertEqual(events, ["connected"])
            self.assertEqual(events, ["connected", "disconnected"])
            connection.heartbeat.side_effect = NeedsAttention("no heartbeat")
            with self.assertRaisesRegex(NeedsAttention, "heartbeat"):
                async with connect_vpn(self.profile):
                    self.fail("Missing heartbeat must prevent browser launch")
            self.assertEqual(events[-2:], ["connected", "disconnected"])

        self.manifest.write_text('[profile.vpn]\nvpn_protocol="secret unsupported value"\n')
        with self.assertRaises(NeedsAttention) as error:
            async with connect_vpn(self.profile):
                self.fail("Invalid configuration must prevent browser launch")
        self.assertNotIn("secret", str(error.exception))

    async def test_v2ray_uses_same_proxy_lifecycle_and_cleans_on_cancellation(self):
        self.manifest.write_text('[profile.vpn]\nvpn_protocol="v2ray"\n'
                                 'v2ray_link="vless://test@vpn.example.test:443?security=tls"\n')
        with self.proxy_runtime() as runtime:
            with self.assertRaises(asyncio.CancelledError):
                async with connect_vpn(self.profile) as connection:
                    self.assertTrue(connection.proxy["server"].startswith("http://127.0.0.1:"))
                    self.assertEqual(runtime.configs[0]["outbounds"][0]["protocol"], "vless")
                    self.assertTrue(runtime.paths[0].exists())
                    self.assertIsNotNone(runtime.probe.call_args.kwargs["proxy"])
                    raise asyncio.CancelledError()
            runtime.stop.assert_awaited_once_with(runtime.process)
            self.assertFalse(runtime.paths[0].exists())
