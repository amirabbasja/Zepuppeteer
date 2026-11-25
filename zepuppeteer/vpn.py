"""Keep each VPN-enabled browser in a network namespace with no direct egress."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
from uuid import uuid4

from .config import read_toml
from .models import NeedsAttention
from .tasks import vpnCheck as probe
from .vpn_bridge import copy_stream


_SOCKS_ARGS = [
    "--proxy-server=socks5://127.0.0.1:1080",
    "--proxy-bypass-list=<-loopback>",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-quic",
]


async def _settle(task):
    """Finish owned resource cleanup even when cancellation repeats."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _metadata(profile):
    if profile.manifest_path is None:
        return {}
    return read_toml(profile.manifest_path)


def vpn_required(profile, platforms=None):
    """Honor a profile-wide flag and the selected platforms' use_vpn flags."""
    metadata = _metadata(profile)
    account = metadata.get("profile", {})
    if not isinstance(account, dict):
        raise ValueError("profile must be a TOML table.")
    flags = []
    for data, label in ((metadata, "use_vpn"), (account, "profile.use_vpn")):
        if "use_vpn" in data:
            value = data["use_vpn"]
            if not isinstance(value, bool):
                raise ValueError(f"{label} must be a boolean.")
            flags.append(value)
    configured = account.get("platforms", {})
    if not isinstance(configured, dict):
        raise ValueError("profile.platforms must be a TOML table.")
    for name, settings in configured.items():
        if platforms is not None and name not in platforms:
            continue
        if not isinstance(settings, dict):
            raise ValueError(f"profile.platforms.{name} must be a TOML table.")
        if "use_vpn" in settings:
            value = settings["use_vpn"]
            if not isinstance(value, bool):
                raise ValueError(f"profile.platforms.{name}.use_vpn must be a boolean.")
            flags.append(value)
    return any(flags)


def _vpn_settings(profile):
    metadata = _metadata(profile)
    account = metadata.get("profile", {})
    settings = account.get("vpn", account if "vpn_protocol" in account else metadata)
    if not isinstance(settings, dict):
        raise ValueError("profile.vpn must be a TOML table.")
    protocol = probe._text(settings.get("vpn_protocol"), "vpn_protocol").lower()
    if protocol not in {"wireguard", "v2ray"}:
        raise ValueError("vpn_protocol must be wireguard or v2ray.")
    return protocol, settings


def _dns_servers(path, networks):
    values = []
    interface = False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.partition("#")[0].strip()
        if line.startswith("["):
            interface = line == "[Interface]"
        key, separator, value = line.partition("=")
        if interface and separator and key.strip() == "DNS":
            for part in value.split(","):
                try:
                    values.append(ipaddress.ip_address(part.strip()))
                except ValueError:
                    raise ValueError("WireGuard DNS must contain numeric resolver IP addresses.") from None
    if not values:
        values = [ipaddress.ip_address("1.1.1.1")]
        if not any(network.version == 4 for network in networks):
            values = [ipaddress.ip_address("2606:4700:4700::1111")]
    if any(not any(address.version == network.version and address in network for network in networks)
           for address in values):
        raise ValueError("WireGuard AllowedIPs must include its DNS resolver addresses.")
    return values


