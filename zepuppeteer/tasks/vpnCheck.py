"""Probe a profile's VPN using a disposable tunnel, with no host route changes."""

import asyncio
import base64
import binascii
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from ..config import read_toml
from ..models import TaskContext, TaskResult


DEFAULT_URL = "https://1.1.1.1/cdn-cgi/trace"


def _required(name):
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"Required executable '{name}' is not installed or is not on PATH. Install it before vpnCheck.")
    return executable


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string.")
    return value.strip()


def _manifest(profile_name, profiles_dir):
    name = _text(profile_name, "Profile name")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Profile name must be a single directory name.")
    root = Path(profiles_dir).expanduser().resolve(strict=True)
    folder = root / name
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"Profile directory does not exist or is symlinked: {name}")
    manifest = folder / "profile.toml"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError(f"Profile '{name}' needs a regular profile.toml file.")
    return name, manifest


async def _stop(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _command(command, *, label, timeout=10, input=None, check=True, redact=None):
    # Only opt into stderr diagnostics when the caller supplies values to redact.
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if redact is not None else asyncio.subprocess.DEVNULL)
    try:
        output, errors = await asyncio.wait_for(process.communicate(input), timeout)
    except asyncio.TimeoutError:
        await _stop(process)
        raise RuntimeError(f"{label} timed out after {timeout:g} seconds.") from None
    except BaseException:
        await _stop(process)
        raise
    if check and process.returncode:
        detail = ""
        if redact is not None and errors:
            detail = errors.decode("utf-8", errors="replace")
            for secret in sorted(set(redact), key=len, reverse=True):
                if secret:
                    detail = detail.replace(secret, "[redacted]")
            detail = re.sub(r"[A-Za-z0-9+/]{43}=", "[redacted key]", detail)
            detail = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", detail).strip()[:1200]
        suffix = f" {detail}" if detail else ""
        raise RuntimeError(f"{label} failed (exit {process.returncode}).{suffix}")
    return process.returncode, output.decode("utf-8", errors="replace")


async def _wireguard_privileges():
    if os.geteuid() == 0:
        return []
    sudo = _required("sudo")
    code, _ = await _command([sudo, "-n", "-v"], label="Check WireGuard sudo authorization", check=False)
    if code:
        if not sys.stdin.isatty():
            raise RuntimeError("WireGuard needs sudo authorization. Run 'sudo -v' in the same terminal, "
                               "then retry vpnCheck. Unattended runs need sudo authorization configured in advance.")
        print("WireGuard needs administrator privileges to create its temporary network namespace. "
              "Authenticate at the sudo prompt:", flush=True)
        # Inherit the terminal so sudo handles the password itself. Never pipe,
        # capture, or store passwords, and never prompt from unattended runs.
        process = await asyncio.create_subprocess_exec(sudo, "-v")
        try:
            code = await process.wait()
        except BaseException:
            await _stop(process)
            raise
        if code:
            raise RuntimeError("WireGuard sudo authentication failed or permission was denied. "
                               "Run 'sudo -v' in your terminal to check access, then retry vpnCheck.")
    return [sudo, "-n"]


def _decode64(value):
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("Invalid base64 data in v2ray_link.") from None


def _port(value):
    try:
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError
        return port
    except (TypeError, ValueError):
        raise ValueError("v2ray_link needs a port between 1 and 65535.") from None


def _stream(query, *, default_security="none"):
    network = query.get("type", "tcp")
    if network not in {"tcp", "raw", "ws", "grpc", "httpupgrade", "xhttp"}:
        raise ValueError("Unsupported v2ray_link transport; use tcp, raw, ws, grpc, httpupgrade, or xhttp.")
    security = query.get("security", default_security) or default_security
    if security not in {"none", "tls", "reality"}:
        raise ValueError("Unsupported v2ray_link security; use none, tls, or reality.")
    if any(str(query.get(key, "")).lower() not in {"", "0", "false"} for key in ("allowInsecure", "insecure")):
        raise ValueError("vpnCheck requires TLS certificate verification; insecure links are not supported.")
    stream = {"network": network, "security": security}
    if network in {"ws", "httpupgrade", "xhttp"}:
        settings = {"path": query.get("path", "/")}
        if query.get("host"):
            if network == "ws":
                settings["headers"] = {"Host": query["host"]}
            else:
                settings["host"] = query["host"]
        if network == "xhttp" and query.get("mode"):
            settings["mode"] = query["mode"]
        stream[f"{network}Settings"] = settings
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": query.get("serviceName", "")}
    elif query.get("headerType", "none") != "none":
        raise ValueError("vpnCheck does not support TCP header obfuscation.")
    if security in {"tls", "reality"}:
        settings = {}
        for source, target in (("sni", "serverName"), ("fp", "fingerprint")):
            if query.get(source):
                settings[target] = query[source]
        if security == "tls" and query.get("alpn"):
            settings["alpn"] = query["alpn"].split(",")
        if security == "reality":
            settings.update(publicKey=_text(query.get("pbk"), "REALITY public key (pbk)"),
                            shortId=query.get("sid", ""), fingerprint=query.get("fp", "chrome"))
            if query.get("spx"):
                settings["spiderX"] = query["spx"]
        stream[f"{security}Settings"] = settings
    return stream


