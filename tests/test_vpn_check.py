import asyncio
import base64
import contextlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zepuppeteer.cli import main
from zepuppeteer.models import Profile, Step, TaskContext, TaskResult, Workflow
from zepuppeteer.tasks import vpnCheck as vpn
from zepuppeteer.workflows import resolve_tasks


TASK = "zepuppeteer.tasks.vpnCheck"
REQUIRE = vpn._required
LINK = "vless://test-id@vpn.example.test:443?type=ws&security=tls&sni=cdn.example.test&host=cdn.example.test&path=%2Fvpn"
PRIVATE_KEY = base64.b64encode(bytes(range(32))).decode()
PUBLIC_KEY = base64.b64encode(bytes(range(32, 64))).decode()
WG = """[Interface]
PrivateKey = PRIVATE_KEY_PLACEHOLDER
Address = 10.0.0.2/32, fd00::2/128
DNS = 1.1.1.1
PostUp = touch /tmp/never-run-this-hook
PostDown = echo never-run-this-either
ListenPort = 51820
FwMark = 1234
[Peer]
PublicKey = PUBLIC_KEY_PLACEHOLDER
Endpoint = 127.0.0.1:51820
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
""".replace("PRIVATE_KEY_PLACEHOLDER", PRIVATE_KEY).replace("PUBLIC_KEY_PLACEHOLDER", PUBLIC_KEY)


class VPNFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-test-vpn-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "account-1"
        self.folder.mkdir()
        self.manifest = self.folder / "profile.toml"
        self.config = self.folder / "tunnel.conf"
        self.config.write_text(WG)
        self.write_manifest()

    def write_manifest(self, protocol="wireguard", table="profile.vpn"):
        self.manifest.write_text(f'[{table}]\nvpn_protocol = "{protocol}"\nwireguard_file = "tunnel.conf"\n'
                                 f'v2ray_link = {json.dumps(LINK)}\n')


