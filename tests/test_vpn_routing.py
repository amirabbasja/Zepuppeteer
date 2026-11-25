"""VPN browser sessions contain all egress inside their owned namespaces."""

import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zepuppeteer.models import NeedsAttention, Profile
from zepuppeteer.vpn import VPNRoute, browser_vpn, vpn_required


MODULE = "zepuppeteer.vpn"
KEY = base64.b64encode(bytes(range(32))).decode()
WIREGUARD = f"""[Interface]
PrivateKey = {KEY}
Address = 10.0.0.2/32, fd00::2/128
DNS = 1.1.1.1
[Peer]
PublicKey = {KEY}
Endpoint = 127.0.0.1:51820
AllowedIPs = 0.0.0.0/0, ::/0
"""


class FlagTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "profile.toml"
        self.profile = Profile("test", self.root / "src", manifest_path=self.manifest)

    def test_profile_flags_and_selected_platforms(self):
        self.manifest.write_text('[profile.platforms.colab]\nuse_vpn=true\n[profile.platforms.kaggle]\nuse_vpn=false\n')
        self.assertTrue(vpn_required(self.profile))
        self.assertTrue(vpn_required(self.profile, {"colab"}))
        self.assertFalse(vpn_required(self.profile, {"kaggle"}))
        self.manifest.write_text('use_vpn=true\n[profile.platforms.kaggle]\nuse_vpn=false\n')
        self.assertTrue(vpn_required(self.profile, {"kaggle"}))
        self.manifest.write_text('[profile]\nuse_vpn=true\n')
        self.assertTrue(vpn_required(self.profile, set()))

    def test_absent_flag_is_disabled_and_invalid_values_are_rejected(self):
        self.assertFalse(vpn_required(replace(self.profile, manifest_path=None)))
        for text in ('use_vpn="false"', '[profile]\nuse_vpn=1', '[profile.platforms.colab]\nuse_vpn="true"'):
            self.manifest.write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "boolean"):
                vpn_required(self.profile)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "profile.toml"
        self.manifest.write_text('[profile]\nuse_vpn=true\n[profile.vpn]\nvpn_protocol="wireguard"\nwireguard_file="wg.conf"\n')
        (self.root / "wg.conf").write_text(WIREGUARD)
        self.profile = Profile("test", self.root / "src", manifest_path=self.manifest)
        self.command = self.enterContext(patch(f"{MODULE}.probe._command", AsyncMock(return_value=(0, ""))))
        self.enterContext(patch(f"{MODULE}.probe._required", side_effect=lambda name: f"/{name}"))
        self.enterContext(patch(f"{MODULE}.probe._wireguard_privileges", AsyncMock(return_value=["/sudo", "-n"])))
        self.check = self.enterContext(patch(f"{MODULE}.probe._probe", AsyncMock(return_value=20.0)))
        self.enterContext(patch(f"{MODULE}.os.getuid", return_value=1000))
        self.enterContext(patch(f"{MODULE}.os.getgid", return_value=1000))
        self.enterContext(patch(f"{MODULE}.sys.platform", "linux"))

    async def test_wireguard_only_routes_inside_namespace_and_lives_until_browser_exit(self):
        async with browser_vpn(self.profile) as route:
            namespace = route.namespace
            self.assertIn("--reuid", route.prefix)
            self.assertIn("1000", route.prefix)
            self.assertEqual(route.protocol, "wireguard")
            self.assertTrue(await route.check_connection())
            commands = [call.args[0] for call in self.command.call_args_list]
            self.assertFalse(any(command[-3:] == ["netns", "delete", namespace] for command in commands))
            routes = [command for command in commands if "route" in command]
            self.assertEqual(len(routes), 2)
            self.assertTrue(all(command[2:5] == ["/ip", "-n", namespace] for command in routes))
            self.assertTrue(any("0.0.0.0/0" in command for command in routes))
            self.assertTrue(any("::/0" in command for command in routes))
            self.assertFalse(any("veth" in command for command in commands))
            self.assertTrue(all(KEY not in " ".join(command) for command in commands))
            self.assertEqual(self.check.call_args.kwargs["prefix"], route.prefix)
            self.assertTrue(any(call.kwargs.get("input") == b"nameserver 1.1.1.1\n" for call in self.command.call_args_list))
        self.assertTrue(any(call.args[0][-3:] == ["netns", "delete", namespace] for call in self.command.call_args_list))
        self.assertTrue(route._authorization_task.done())

    async def test_connection_failure_never_yields_and_removes_resources(self):
        self.check.side_effect = RuntimeError("VPN unreachable")
        with self.assertRaisesRegex(NeedsAttention, "VPN unreachable"):
            async with browser_vpn(self.profile):
                self.fail("An unverified VPN must never launch a browser")
        commands = [call.args[0] for call in self.command.call_args_list]
        self.assertTrue(any("delete" in command and "netns" in command for command in commands))
        self.assertTrue(any("delete" in command and "link" in command for command in commands))

    async def test_cancelled_setup_finishes_cleanup(self):
        self.check.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            async with browser_vpn(self.profile):
                self.fail("Cancelled setup must not yield")
        self.assertTrue(any("netns" in call.args[0] and "delete" in call.args[0] for call in self.command.call_args_list))

    async def test_invalid_vpn_flags_fail_closed_as_needs_attention(self):
        self.manifest.write_text('use_vpn="true"\n')
        with self.assertRaisesRegex(NeedsAttention, "boolean"):
            async with browser_vpn(self.profile):
                self.fail("Invalid flags must fail closed")
        self.command.assert_not_awaited()

    async def test_missing_tools_fail_before_namespace_creation(self):
        with patch(f"{MODULE}.probe._required", side_effect=RuntimeError("wg missing")):
            with self.assertRaisesRegex(NeedsAttention, "wg missing"):
                async with browser_vpn(self.profile):
                    self.fail("Missing tools must fail closed")
        self.command.assert_not_awaited()

    async def test_host_dns_cache_cannot_bypass_wireguard_dns_isolation(self):
        original_exists = Path.exists
        def exists(path):
            return str(path) == "/run/nscd/socket" or original_exists(path)
        with patch.object(Path, "exists", exists):
            with self.assertRaisesRegex(NeedsAttention, "nscd"):
                async with browser_vpn(self.profile):
                    self.fail("Host DNS cache must not bypass the VPN")
        self.command.assert_not_awaited()

    async def test_namespace_processes_are_killed_before_namespace_is_removed(self):
        async with browser_vpn(self.profile) as route:
            namespace = route.namespace
            responses = iter(((0, "123\n456\n"), (0, "")))
            async def command(command, **kwargs):
                if "pids" in command:
                    return next(responses)
                return 0, ""
            self.command.side_effect = command
        calls = [call.args[0] for call in self.command.call_args_list]
        kill = next(i for i, command in enumerate(calls) if "-TERM" in command)
        delete = next(i for i, command in enumerate(calls) if command[-3:] == ["netns", "delete", namespace])
        self.assertLess(kill, delete)
        self.assertIn("123", calls[kill])

    async def test_uninspectable_namespace_is_preserved_for_cleanup(self):
        with self.assertRaisesRegex(NeedsAttention, "namespace preserved"):
            async with browser_vpn(self.profile) as route:
                namespace = route.namespace
                async def command(command, **kwargs):
                    return (1, "") if "pids" in command else (0, "")
                self.command.side_effect = command
        self.assertFalse(any(call.args[0][-3:] == ["netns", "delete", namespace] for call in self.command.call_args_list))

    async def test_v2ray_namespace_has_only_loopback_and_private_proxy_bridge(self):
        self.manifest.write_text('[profile]\nuse_vpn=true\n[profile.vpn]\nvpn_protocol="v2ray"\nv2ray_link="fixture"\n')
        processes = []
        handlers = []
        async def spawn(*args, **kwargs):
            process = SimpleNamespace(returncode=None)
            processes.append(process)
            if "--ready" in args:
                Path(args[args.index("--ready") + 1]).write_text("123")
            return process
        async def server(handler, *, path):
            handlers.append(handler)
            Path(path).touch(mode=0o600)
            return SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        with patch(f"{MODULE}.probe._outbound", return_value={"protocol": "vless", "settings": {}}), \
                patch(f"{MODULE}._unused_port", return_value=12000), \
                patch(f"{MODULE}.asyncio.start_unix_server", side_effect=server), \
                patch(f"{MODULE}.asyncio.create_subprocess_exec", side_effect=spawn), \
                patch(f"{MODULE}.VPNRoute._wait_listener", AsyncMock()), \
                patch(f"{MODULE}.probe._stop", AsyncMock()):
            async with browser_vpn(self.profile) as route:
                namespace = route.namespace
                args = route.browser_args
                self.assertIn("--proxy-server=socks5://127.0.0.1:1080", args)
                self.assertIn("--proxy-bypass-list=<-loopback>", args)
                self.assertIn("--disable-quic", args)
                commands = [call.args[0] for call in self.command.call_args_list]
                self.assertFalse(any("route" in command or "veth" in command for command in commands))
                configs = list(Path(route._directory.name).glob("xray.json"))
                self.assertEqual(configs[0].stat().st_mode & 0o777, 0o600)
                config = json.loads(configs[0].read_text())
                self.assertEqual(config["inbounds"][0]["settings"]["auth"], "password")
                self.assertEqual(len(config["outbounds"]), 1)
                self.assertNotIn("freedom", json.dumps(config))
                self.assertEqual(self.check.call_args.kwargs["prefix"], route.prefix)
                client, remote = asyncio.StreamReader(), asyncio.StreamReader()
                client.feed_data(b"\x05\x01\x00hello")
                client.feed_eof()
                remote.feed_data(b"\x05\x02\x01\x00world")
                remote.feed_eof()
                writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock(), can_write_eof=lambda: False)
                upstream = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock(), can_write_eof=lambda: False)
                with patch(f"{MODULE}.asyncio.open_connection", AsyncMock(return_value=(remote, upstream))):
                    await handlers[0](client, writer)
                self.assertEqual(writer.write.call_args_list[0].args[0], b"\x05\x00")
                self.assertEqual(upstream.write.call_args_list[0].args[0], b"\x05\x01\x02")
                self.assertIn(b"browser", upstream.write.call_args_list[1].args[0])
                self.assertIn(b"world", [call.args[0] for call in writer.write.call_args_list])
                self.assertIn(b"hello", [call.args[0] for call in upstream.write.call_args_list])
                directory = route._directory.name
            self.assertFalse(Path(directory).exists())
        self.assertEqual(len(processes), 2)

    async def test_dead_vpn_process_is_reported(self):
        route = VPNRoute(self.profile, "v2ray", {})
        route._processes = [SimpleNamespace(returncode=1)]
        with self.assertRaisesRegex(NeedsAttention, "no longer running"):
            await route.check_connection()

    async def test_authorization_refresh_failure_is_reported_without_fallback(self):
        route = VPNRoute(self.profile, "wireguard", {})
        route._sudo = ["/sudo", "-n"]
        route._ip = "/ip"
        self.command.return_value = (1, "")
        with patch(f"{MODULE}.asyncio.sleep", AsyncMock()):
            await route._refresh_authorization()
        with self.assertRaisesRegex(NeedsAttention, "authorization is no longer available"):
            await route.check_connection()

    async def test_repeated_cancellation_finishes_resource_creation_and_tracks_ownership(self):
        route = VPNRoute(self.profile, "wireguard", {})
        started, finish = asyncio.Event(), asyncio.Event()
        async def command(*args, **kwargs):
            started.set()
            await finish.wait()
            return 0, ""
        self.command.side_effect = command
        operation = asyncio.create_task(route._change(["/ip", "netns", "add", "owned"], "Create namespace", "namespace"))
        await started.wait()
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertIn("namespace", route._resources)

    async def test_cancelled_process_spawn_still_records_child_for_cleanup(self):
        route = VPNRoute(self.profile, "v2ray", {})
        started, finish = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None)
        async def spawn(*args, **kwargs):
            started.set()
            await finish.wait()
            return process
        with patch(f"{MODULE}.asyncio.create_subprocess_exec", side_effect=spawn):
            operation = asyncio.create_task(route._spawn("/xray"))
            await started.wait()
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation
        self.assertEqual(route._processes, [process])