def _outbound(link):
    link = _text(link, "v2ray_link")
    try:
        parsed = urlsplit(link)
        if parsed.scheme == "vmess":
            data = json.loads(_decode64(link[len("vmess://"):].partition("#")[0]))
            if not isinstance(data, dict):
                raise ValueError
            query = {"type": data.get("net", "tcp"), "security": data.get("tls", "none"),
                     **{key: data[key] for key in ("host", "path", "sni", "alpn", "fp", "allowInsecure") if key in data}}
            if query["type"] == "grpc":
                query["serviceName"] = data.get("path", "")
            if data.get("type", "none") not in {"", "none"}:
                raise ValueError("Unsupported VMess header type.")
            server = {"address": _text(data.get("add"), "VMess address"), "port": _port(data.get("port")),
                      "users": [{"id": _text(data.get("id"), "VMess id"),
                                 "alterId": int(data.get("aid", 0)), "security": data.get("scy", "auto")}]}
            return {"protocol": "vmess", "settings": {"vnext": [server]}, "streamSettings": _stream(query)}
        if parsed.scheme not in {"vless", "trojan", "ss"}:
            raise ValueError("Unsupported v2ray_link; use vless://, vmess://, trojan://, or ss://.")
        if parsed.scheme == "ss" and not parsed.hostname:
            raise ValueError("Use SIP002 ss://base64(method:password)@host:port links.")
        query_lists = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query_lists.values()):
            raise ValueError("Duplicate v2ray_link query parameters are not supported.")
        query = {key: values[0] for key, values in query_lists.items()}
        server = {"address": _text(parsed.hostname, "VPN server address"), "port": _port(parsed.port)}
        credential = _text(unquote(parsed.username or ""), "VPN credential")
        if parsed.scheme == "ss":
            if query.get("plugin"):
                raise ValueError("Shadowsocks plugins are not supported by vpnCheck.")
            credentials = f"{credential}:{unquote(parsed.password)}" if parsed.password is not None else _decode64(credential)
            method, separator, password = credentials.partition(":")
            if not separator or not method or not password:
                raise ValueError("Invalid Shadowsocks credentials.")
            server.update(method=method, password=password)
            return {"protocol": "shadowsocks", "settings": {"servers": [server]}}
        if parsed.password is not None:
            raise ValueError("Encode ':' in the VPN credential as %3A.")
        if parsed.scheme == "vless":
            user = {"id": credential, "encryption": query.get("encryption", "none")}
            if query.get("flow"):
                user["flow"] = query["flow"]
            server["users"] = [user]
            settings = {"vnext": [server]}
        else:
            server["password"] = credential
            settings = {"servers": [server]}
        return {"protocol": parsed.scheme, "settings": settings,
                "streamSettings": _stream(query, default_security="tls" if parsed.scheme == "trojan" else "none")}
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        # urllib/json errors may contain credentials. Keep public errors generic.
        raise ValueError("Invalid or unsupported v2ray_link. Use a supported share link and transport; TLS verification is required.") from None


async def _probe(curl, url, timeout, *, prefix=(), proxy=None):
    command = [*prefix, curl, "--disable", "--silent", "--fail", "--output", os.devnull,
               "--max-time", str(timeout), "--connect-timeout", str(timeout),
               "--proto", "=https", "--write-out", "%{time_total}"]
    # --disable ignores ~/.curlrc; explicit proxy settings prevent environment bypass.
    if proxy:
        address, credentials = proxy
        command += ["--proxy", address, "--proxy-user", credentials, "--noproxy", ""]
    else:
        command += ["--proxy", "", "--noproxy", "*"]
    _, output = await _command([*command, "--url", url], label="HTTPS probe through VPN", timeout=timeout + 2)
    try:
        latency = float(output) * 1000
        if not math.isfinite(latency) or latency < 0:
            raise ValueError
        return round(latency, 2)
    except ValueError:
        raise RuntimeError("VPN probe returned an invalid latency.") from None