def _unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@dataclass
class VPNRoute:
    profile: object
    protocol: str
    settings: dict
    namespace: str = field(default_factory=lambda: "zb" + uuid4().hex[:12])
    prefix: list = field(default_factory=list)
    browser_args: list = field(default_factory=list)
    environment: dict | None = None
    _sudo: list = field(default_factory=list)
    _resources: set = field(default_factory=set)
    _processes: list = field(default_factory=list)
    _connections: set = field(default_factory=set)
    _server: object = None
    _directory: object = None
    _proxy: object = None
    _timeout: float = 15
    _authorization_task: object = None
    _authorization_error: object = None

    async def _change(self, command, label, resource=None, present=True, **kwargs):
        async def operation():
            result = await probe._command(command, label=label, **kwargs)
            if resource:
                if present:
                    self._resources.add(resource)
                else:
                    self._resources.discard(resource)
            return result
        task = asyncio.create_task(operation())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _settle(task)
            except Exception:
                pass
            raise

    async def _spawn(self, *args, **kwargs):
        """Record a launched process even if cancellation arrives during spawn."""
        task = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
        try:
            process = await asyncio.shield(task)
        except asyncio.CancelledError:
            process = await _settle(task)
            self._processes.append(process)
            raise
        self._processes.append(process)
        return process

    async def start(self):
        if sys.platform != "linux":
            raise RuntimeError("Per-profile VPN routing requires Linux network namespaces.")
        if self.protocol == "wireguard" and any(Path(path).exists() for path in ("/run/nscd/socket", "/var/run/nscd/socket")):
            raise RuntimeError("WireGuard browser isolation cannot use the host nscd DNS cache; disable nscd first.")
        self._ip = probe._required("ip")
        self._remove = probe._required("rm")
        self._kill = probe._required("kill")
        setpriv = probe._required("setpriv")
        self._curl = probe._required("curl")
        # Parse the private settings before privileged changes or authentication.
        if self.protocol == "wireguard":
            path = Path(probe._text(self.settings.get("wireguard_file"), "wireguard_file")).expanduser()
            if not path.is_absolute():
                path = self.profile.manifest_path.parent / path
            config, addresses, mtu, networks = probe._wireguard_config(path)
            config, secrets = await probe._prepare_wireguard_config(config, self._timeout)
            dns = _dns_servers(path, networks)
            self._probe_url = probe.DEFAULT_URL
            if not any(network.version == 4 and ipaddress.ip_address("1.1.1.1") in network for network in networks):
                target = ipaddress.ip_address("2606:4700:4700::1111")
                if not any(network.version == 6 and target in network for network in networks):
                    raise ValueError("WireGuard AllowedIPs must include 1.1.1.1 or 2606:4700:4700::1111 for connectivity verification.")
                self._probe_url = "https://[2606:4700:4700::1111]/cdn-cgi/trace"
            self._wg = probe._required("wg")
            self._install = probe._required("install")
            self._mkdir = probe._required("mkdir")
        else:
            self._xray = probe._required("xray")
            outbound = probe._outbound(probe._text(self.settings.get("v2ray_link"), "v2ray_link"))
        self._sudo = await probe._wireguard_privileges()
        await self._change([*self._sudo, self._ip, "netns", "add", self.namespace],
                           "Create browser VPN namespace", "namespace")
        self.prefix = [*self._sudo, self._ip, "netns", "exec", self.namespace, setpriv,
                       "--reuid", str(os.getuid()), "--regid", str(os.getgid()), "--init-groups"]
        await probe._command([*self._sudo, self._ip, "-n", self.namespace, "link", "set", "lo", "up"],
                             label="Enable browser namespace loopback")
        if self.protocol == "wireguard":
            await self._start_wireguard(config, secrets, addresses, mtu, networks, dns)
        else:
            await self._start_xray(outbound)
        await self.check_connection()
        if self._sudo:
            self._authorization_task = asyncio.create_task(self._refresh_authorization())

    async def _start_wireguard(self, config, secrets, addresses, mtu, networks, dns):
        name = self.namespace
        self._config_path = f"/etc/wireguard/.zepuppeteer-{name}.conf"
        await self._change([*self._sudo, self._ip, "link", "add", name, "type", "wireguard"],
                           "Create browser WireGuard interface", "interface")
        await probe._command([*self._sudo, self._mkdir, "-p", "-m", "700", "/etc/wireguard"],
                             label="Prepare private WireGuard settings")
        self._resources.add("config")
        await self._change([*self._sudo, self._install, "-m", "600", "-T", "/dev/stdin", self._config_path],
                           "Stage browser WireGuard configuration", input=config, redact=secrets)
        await probe._command([*self._sudo, self._wg, "setconf", name, self._config_path],
                             label="Configure browser WireGuard interface", redact=secrets)
        await self._change([*self._sudo, self._remove, "-f", "--", self._config_path],
                           "Remove staged WireGuard keys", "config", False)
        await self._change([*self._sudo, self._ip, "link", "set", name, "netns", name],
                           "Isolate browser WireGuard interface", "moved")
        local = [*self._sudo, self._ip, "-n", name]
        for address in addresses:
            options = ["nodad"] if ipaddress.ip_interface(address).version == 6 else []
            await probe._command([*local, "address", "add", address, "dev", name, *options],
                                 label="Set browser VPN address")
        await probe._command([*local, "link", "set", "dev", name, "mtu", str(mtu), "up"],
                             label="Enable browser VPN interface")
        # Route all allowed prefixes, including provider default routes. Other
        # destinations have no route; no host-facing interface exists here.
        for network in sorted(set(networks), key=lambda network: (network.version, network.prefixlen, int(network.network_address))):
            await probe._command([*local, f"-{network.version}", "route", "replace", str(network), "dev", name],
                                 label="Route browser traffic only through WireGuard")
        await probe._command([*self._sudo, self._mkdir, "-p", "/etc/netns"],
                             label="Prepare isolated resolver directory")
        self._dns_path = f"/etc/netns/{name}"
        await self._change([*self._sudo, self._mkdir, "-m", "755", self._dns_path],
                           "Create browser-only DNS settings", "dns")
        resolv = "".join(f"nameserver {address}\n" for address in dns).encode()
        nss = Path("/etc/nsswitch.conf").read_text(encoding="utf-8")
        nss, replacements = re.subn(r"^[ \t]*hosts[ \t]*:.*$", "hosts: files dns", nss, flags=re.M)
        if not replacements:
            nss += "\nhosts: files dns\n"
        for filename, contents in (("resolv.conf", resolv), ("nsswitch.conf", nss.encode())):
            await probe._command([*self._sudo, self._install, "-m", "644", "-T", "/dev/stdin", f"{self._dns_path}/{filename}"],
                                 label="Set isolated browser resolver", input=contents)
        self.browser_args = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]

    async def _start_xray(self, outbound):
        self._directory = tempfile.TemporaryDirectory(prefix="zepuppeteer-browser-vpn-")
        directory = Path(self._directory.name)
        directory.chmod(0o700)
        port = _unused_port()
        password = uuid4().hex
        self._proxy = (f"socks5h://127.0.0.1:{port}", f"browser:{password}")
        settings = {"log": {"loglevel": "none"}, "inbounds": [{
            "listen": "127.0.0.1", "port": port, "protocol": "socks",
            "settings": {"auth": "password", "accounts": [{"user": "browser", "pass": password}], "udp": False}}],
            "outbounds": [outbound]}
        config = directory / "xray.json"
        with open(config, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            json.dump(settings, stream)
        await probe._command([self._xray, "run", "-test", "-c", str(config)], label="Validate browser VPN configuration")
        process = await self._spawn(self._xray, "run", "-c", str(config),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await self._wait_listener(port, process)

        async def bridge(reader, writer):
            task = asyncio.current_task()
            self._connections.add(task)
            upstream = None
            try:
                version, count = await asyncio.wait_for(reader.readexactly(2), 5)
                methods = await asyncio.wait_for(reader.readexactly(count), 5)
                if version != 5 or 0 not in methods:
                    return
                remote, upstream = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
                upstream.write(b"\x05\x01\x02")
                await upstream.drain()
                if await remote.readexactly(2) != b"\x05\x02":
                    return
                user, secret = b"browser", password.encode()
                upstream.write(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
                await upstream.drain()
                if await remote.readexactly(2) != b"\x01\x00":
                    return
                writer.write(b"\x05\x00")
                await writer.drain()
                await asyncio.gather(copy_stream(reader, upstream), copy_stream(remote, writer))
            except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                pass
            finally:
                for stream in (writer, upstream):
                    if stream is not None:
                        stream.close()
                        try:
                            await stream.wait_closed()
                        except (ConnectionError, OSError):
                            pass
                self._connections.discard(task)

        socket_path = directory / "proxy.sock"
        server_task = asyncio.create_task(asyncio.start_unix_server(bridge, path=socket_path))
        try:
            self._server = await asyncio.shield(server_task)
        except asyncio.CancelledError:
            self._server = await _settle(server_task)
            raise
        socket_path.chmod(0o600)
        ready = directory / "bridge.ready"
        helper = str(Path(__file__).with_name("vpn_bridge.py"))
        process = await self._spawn(*self.prefix, sys.executable, helper,
            "--socket", str(socket_path), "--port", "1080", "--ready", str(ready),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        deadline = asyncio.get_running_loop().time() + 5
        while not ready.exists():
            if process.returncode is not None or asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Browser VPN namespace relay did not start.")
            await asyncio.sleep(0.05)
        self.browser_args = list(_SOCKS_ARGS)

    async def _wait_listener(self, port, process):
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            if process.returncode is not None:
                raise RuntimeError("Browser VPN process exited during startup.")
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.2)
                writer.close()
                await writer.wait_closed()
                return
            except (OSError, asyncio.TimeoutError):
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError("Browser VPN proxy did not start.") from None
                await asyncio.sleep(0.05)

    async def check_connection(self):
        if self._authorization_error is not None:
            raise NeedsAttention(f"Profile '{self.profile.name}': VPN sudo authorization is no longer available.") from self._authorization_error
        if any(process.returncode is not None for process in self._processes):
            raise NeedsAttention(f"Profile '{self.profile.name}': browser VPN process is no longer running.")
        if self.protocol == "wireguard":
            return await probe._probe(self._curl, self._probe_url, self._timeout, prefix=self.prefix)
        return await probe._probe(self._curl, probe.DEFAULT_URL, self._timeout, prefix=self.prefix,
                                  proxy=("socks5h://127.0.0.1:1080", ""))

    async def _refresh_authorization(self):
        # Keep this session's cached sudo timestamp valid for eventual cleanup.
        # This does not change sudo rules or authenticate noninteractive users.
        try:
            while True:
                await asyncio.sleep(60)
                code, _ = await probe._command([*self._sudo, self._ip, "netns", "list"],
                    label="Refresh browser VPN sudo authorization", check=False)
                if code:
                    raise RuntimeError("Cached VPN sudo authorization expired.")
        except (OSError, ValueError, RuntimeError) as error:
            self._authorization_error = error

    async def close(self):
        failures = []
        if self._authorization_task is not None:
            self._authorization_task.cancel()
            await asyncio.gather(self._authorization_task, return_exceptions=True)
        namespace_empty = True
        if "namespace" in self._resources:
            namespace_empty = await self._stop_namespace_processes()
            if not namespace_empty:
                failures.append("live namespace processes; namespace preserved")
        for process in reversed(self._processes):
            try:
                await probe._stop(process)
            except Exception:
                failures.append("VPN process")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in tuple(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*tuple(self._connections), return_exceptions=True)
        if self._directory is not None:
            self._directory.cleanup()
        commands = []
        if "config" in self._resources:
            commands.append([*self._sudo, self._remove, "-f", "--", self._config_path])
        if "interface" in self._resources:
            local = [self._ip, "-n", self.namespace] if "moved" in self._resources else [self._ip]
            commands.append([*self._sudo, *local, "link", "delete", self.namespace])
        if "namespace" in self._resources and namespace_empty:
            commands.append([*self._sudo, self._ip, "netns", "delete", self.namespace])
        if "dns" in self._resources:
            commands.append([*self._sudo, self._remove, "-rf", "--", self._dns_path])
        for command in commands:
            try:
                code, _ = await probe._command(command, label="Remove browser VPN resources", check=False)
                if code:
                    failures.append("namespace/interface")
            except Exception:
                failures.append("namespace/interface")
        if failures:
            raise NeedsAttention(f"Could not clean up browser VPN '{self.namespace}': {', '.join(failures)}.")

    async def _stop_namespace_processes(self):
        """A named namespace must stay inspectable until all its processes exit."""
        async def pids():
            code, output = await probe._command(
                [*self._sudo, self._ip, "netns", "pids", self.namespace],
                label="Find processes in owned browser VPN namespace", check=False)
            if code or any(not item.isdecimal() or int(item) <= 1 for item in output.split()):
                raise RuntimeError("Cannot inspect browser VPN namespace processes.")
            return output.split()
        try:
            for sig in ("-TERM", "-KILL"):
                processes = await pids()
                if not processes:
                    return True
                await probe._command([*self._sudo, self._ip, "netns", "exec", self.namespace,
                                      self._kill, sig, "--", *processes],
                                     label="Stop processes in owned browser VPN namespace", check=False)
                await asyncio.sleep(0.1)
            return not await pids()
        except (OSError, ValueError, RuntimeError):
            return False


@asynccontextmanager
async def browser_vpn(profile, *, platforms=None):
    """Create and hold an isolated VPN until the browser has been closed."""
    try:
        required = vpn_required(profile, platforms)
        if required:
            protocol, settings = _vpn_settings(profile)
    except (OSError, ValueError, TypeError) as error:
        raise NeedsAttention(f"Profile '{profile.name}': invalid VPN configuration: {error}") from error
    if not required:
        yield None
        return
    route = VPNRoute(profile, protocol, settings)
    failure = None
    try:
        try:
            await route.start()
        except (OSError, ValueError, RuntimeError) as error:
            raise NeedsAttention(f"Profile '{profile.name}': browser VPN could not connect: {error}") from error
        yield route
    except BaseException as error:
        failure = error
        raise
    finally:
        cleanup = asyncio.create_task(route.close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await _settle(cleanup)
            raise
        except Exception as error:
            if failure is not None:
                raise failure from error
            raise