class VPNEntryTests(VPNFixture):
    async def test_named_profile_does_not_need_browser_data_and_resolves_relative_config(self):
        output = io.StringIO()
        with patch(f"{TASK}._wireguard", AsyncMock(return_value=(143.25, 27.5))) as check, \
                patch(f"{TASK}._required", return_value="/curl"), contextlib.redirect_stdout(output):
            result = await vpn.vpnCheck("account-1", profiles_dir=self.root)
        check.assert_awaited_once_with(self.config, "/curl", vpn.DEFAULT_URL, 15)
        self.assertEqual(result.outputs["ping_ms"], 27.5)
        self.assertTrue(result.outputs["connected"])
        self.assertIn("143.25 ms", output.getvalue())
        self.assertIn("27.50 ms", output.getvalue())
        self.assertNotIn(PRIVATE_KEY, output.getvalue())

    async def test_profile_keys_and_case_insensitive_protocol_are_supported(self):
        self.write_manifest("V2Ray", "profile")
        with patch(f"{TASK}._xray", AsyncMock(return_value=(123, None))) as check, \
                patch(f"{TASK}._required", return_value="/curl"), contextlib.redirect_stdout(io.StringIO()) as output:
            result = await vpn.vpnCheck("account-1", profiles_dir=self.root)
        check.assert_awaited_once_with(LINK, "/curl", vpn.DEFAULT_URL, 15)
        self.assertIsNone(result.outputs["ping_ms"])
        self.assertIn("ICMP is not carried", output.getvalue())

    async def test_top_level_vpn_settings_coexist_with_browser_metadata(self):
        self.manifest.write_text('vpn_protocol="v2ray"\nv2ray_link=' + json.dumps(LINK)
                                 + '\n[profile]\nbrave_browser_displayName="Account 1"\n')
        with patch(f"{TASK}._xray", AsyncMock(return_value=(123, None))) as check, \
                patch(f"{TASK}._required", return_value="/curl"), contextlib.redirect_stdout(io.StringIO()):
            await vpn.vpnCheck("account-1", profiles_dir=self.root)
        check.assert_awaited_once()

    async def test_missing_curl_fails_without_starting_vpn(self):
        with patch(f"{TASK}.shutil.which", return_value=None), patch(f"{TASK}._wireguard") as tunnel:
            with self.assertRaisesRegex(RuntimeError, "curl"):
                await vpn.vpnCheck("account-1", profiles_dir=self.root)
        tunnel.assert_not_called()

    async def test_bad_profile_names_and_symlinks_fail_before_subprocesses(self):
        with patch(f"{TASK}._command") as command:
            for name in ("../account-1", "..", ".", "a/b", "a\\b", "missing", ""):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    await vpn.vpnCheck(name, profiles_dir=self.root)
            (self.root / "linked").symlink_to(self.folder, target_is_directory=True)
            with self.assertRaises(ValueError):
                await vpn.vpnCheck("linked", profiles_dir=self.root)
            command.assert_not_called()

    async def test_invalid_settings_fail_before_dependencies_or_system_changes(self):
        with patch(f"{TASK}._required") as dependency:
            for content in ('[profile]\nvpn_protocol="openvpn"', '[profile]\nvpn_protocol=2',
                            'profile=[]', '[profile]\nvpn=[]'):
                self.manifest.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    await vpn.vpnCheck("account-1", profiles_dir=self.root)
            dependency.assert_not_called()

    async def test_probe_parameters_are_validated(self):
        for options in ({"timeout": 0}, {"timeout": True}, {"timeout": float("nan")}, {"timeout": 301},
                        {"url": "http://1.1.1.1"}, {"url": "https://user:pass@1.1.1.1"}, {"url": "https://[bad"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                await vpn.vpnCheck("account-1", profiles_dir=self.root, **options)

    async def test_workflow_task_uses_selected_manifest_and_params(self):
        ctx = TaskContext(None, Profile("account-1", self.folder / "src", manifest_path=self.manifest),
                          {"timeout": 9}, {}, self.root, "run", "vpnCheck")
        self.assertIs(resolve_tasks(Workflow("vpn", (Step("check", "vpnCheck"),)))["vpnCheck"], vpn.run)
        with patch(f"{TASK}._check", AsyncMock(return_value=TaskResult())) as check:
            await vpn.run(ctx)
        check.assert_awaited_once_with("account-1", self.manifest, timeout=9, url=vpn.DEFAULT_URL)


class VPNCLITests(unittest.TestCase):
    def test_cli_checks_vpn_without_browser_discovery_or_launch(self):
        with patch(f"{TASK}.vpnCheck", AsyncMock(return_value=TaskResult())) as check, \
                patch("zepuppeteer.cli.discover_profiles") as profiles, \
                patch("zepuppeteer.cli.find_brave") as brave:
            main(["vpnCheck", "account-1", "--profiles-dir", "/profiles", "--timeout", "8"])
        check.assert_awaited_once_with("account-1", profiles_dir="/profiles", timeout=8, url=vpn.DEFAULT_URL)
        profiles.assert_not_called()
        brave.assert_not_called()

    def test_failure_has_nonzero_exit(self):
        with patch(f"{TASK}.vpnCheck", AsyncMock(side_effect=RuntimeError("xray is missing"))), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as failure:
                main(["vpnCheck", "account-1"])
        self.assertEqual(failure.exception.code, 1)
        self.assertIn("xray is missing", output.getvalue())


class LinkTests(unittest.TestCase):
    def test_vless_websocket_and_reality_settings(self):
        outbound = vpn._outbound(LINK)
        self.assertEqual(outbound["protocol"], "vless")
        stream = outbound["streamSettings"]
        self.assertEqual(stream["wsSettings"], {"path": "/vpn", "headers": {"Host": "cdn.example.test"}})
        self.assertEqual(stream["tlsSettings"]["serverName"], "cdn.example.test")
        outbound = vpn._outbound("vless://id@host.test:443?security=reality&pbk=key&sid=abcd&flow=xtls-rprx-vision&fp=chrome")
        self.assertEqual(outbound["streamSettings"]["realitySettings"]["publicKey"], "key")
        self.assertEqual(outbound["settings"]["vnext"][0]["users"][0]["flow"], "xtls-rprx-vision")

    def test_vmess_trojan_and_shadowsocks(self):
        data = {"v": "2", "add": "host.test", "port": "443", "id": "test-id", "aid": "0",
                "net": "grpc", "path": "service", "tls": "tls", "sni": "host.test"}
        link = "vmess://" + base64.b64encode(json.dumps(data).encode()).decode().rstrip("=")
        outbound = vpn._outbound(link)
        self.assertEqual(outbound["protocol"], "vmess")
        self.assertEqual(outbound["streamSettings"]["grpcSettings"]["serviceName"], "service")
        outbound = vpn._outbound("trojan://pass%3Aword@[2001:db8::1]:443?security=tls")
        self.assertEqual(outbound["settings"]["servers"][0]["password"], "pass:word")
        credentials = base64.urlsafe_b64encode(b"aes-256-gcm:password").decode().rstrip("=")
        outbound = vpn._outbound(f"ss://{credentials}@host.test:8388#name")
        self.assertEqual(outbound["settings"]["servers"][0]["method"], "aes-256-gcm")

    def test_invalid_or_insecure_links_fail_without_exposing_secrets(self):
        for link in ("vless://secret@host:invalid", "vmess://secret", "https://secret.test/", "ss://secret@host:1",
                     "vless://secret@host:443?security=tls&allowInsecure=1",
                     "vless://secret@host:443?type=kcp", "vless://secret@host:443?security=reality",
                     "vless://secret@host:443?type=ws&type=grpc"):
            with self.subTest(link=link), self.assertRaises(ValueError) as error:
                vpn._outbound(link)
            self.assertNotIn("secret", str(error.exception))


class TunnelTests(VPNFixture):
    def setUp(self):
        super().setUp()
        self.command = AsyncMock(return_value=(0, ""))
        for name, replacement in (("_required", lambda name: f"/{name}"), ("_command", self.command),
                                  ("sys.platform", "linux"), ("os.geteuid", lambda: 0),
                                  ("shutil.which", lambda name: f"/{name}")):
            mock = patch(f"{TASK}.{name}", replacement)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_wireguard_only_routes_inside_namespace_and_never_executes_hooks(self):
        async def command(command, **kwargs):
            if command[0] == "/ip" and "/ping" in command:
                return 0, "rtt min/avg/max/mdev = 10.0/20.5/30.0/1.0 ms"
            return 0, ""
        self.command.side_effect = command
        with patch(f"{TASK}._probe", AsyncMock(return_value=122)):
            self.assertEqual(await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15), (122, 20.5))
        commands = [call.args[0] for call in self.command.call_args_list]
        routes = [command for command in commands if "route" in command]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0][1], "-n")
        self.assertIn("1.1.1.1/32", routes[0])
        self.assertIn(["netns", "delete"], [command[1:3] for command in commands])
        config = next(call.kwargs["input"] for call in self.command.call_args_list if "input" in call.kwargs)
        self.assertIn(f"PrivateKey = {PRIVATE_KEY}".encode(), config)
        for ignored in (b"PostUp", b"PostDown", b"DNS", b"ListenPort", b"FwMark", b"touch"):
            self.assertNotIn(ignored, config)
        configure = next(call for call in self.command.call_args_list if call.args[0][0] == "/wg")
        config_path = configure.args[0][-1]
        self.assertTrue(config_path.startswith("/etc/wireguard/.zepuppeteer-"))
        self.assertNotIn("/dev/stdin", configure.args[0])
        stage = next(call for call in self.command.call_args_list if call.args[0][0] == "/install")
        self.assertEqual(stage.args[0][1:], ["-m", "600", "-T", "/dev/stdin", config_path])
        self.assertIn(PRIVATE_KEY, configure.kwargs["redact"])
        self.assertIn(["/rm", "-f", "--", config_path], commands)

    async def test_staged_config_is_removed_on_write_or_configure_failure(self):
        for executable in ("/install", "/wg"):
            self.command.reset_mock()
            async def command(command, **kwargs):
                if command[0] == executable:
                    raise RuntimeError("staging or configuration failed")
                return 0, ""
            self.command.side_effect = command
            with self.subTest(executable=executable), self.assertRaisesRegex(RuntimeError, "staging or configuration"):
                await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15)
            commands = [call.args[0] for call in self.command.call_args_list]
            removals = [command for command in commands if command[0] == "/rm"]
            self.assertEqual(len(removals), 1)
            self.assertTrue(removals[0][-1].startswith("/etc/wireguard/.zepuppeteer-"))
            self.assertEqual(commands[-1][1:3], ["netns", "delete"])

    async def test_wireguard_cleanup_after_probe_failure_and_cancellation(self):
        for error in (RuntimeError("unreachable"), asyncio.CancelledError()):
            self.command.reset_mock()
            with self.subTest(error=type(error).__name__), \
                    patch(f"{TASK}._probe", AsyncMock(side_effect=error)), self.assertRaises(type(error)):
                await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15)
            last = [call.args[0] for call in self.command.call_args_list[-2:]]
            self.assertIn("link", last[0])
            self.assertIn("delete", last[0])
            self.assertEqual(last[1][1:3], ["netns", "delete"])

    async def test_wireguard_cleanup_before_interface_move_and_when_delete_fails(self):
        async def command(command, **kwargs):
            if command[0] == "/wg":
                raise RuntimeError("bad config")
            if command[1:3] == ["link", "delete"]:
                raise RuntimeError("delete timed out")
            return 0, ""
        self.command.side_effect = command
        with self.assertRaisesRegex(RuntimeError, "Could not remove"):
            await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15)
        self.assertEqual(self.command.call_args_list[-1].args[0][1:3], ["netns", "delete"])

    async def test_wireguard_cancellation_during_creation_still_cleans_namespace(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def command(command, **kwargs):
            if command[1:3] == ["netns", "add"]:
                started.set()
                await release.wait()
            return 0, ""
        self.command.side_effect = command
        task = asyncio.create_task(vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15))
        await started.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.command.call_args_list[-1].args[0][1:3], ["netns", "delete"])

    async def test_wireguard_rejects_probe_outside_allowed_ips_before_commands(self):
        self.config.write_text(WG.replace("0.0.0.0/0, ::/0", "10.0.0.0/24"))
        with self.assertRaisesRegex(ValueError, "AllowedIPs"):
            await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15)
        self.command.assert_not_called()

    async def test_unprivileged_wireguard_fails_without_interactive_sudo(self):
        self.command.return_value = (1, "")
        with patch(f"{TASK}.os.geteuid", return_value=1000), \
                patch(f"{TASK}.sys.stdin.isatty", return_value=False), \
                self.assertRaisesRegex(RuntimeError, "sudo ip netns list"):
            await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15)
        self.command.assert_awaited_once()
        self.assertEqual(self.command.call_args.args[0], ["/sudo", "-n", "/ip", "netns", "list"])

    async def test_endpoint_dns_failure_precedes_sudo_and_network_changes(self):
        self.config.write_text(WG.replace("127.0.0.1:51820", "vpn.example.test:51820"))
        with patch(f"{TASK}._resolve_wireguard_endpoint", AsyncMock(side_effect=RuntimeError("Cannot resolve WireGuard Endpoint"))), \
                patch(f"{TASK}._wireguard_privileges", AsyncMock()) as privileges:
            with self.assertRaisesRegex(RuntimeError, "Cannot resolve WireGuard Endpoint"):
                await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15)
        self.command.assert_not_called()
        privileges.assert_not_awaited()

    async def test_bad_key_precedes_sudo_and_never_echoes_key(self):
        self.config.write_text(WG.replace(PRIVATE_KEY, "invalid-private-key"))
        with self.assertRaisesRegex(ValueError, "WireGuard PrivateKey") as error:
            await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15)
        self.assertNotIn("invalid-private-key", str(error.exception))
        self.command.assert_not_called()

    async def test_missing_dependencies_fail_before_subprocesses(self):
        for dependency in ("wg", "ip", "xray"):
            with self.subTest(dependency=dependency), \
                    patch(f"{TASK}._required", REQUIRE), \
                    patch(f"{TASK}.shutil.which", side_effect=lambda name: None if name == dependency else f"/{name}"):
                with self.assertRaisesRegex(RuntimeError, dependency):
                    if dependency == "xray":
                        await vpn._xray(LINK, "/curl", vpn.DEFAULT_URL, 15)
                    else:
                        await vpn._wireguard(self.config, "/curl", vpn.DEFAULT_URL, 15)
        self.command.assert_not_called()

    async def test_xray_private_config_local_authenticated_proxy_and_cleanup(self):
        process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())
        writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        paths = []
        async def validate(command, **kwargs):
            path = Path(command[-1])
            paths.append(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            config = json.loads(path.read_text())
            inbound, = config["inbounds"]
            self.assertEqual(inbound["listen"], "127.0.0.1")
            self.assertEqual(inbound["settings"]["auth"], "password")
            self.assertEqual(len(config["outbounds"]), 1)
            return 0, ""
        self.command.side_effect = validate
        with patch(f"{TASK}.socket.socket") as socket_factory, \
                patch(f"{TASK}.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
                patch(f"{TASK}.asyncio.open_connection", AsyncMock(return_value=(None, writer))), \
                patch(f"{TASK}._probe", AsyncMock(side_effect=RuntimeError("unreachable"))):
            socket_factory.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
            with self.assertRaisesRegex(RuntimeError, "unreachable"):
                await vpn._xray(LINK, "/curl", vpn.DEFAULT_URL, 15)
        process.terminate.assert_called_once()
        self.assertFalse(paths[0].exists())


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_forces_proxy_and_ignores_curl_config(self):
        with patch(f"{TASK}._command", AsyncMock(return_value=(0, "0.125"))) as command:
            self.assertEqual(await vpn._probe("/curl", vpn.DEFAULT_URL, 4, proxy=("socks5h://127.0.0.1:123", "u:p")), 125)
        args = command.call_args.args[0]
        self.assertEqual(args[1], "--disable")
        self.assertIn("--proxy-user", args)
        self.assertEqual(args[args.index("--noproxy") + 1], "")

    async def test_command_timeout_terminates_child_and_errors_do_not_echo_secrets(self):
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            await vpn._command([sys.executable, "-c", "import time; time.sleep(10)"], label="probe", timeout=0.05)
        with self.assertRaises(RuntimeError) as error:
            await vpn._command([sys.executable, "-c", "import sys; print('secret', file=sys.stderr); sys.exit(1)"], label="probe")
        self.assertNotIn("secret", str(error.exception))

    async def test_wireguard_stderr_reports_cause_and_redacts_keys(self):
        message = f"Key is not the correct length or format: invalid-key\nOther key: {PRIVATE_KEY}\nConfiguration parsing error"
        with self.assertRaises(RuntimeError) as error:
            await vpn._command([sys.executable, "-c", "import sys; sys.stderr.write(sys.argv[1]); sys.exit(1)", message],
                               label="Configure WireGuard", redact=("invalid-key", PRIVATE_KEY))
        self.assertIn("Configuration parsing error", str(error.exception))
        self.assertIn("[redacted]", str(error.exception))
        self.assertNotIn("invalid-key", str(error.exception))
        self.assertNotIn(PRIVATE_KEY, str(error.exception))


class WireGuardConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_hostname_is_resolved_on_host_and_replaced_with_numeric_endpoint(self):
        config = WG.replace("127.0.0.1:51820", "vpn.example.test:51820").encode()
        for address, expected in (("192.0.2.1", "192.0.2.1:51820"), ("2001:db8::1", "[2001:db8::1]:51820")):
            with self.subTest(address=address), patch(f"{TASK}._resolve_wireguard_endpoint", AsyncMock(return_value=address)) as resolve:
                prepared, secrets = await vpn._prepare_wireguard_config(config, 15)
            resolve.assert_awaited_once_with("vpn.example.test", 51820, 15)
            self.assertIn(f"Endpoint = {expected}\n".encode(), prepared)
            self.assertNotIn(b"vpn.example.test", prepared)
            self.assertIn(PRIVATE_KEY, secrets)

    async def test_numeric_endpoints_do_not_require_dns(self):
        with patch(f"{TASK}._resolve_wireguard_endpoint", AsyncMock()) as resolve:
            prepared, _ = await vpn._prepare_wireguard_config(WG.encode(), 15)
        resolve.assert_not_awaited()
        self.assertIn(b"Endpoint = 127.0.0.1:51820\n", prepared)

    async def test_dns_worker_is_bounded_and_only_returns_an_ip(self):
        with patch(f"{TASK}._command", AsyncMock(return_value=(0, '"192.0.2.1"'))) as command:
            self.assertEqual(await vpn._resolve_wireguard_endpoint("vpn.example.test", 51820, 3), "192.0.2.1")
        self.assertEqual(command.call_args.kwargs["timeout"], 3)
        for result in ((0, '"not-an-address"'), (0, "")):
            with patch(f"{TASK}._command", AsyncMock(return_value=result)), \
                    self.assertRaisesRegex(RuntimeError, "Cannot resolve WireGuard Endpoint"):
                await vpn._resolve_wireguard_endpoint("vpn.example.test", 51820, 3)

    async def test_bad_endpoints_fail_without_exposing_values(self):
        for endpoint in ("invalid-secret", "host:invalid-secret", "host:0", "user:invalid-secret@host:443"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError) as error:
                await vpn._prepare_wireguard_config(WG.replace("127.0.0.1:51820", endpoint).encode(), 15)
            self.assertNotIn("invalid-secret", str(error.exception))


class WireGuardPrivilegeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, replacement in (("os.geteuid", lambda: 1000), ("_required", lambda name: f"/{name}")):
            mock = patch(f"{TASK}.{name}", replacement)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_cached_authorization_does_not_prompt(self):
        with patch(f"{TASK}._command", AsyncMock(return_value=(0, ""))) as command, \
                patch(f"{TASK}.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            self.assertEqual(await vpn._wireguard_privileges(), ["/sudo", "-n"])
        command.assert_awaited_once_with(["/sudo", "-n", "/ip", "netns", "list"],
                                        label="Check WireGuard sudo authorization", check=False)
        spawn.assert_not_awaited()

    async def test_command_specific_passwordless_access_does_not_validate_all_sudo_rules(self):
        async def policy(command, **kwargs):
            # This user can run the required ip command, but sudo -v would
            # require a password because other rules are not NOPASSWD.
            if command == ["/sudo", "-n", "/ip", "netns", "list"]:
                return 0, ""
            return 1, ""

        with patch(f"{TASK}._command", side_effect=policy), \
                patch(f"{TASK}.sys.stdin.isatty", return_value=False), \
                patch(f"{TASK}.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            self.assertEqual(await vpn._wireguard_privileges(), ["/sudo", "-n"])
        spawn.assert_not_awaited()

    async def test_terminal_authentication_inherits_terminal_and_returns_sudo_prefix(self):
        process = SimpleNamespace(wait=AsyncMock(return_value=0))
        with patch(f"{TASK}._command", AsyncMock(return_value=(1, ""))), \
                patch(f"{TASK}.sys.stdin.isatty", return_value=True), \
                patch(f"{TASK}.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await vpn._wireguard_privileges(), ["/sudo", "-n"])
        spawn.assert_awaited_once_with("/sudo", "/ip", "netns", "list")
        process.wait.assert_awaited_once()

    async def test_unattended_runs_fail_with_instructions_and_never_prompt(self):
        with patch(f"{TASK}._command", AsyncMock(return_value=(1, ""))), \
                patch(f"{TASK}.sys.stdin.isatty", return_value=False), \
                patch(f"{TASK}.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaisesRegex(RuntimeError, "sudo ip netns list.*same terminal"):
                await vpn._wireguard_privileges()
        spawn.assert_not_awaited()

    async def test_failed_authentication_stops_before_privileged_commands(self):
        process = SimpleNamespace(wait=AsyncMock(return_value=1))
        with patch(f"{TASK}._command", AsyncMock(return_value=(1, ""))), \
                patch(f"{TASK}.sys.stdin.isatty", return_value=True), \
                patch(f"{TASK}.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "authentication failed or permission was denied"):
                await vpn._wireguard_privileges()

    async def test_root_does_not_run_sudo(self):
        with patch(f"{TASK}.os.geteuid", return_value=0), patch(f"{TASK}._command", AsyncMock()) as command:
            self.assertEqual(await vpn._wireguard_privileges(), [])
        command.assert_not_awaited()
