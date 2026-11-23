"""Keep a profile's VPN alive for the browser lifecycle, with live heartbeats."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import tempfile
from uuid import uuid4

from .config import read_toml
from .models import NeedsAttention
from .tasks import vpnCheck as vpn


class VPNSetupError(RuntimeError):
    """An actionable VPN diagnostic that contains no configuration secrets."""


def _required(name):
    try:
        return vpn._required(name)
    except RuntimeError:
        raise VPNSetupError(f"Required executable '{name}' is missing. Install it before running Colab.") from None


async def _command(command, **kwargs):
    # vpn._command only includes stderr when explicit redaction is supplied.
    # Preserve those sanitized diagnostics instead of hiding the failing step.
    try:
        return await vpn._command(command, **kwargs)
    except RuntimeError as error:
        raise VPNSetupError(str(error)) from None
    except OSError as error:
        raise VPNSetupError(f"{kwargs['label']}: {error.strerror}.") from None


def uses_vpn(colab_data):
    value = colab_data.get("use_vpn", False)
    if type(value) is not bool:
        raise ValueError("Colab use_vpn must be a boolean.")
    return value


def colab_uses_vpn(profile):
    if profile.manifest_path is None:
        return False
    data = read_toml(profile.manifest_path).get("profile", {}).get("platforms", {}).get("colab", {})
    if not isinstance(data, dict):
        raise ValueError("Colab platform data must be a TOML table.")
    return uses_vpn(data)


@dataclass
class VPNConnection:
    profile_name: str
    curl: str
    proxy: dict | None = field(default=None, repr=False)
    process: object = None

    async def run(self, operation, *, heartbeat_interval=15):
        """Cancel browser work if the VPN stops responding while it runs."""
        async def watch():
            while True:
                await asyncio.sleep(heartbeat_interval)
                await self.heartbeat()

        work = asyncio.ensure_future(operation)
        monitor = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait((work, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            return await work
        finally:
            for task in (work, monitor):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, monitor, return_exceptions=True)

    async def heartbeat(self):
        """Require traffic through the active tunnel, never a direct fallback."""
        stage = "checking the active VPN transport"
        try:
            if self.proxy is None or self.process is None:
                raise VPNSetupError("VPN has no active transport.")
            if self.process.returncode is not None:
                raise VPNSetupError("VPN proxy disconnected.")
            proxy = (self.proxy["server"], f'{self.proxy["username"]}:{self.proxy["password"]}')
            stage = "sending the HTTPS heartbeat through the VPN"
            latency = await vpn._probe(self.curl, vpn.DEFAULT_URL, 15, proxy=proxy)
            if not math.isfinite(latency) or latency < 0:
                raise VPNSetupError("Invalid VPN heartbeat.")
            if self.process.returncode is not None:
                raise VPNSetupError("VPN proxy disconnected during the heartbeat.")
        except Exception as error:
            detail = str(error) if isinstance(error, VPNSetupError) else f"Failed while {stage}."
            raise NeedsAttention(
                f"Profile '{self.profile_name}': VPN is not connected or has no heartbeat. "
                f"{detail} Check the VPN connection before retrying Colab."
            ) from None


def _outbound_secrets(value):
    """Collect credentials that Xray might repeat in validation diagnostics."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"id", "password", "pass", "secretKey", "preSharedKey", "publicKey"} and isinstance(item, str):
                yield item
            else:
                yield from _outbound_secrets(item)
    elif isinstance(value, list):
        for item in value:
            yield from _outbound_secrets(item)


