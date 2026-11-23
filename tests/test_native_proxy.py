"""Native browser proxy authentication uses a loopback relay, not a driver."""
import asyncio
import base64
from pathlib import Path
import unittest

from zepuppeteer.brave import native_command
from zepuppeteer.models import Profile
from zepuppeteer.vpn import browser_proxy

class NativeProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_injects_upstream_auth_and_relays_tunnel_bytes(self):
        headers_seen = asyncio.Future()
        async def upstream(reader, writer):
            try:
                headers_seen.set_result(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                writer.write((await reader.readexactly(4)).upper())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        async with browser_proxy({"server": f"http://127.0.0.1:{port}", "username": "test", "password": "secret"}) as proxy:
            native = native_command("/brave", Profile("one", Path("/tmp/one")), proxy=proxy)
            self.assertFalse(any("secret" in flag or "test:" in flag for flag in native))
            relay_port = int(proxy["server"].rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
            writer.write(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test\r\nProxy-Authorization: caller\r\n\r\n")
            await writer.drain()
            self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2))
            writer.write(b"ping")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 2), b"PING")
            headers = await headers_seen
            self.assertIn(b"Proxy-Authorization: Basic " + base64.b64encode(b"test:secret"), headers)
            self.assertNotIn(b"caller", headers)
            writer.close()
            await writer.wait_closed()
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", relay_port)

    def test_proxy_rejects_external_or_credentialed_server(self):
        for server in ("http://outside.test:1234", "http://u:p@127.0.0.1:1234", "socks5://127.0.0.1:1234"):
            with self.subTest(server=server), self.assertRaises(ValueError):
                native_command("/brave", Profile("one", Path("/tmp/one")), proxy={"server": server})