async def _xray(link, curl, url, timeout):
    xray = _required("xray")
    outbound = _outbound(link)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    password = uuid4().hex
    config = {"log": {"loglevel": "none"},
              "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks",
                            "settings": {"auth": "password", "accounts": [{"user": "vpncheck", "pass": password}],
                                         "udp": False}}], "outbounds": [outbound]}
    with tempfile.TemporaryDirectory(prefix="zepuppeteer-vpncheck-") as directory:
        path = Path(directory) / "xray.json"
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            json.dump(config, stream)
        await _command([xray, "run", "-test", "-c", str(path)], label="Xray config validation (check link and installed Xray version)")
        process = await asyncio.create_subprocess_exec(
            xray, "run", "-c", str(path), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            deadline = asyncio.get_running_loop().time() + min(timeout, 5)
            while True:
                if process.returncode is not None:
                    raise RuntimeError("Xray exited before the connectivity check.")
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.2)
                    writer.close()
                    await writer.wait_closed()
                    break
                except (OSError, asyncio.TimeoutError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError("Xray did not start its local SOCKS listener.") from None
                    await asyncio.sleep(0.05)
            latency = await _probe(curl, url, timeout,
                                   proxy=(f"socks5h://127.0.0.1:{port}", f"vpncheck:{password}"))
            if process.returncode is not None:
                raise RuntimeError("Xray exited during the connectivity check.")
            return latency, None
        finally:
            await _stop(process)


def _wireguard_config(path):
    """Extract native wg settings; never execute wg-quick hooks or change DNS."""
    sections, current = [], None
    addresses, mtu, networks = [], 1420, []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        if line in {"[Interface]", "[Peer]"}:
            current = (line, [])
            sections.append(current)
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if current is None or not separator or not value:
            raise ValueError("Invalid WireGuard configuration.")
        if current[0] == "[Interface]":
            if key == "Address":
                addresses.extend(str(ipaddress.ip_interface(item.strip())) for item in value.split(","))
            elif key == "MTU":
                mtu = int(value)
                if not 1280 <= mtu <= 65535:
                    raise ValueError("WireGuard MTU must be between 1280 and 65535.")
            elif key == "PrivateKey":
                current[1].append(f"{key} = {value}")
            elif key not in {"ListenPort", "FwMark", "DNS", "Table", "SaveConfig", "PreUp", "PostUp", "PreDown", "PostDown"}:
                raise ValueError("Unsupported WireGuard interface setting.")
        else:
            if key not in {"PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"}:
                raise ValueError("Unsupported WireGuard peer setting.")
            if key == "AllowedIPs":
                networks.extend(ipaddress.ip_network(item.strip(), strict=False) for item in value.split(","))
            current[1].append(f"{key} = {value}")
    if ([section[0] for section in sections].count("[Interface]") != 1
            or not sections or sections[0][0] != "[Interface]" or len(sections) < 2
            or not addresses or not networks or not any(line.startswith("PrivateKey =") for line in sections[0][1])):
        raise ValueError("WireGuard config needs an Interface with PrivateKey and Address, plus a Peer with AllowedIPs.")
    return ("\n".join(line for header, lines in sections for line in [header, *lines]) + "\n").encode(), addresses, mtu, networks


async def _resolve_wireguard_endpoint(host, port, timeout):
    # Bound libc DNS with a child process, so a stuck lookup cannot keep Python's
    # default executor alive after the probe times out.
    resolver = ("import json,socket,sys; "
                "addresses=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_DGRAM); "
                "print(json.dumps(addresses[0][4][0]))")
    try:
        _, output = await _command([sys.executable, "-c", resolver, host, str(port)],
                                   label="Resolve WireGuard Endpoint hostname (check host DNS)",
                                   timeout=min(timeout, 10))
        return str(ipaddress.ip_address(json.loads(output)))
    except (RuntimeError, ValueError, TypeError):
        raise RuntimeError("Cannot resolve WireGuard Endpoint hostname. Check host DNS or use "
                           "the VPN provider's numeric Endpoint IP in wireguard_file.") from None


async def _prepare_wireguard_config(config, timeout):
    """Validate keys and resolve endpoints before sudo or any network mutations."""
    lines, secrets = [], []
    for line in config.decode("utf-8").splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key in {"PrivateKey", "PublicKey", "PresharedKey"}:
            try:
                decoded = base64.b64decode(value, validate=True)
                valid = len(decoded) == 32 and base64.b64encode(decoded).decode() == value
            except (ValueError, binascii.Error):
                valid = False
            if not valid:
                raise ValueError(f"WireGuard {key} must be a valid base64-encoded 32-byte key.")
            secrets.append(value)
        elif key == "Endpoint":
            try:
                endpoint = urlsplit("//" + value)
                host, port = endpoint.hostname, endpoint.port
                if (not host or port is None or not 1 <= port <= 65535
                        or endpoint.username or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment):
                    raise ValueError
            except ValueError:
                raise ValueError("WireGuard Endpoint must be host:port or [IPv6]:port.") from None
            try:
                ipaddress.ip_address(host)
            except ValueError:
                host = await _resolve_wireguard_endpoint(host, port, timeout)
            address = f"[{host}]" if ":" in host else host
            line = f"Endpoint = {address}:{port}"
        lines.append(line)
    return ("\n".join(lines) + "\n").encode(), tuple(secrets)


async def _wireguard(path, curl, url, timeout):
    if sys.platform != "linux":
        raise RuntimeError("Isolated WireGuard checks currently require Linux network namespaces.")
    wg, ip = _required("wg"), _required("ip")
    install, mkdir, remove = _required("install"), _required("mkdir"), _required("rm")
    ping = shutil.which("ping")
    try:
        config, addresses, mtu, networks = _wireguard_config(path)
    except (ValueError, OSError):
        raise ValueError("Cannot read a valid WireGuard config with PrivateKey, Address and peer AllowedIPs.") from None
    # A numeric probe URL avoids DNS changes and DNS leaks from the isolated namespace.
    try:
        target = ipaddress.ip_address(urlsplit(url).hostname)
    except ValueError:
        raise ValueError("WireGuard --url must use a numeric IP host, e.g. https://1.1.1.1/cdn-cgi/trace.") from None
    if not any(target.version == network.version and target in network for network in networks):
        raise ValueError("WireGuard AllowedIPs do not include the probe IP; choose a matching --url.")
    if not any(ipaddress.ip_interface(address).version == target.version for address in addresses):
        raise ValueError("WireGuard config needs an Address matching the probe IP family.")
    config, secrets = await _prepare_wireguard_config(config, timeout)
    prefix = await _wireguard_privileges()
    name = "zv" + uuid4().hex[:12]
    config_path = f"/etc/wireguard/.zepuppeteer-{name}.conf"
    resources = {"namespace": False, "interface": False, "moved": False, "config": False}

    async def change(command, label, resource=None, present=True, **kwargs):
        # Let short mutations finish before honoring cancellation, so ownership is
        # recorded even if Ctrl+C arrives just after the kernel creates/moves it.
        async def operation():
            await _command(command, label=label, **kwargs)
            if resource:
                resources[resource] = present
        operation_task = asyncio.create_task(operation())
        try:
            await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            try:
                await operation_task
            except Exception:
                pass
            raise

    try:
        await change([*prefix, ip, "netns", "add", name], "Create isolated WireGuard namespace (requires CAP_NET_ADMIN)", "namespace")
        await change([*prefix, ip, "link", "add", name, "type", "wireguard"], "Create temporary WireGuard interface", "interface")
        # The wg AppArmor profile permits configuration in /etc/wireguard.
        # mkdir -p preserves existing permissions; install creates a root-owned
        # mode-600 file, without exposing the key during file creation.
        await change([*prefix, mkdir, "-p", "-m", "700", "/etc/wireguard"], "Prepare WireGuard configuration directory")
        resources["config"] = True  # Remove a partially written file on failure too.
        await change([*prefix, install, "-m", "600", "-T", "/dev/stdin", config_path],
                     "Stage private WireGuard configuration", input=config, redact=secrets)
        await change([*prefix, wg, "setconf", name, config_path], "Configure temporary WireGuard interface", redact=secrets)
        await change([*prefix, remove, "-f", "--", config_path], "Remove temporary WireGuard configuration", "config", present=False)
        await change([*prefix, ip, "link", "set", name, "netns", name], "Isolate WireGuard interface", "moved")
        local = [*prefix, ip, "-n", name]
        await _command([*local, "link", "set", "lo", "up"], label="Enable isolated loopback")
        for address in addresses:
            options = ["nodad"] if ipaddress.ip_interface(address).version == 6 else []
            await _command([*local, "address", "add", address, "dev", name, *options], label="Set isolated WireGuard address")
        await _command([*local, "link", "set", "dev", name, "mtu", str(mtu), "up"], label="Enable isolated WireGuard interface")
        await _command([*local, f"-{target.version}", "route", "add", f"{target}/{target.max_prefixlen}", "dev", name],
                       label="Route probe only through WireGuard")
        execute = [*prefix, ip, "netns", "exec", name]
        latency = await _probe(curl, url, timeout, prefix=execute)
        ping_ms = None
        if ping:
            code, output = await _command([*execute, ping, "-n", "-c", "3", "-W", "1", "-w", "4", str(target)],
                                           label="WireGuard ping", timeout=5, check=False)
            match = re.search(r"=\s*[\d.]+/([\d.]+)/", output)
            if code == 0 and match:
                ping_ms = float(match[1])
        return latency, ping_ms
    finally:
        # Only remove resources created by this invocation; never touch existing VPNs.
        failed = []
        cleanup = []
        if resources["config"]:
            cleanup.append(("config", [*prefix, remove, "-f", "--", config_path]))
        if resources["interface"]:
            command = [*prefix, ip, "-n", name, "link", "delete", name] if resources["moved"] else [*prefix, ip, "link", "delete", name]
            cleanup.append(("interface", command))
        if resources["namespace"]:
            cleanup.append(("namespace", [*prefix, ip, "netns", "delete", name]))
        for resource, command in cleanup:
            try:
                code, _ = await _command(command, label=f"Delete temporary WireGuard {resource}", check=False)
                if code:
                    failed.append(resource)
            except Exception:
                failed.append(resource)
        if failed:
            raise RuntimeError(f"Could not remove temporary WireGuard {' and '.join(failed)} '{name}'; clean it up with sudo ip.")


async def _check(profile_name, manifest, *, timeout=15, url=DEFAULT_URL):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("VPN timeout must be a finite number greater than 0 and at most 300 seconds.")
    url = _text(url, "Probe URL")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Probe URL must be HTTPS with a host and no credentials.")
    metadata = read_toml(manifest)
    profile_data = metadata.get("profile", {})
    if not isinstance(profile_data, dict):
        raise ValueError("profile must be a TOML table.")
    if "vpn" in profile_data:
        data = profile_data["vpn"]
    elif "vpn_protocol" in profile_data:
        data = profile_data
    else:
        data = metadata
    if not isinstance(data, dict):
        raise ValueError("profile.vpn must be a TOML table.")
    protocol = _text(data.get("vpn_protocol"), "vpn_protocol").lower()
    if protocol not in {"wireguard", "v2ray"}:
        raise ValueError("vpn_protocol must be wireguard or v2ray.")
    curl = _required("curl")
    if protocol == "wireguard":
        path = Path(_text(data.get("wireguard_file"), "wireguard_file")).expanduser()
        if not path.is_absolute():
            path = manifest.parent / path
        if not path.is_file():
            raise ValueError("wireguard_file does not point to a readable configuration file.")
        latency, ping_ms = await _wireguard(path, curl, url, timeout)
    else:
        latency, ping_ms = await _xray(_text(data.get("v2ray_link"), "v2ray_link"), curl, url, timeout)
    print(f"{profile_name}: {protocol} connected; HTTPS latency: {latency:.2f} ms", flush=True)
    if ping_ms is not None:
        print(f"Ping average: {ping_ms:.2f} ms", flush=True)
    else:
        reason = "ICMP is not carried by the SOCKS proxy" if protocol == "v2ray" else "ping missing or ICMP blocked"
        print(f"Ping unavailable ({reason}).", flush=True)
    return TaskResult(outputs={"profile": profile_name, "vpn_protocol": protocol, "connected": True,
                               "latency_ms": latency, "ping_ms": ping_ms})


async def vpnCheck(profile_name: str, *, profiles_dir="./profiles", timeout=15, url=DEFAULT_URL) -> TaskResult:
    """Check a named profile's actual tunnel and print HTTPS latency and optional ping."""
    name, manifest = _manifest(profile_name, profiles_dir)
    return await _check(name, manifest, timeout=timeout, url=url)


async def run(ctx: TaskContext) -> TaskResult:
    """Workflow entry point, using the current profile's manifest."""
    if ctx.profile.manifest_path is None:
        raise ValueError(f"Profile '{ctx.profile.name}' needs profile.toml VPN settings.")
    return await _check(ctx.profile.name, ctx.profile.manifest_path,
                        timeout=ctx.params.get("timeout", 15), url=ctx.params.get("url", DEFAULT_URL))