@asynccontextmanager
async def _xray_proxy(outbound, profile, curl, *, label, secrets=()):
    """Expose one VPN outbound to Brave without changing the host network."""
    xray = _required("xray")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    password = uuid4().hex
    config = {"log": {"loglevel": "none"},
              "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "http",
                            "settings": {"accounts": [{"user": "colab", "pass": password}]}}],
              "outbounds": [outbound]}
    with tempfile.TemporaryDirectory(prefix="zepuppeteer-vpn-") as directory:
        path = Path(directory) / "xray.json"
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            json.dump(config, stream)
        await _command([xray, "run", "-test", "-c", str(path)], label=f"Validate {label} proxy configuration",
                       redact=(*secrets, password, *_outbound_secrets(outbound)))
        startup = asyncio.create_task(asyncio.create_subprocess_exec(
            xray, "run", "-c", str(path), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
        try:
            process = await asyncio.shield(startup)
        except asyncio.CancelledError:
            # A cancelled spawn can already have created its child. Reap it
            # before removing the private configuration or leaving the scope.
            try:
                process = await startup
            except Exception:
                pass
            else:
                await vpn._stop(process)
            raise
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                if process.returncode is not None:
                    raise VPNSetupError(f"{label} proxy exited before connecting. Check Xray and the VPN configuration.")
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.2)
                    writer.close()
                    await writer.wait_closed()
                    break
                except (OSError, asyncio.TimeoutError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise VPNSetupError(f"{label} did not start its local proxy.") from None
                    await asyncio.sleep(0.05)
            yield VPNConnection(profile.name, curl, process=process,
                                proxy={"server": f"http://127.0.0.1:{port}",
                                       "username": "colab", "password": password,
                                       "bypass": "<-loopback>"})
        finally:
            await vpn._stop(process)


async def _wireguard_outbound(path):
    """Convert a wg-quick file to an entirely userspace Xray outbound."""
    try:
        config, addresses, mtu, _ = vpn._wireguard_config(path)
        config, secrets = await vpn._prepare_wireguard_config(config, 15)
        dns, in_interface = [], False
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.partition("#")[0].strip()
            if line.startswith("["):
                in_interface = line == "[Interface]"
            key, _, value = line.partition("=")
            if in_interface and key.strip() == "DNS":
                dns.extend(str(ipaddress.ip_address(item.strip())) for item in value.split(","))

        settings = {"noKernelTun": True, "address": [str(ipaddress.ip_interface(item).ip) for item in addresses],
                    "mtu": mtu, "remoteDNS": dns or ["1.1.1.1", "1.0.0.1"], "peers": []}
        peer = None
        names = {"PublicKey": "publicKey", "PresharedKey": "preSharedKey", "Endpoint": "endpoint"}
        for line in config.decode("utf-8").splitlines():
            if line == "[Peer]":
                peer = {}
                settings["peers"].append(peer)
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key == "PrivateKey":
                settings["secretKey"] = value
            elif key in names:
                peer[names[key]] = value
            elif key == "AllowedIPs":
                peer.setdefault("allowedIPs", []).extend(
                    str(ipaddress.ip_network(item.strip(), strict=False)) for item in value.split(","))
            elif key == "PersistentKeepalive":
                keepalive = 0 if value.lower() == "off" else int(value)
                if not 0 <= keepalive <= 65535:
                    raise ValueError("Invalid WireGuard keepalive.")
                peer["keepAlive"] = keepalive
        if any(not all(peer.get(key) for key in ("publicKey", "endpoint", "allowedIPs")) for peer in settings["peers"]):
            raise ValueError("Incomplete WireGuard peer.")
        return {"protocol": "wireguard", "settings": settings}, secrets
    except OSError:
        raise VPNSetupError("Cannot read wireguard_file. Check its path and file permissions.") from None
    except (ValueError, UnicodeError):
        # Parser errors can echo private input; never expose those values.
        raise VPNSetupError("Invalid WireGuard configuration. Check its keys, addresses, MTU, DNS, AllowedIPs, and peer endpoints.") from None
    except RuntimeError as error:
        raise VPNSetupError(str(error)) from None


@asynccontextmanager
async def _v2ray(data, profile, curl):
    outbound = vpn._outbound(data.get("v2ray_link"))
    async with _xray_proxy(outbound, profile, curl, label="V2Ray") as connection:
        yield connection


@asynccontextmanager
async def _wireguard(data, profile, curl):
    path = Path(vpn._text(data.get("wireguard_file"), "wireguard_file")).expanduser()
    if not path.is_absolute():
        path = profile.manifest_path.parent / path
    outbound, secrets = await _wireguard_outbound(path)
    async with _xray_proxy(outbound, profile, curl, label="WireGuard", secrets=secrets) as connection:
        yield connection


@asynccontextmanager
async def connect_vpn(profile):
    """Connect before browser launch; retain the tunnel until the browser closes."""
    async with AsyncExitStack() as resources:
        try:
            if profile.manifest_path is None:
                raise ValueError("A profile.toml with VPN settings is required.")
            metadata = read_toml(profile.manifest_path)
            profile_data = metadata.get("profile", {})
            data = profile_data.get("vpn", profile_data if "vpn_protocol" in profile_data else metadata)
            protocol = vpn._text(data.get("vpn_protocol"), "vpn_protocol").lower()
            curl = _required("curl")
            if protocol == "wireguard":
                connection = await resources.enter_async_context(_wireguard(data, profile, curl))
            elif protocol == "v2ray":
                connection = await resources.enter_async_context(_v2ray(data, profile, curl))
            else:
                raise ValueError("vpn_protocol must be wireguard or v2ray.")
            await connection.heartbeat()
        except NeedsAttention:
            raise
        except Exception as error:
            detail = f" {error}" if isinstance(error, VPNSetupError) else ""
            raise NeedsAttention(
                f"Profile '{profile.name}': could not connect to its configured VPN.{detail} "
                "Check profile.vpn, the connection file/link, and that xray and curl are installed on PATH."
            ) from None
        yield connection


@asynccontextmanager
async def browser_proxy(proxy):
    """Bridge native Brave to the authenticated VPN without browser APIs.

    Brave has no command-line proxy authentication. A short-lived loopback relay
    adds credentials only on the connection to Xray, never in browser arguments.
    """
    import base64
    from urllib.parse import urlsplit
    if proxy is None:
        raise VPNSetupError("VPN has no active browser proxy.")
    url = urlsplit(proxy["server"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
        raise VPNSetupError("VPN proxy must be an HTTP server on 127.0.0.1.")
    auth = base64.b64encode(f"{proxy['username']}:{proxy['password']}".encode()).decode()
    active = set()

    async def relay(reader, writer):
        task = asyncio.current_task()
        active.add(task)
        upstream = None
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = headers[:-4].split(b"\r\n")
            method = lines[0].split(b" ", 1)[0]
            # Never accept a caller's credentials or persist a plain HTTP socket
            # that could send subsequent requests without the upstream header.
            lines = [line for line in lines if not line.lower().startswith(
                (b"proxy-authorization:", b"proxy-connection:", b"connection:"))]
            lines.append(f"Proxy-Authorization: Basic {auth}".encode())
            if method != b"CONNECT":
                lines.append(b"Connection: close")
            remote, upstream = await asyncio.wait_for(asyncio.open_connection(url.hostname, url.port), 10)
            upstream.write(b"\r\n".join(lines) + b"\r\n\r\n")
            await upstream.drain()

            async def copy(source, destination):
                while block := await source.read(65536):
                    destination.write(block)
                    await destination.drain()

            pipes = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(remote, writer))]
            try:
                done, _ = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    finished.result()
            finally:
                for pipe in pipes:
                    pipe.cancel()
                await asyncio.gather(*pipes, return_exceptions=True)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # Browser navigation surfaces connection errors; never log credentials.
        finally:
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except (OSError, ConnectionError):
                        pass
            active.discard(task)

    server = await asyncio.start_server(relay, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        yield {"server": f"http://127.0.0.1:{port}"}
    finally:
        server.close()
        await server.wait_closed()
        tasks = list(active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
