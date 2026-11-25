"""Colab must verify required VPN connectivity before notebook navigation."""

import asyncio
import contextlib
from dataclasses import replace
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zepuppeteer.models import NeedsAttention, Profile, TaskContext
from zepuppeteer.tasks.colab.open_notebook import run


MODULE = "zepuppeteer.tasks.colab.open_notebook"


class ColabVPNTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-colab-vpn-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "profile.toml"
        self.write_manifest("true")
        self.page = SimpleNamespace(url="https://colab.research.google.com/drive/test")
        self.browser = SimpleNamespace(new_page=AsyncMock(return_value=self.page))
        self.ctx = TaskContext(self.browser, Profile("test", self.root / "src", manifest_path=self.manifest),
                               {"verbose": False}, {}, self.root, "run", "colab.open_notebook")
        self.vpn = AsyncMock(return_value=12.5)
        self.browser._zepuppeteer_vpn_route = SimpleNamespace(check_connection=self.vpn)
        # Stop at navigation so these tests cannot start a runtime or run cells.
        self.navigation = NeedsAttention("Notebook navigation reached.")
        self.site = SimpleNamespace(open_notebook=AsyncMock(side_effect=self.navigation))
        self.enterContext(patch(f"{MODULE}.ColabSite", return_value=self.site))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def write_manifest(self, flag):
        self.manifest.write_text(
            '[profile.vpn]\nvpn_protocol="v2ray"\nv2ray_link="fixture"\n'
            '[profile.platforms.colab]\nlogin_username="account@example.test"\n'
            'notebook_url="https://colab.research.google.com/drive/test"\n'
            'notebook_name="test"\n' + (f'use_vpn={flag}\n' if flag is not None else ""))

    async def test_connected_vpn_is_checked_before_navigation_with_current_manifest(self):
        async def connected():
            self.browser.new_page.assert_not_awaited()
            return 12.5
        self.vpn.side_effect = connected
        ctx = replace(self.ctx, params={"verbose": False, "url": "not-a-vpn-probe-url", "timeout": 0})
        with self.assertRaises(NeedsAttention) as raised:
            await run(ctx)
        self.assertIs(raised.exception, self.navigation)
        self.vpn.assert_awaited_once()
        self.site.open_notebook.assert_awaited_once_with("https://colab.research.google.com/drive/test")

    async def test_disabled_or_unspecified_vpn_is_not_checked(self):
        for flag in ("false", None):
            with self.subTest(flag=flag):
                self.write_manifest(flag)
                with self.assertRaises(NeedsAttention) as raised:
                    await run(self.ctx)
                self.assertIs(raised.exception, self.navigation)
        self.vpn.assert_not_awaited()

    async def test_browser_without_active_vpn_cannot_navigate(self):
        self.browser._zepuppeteer_vpn_route = None
        with self.assertRaisesRegex(NeedsAttention, "browser was not launched through its VPN"):
            await run(self.ctx)
        self.browser.new_page.assert_not_awaited()
        self.vpn.assert_not_awaited()

    async def test_probe_and_configuration_errors_require_attention_before_navigation(self):
        for error in (RuntimeError("VPN request timed out"), ValueError("Missing VPN configuration"),
                      OSError("xray is missing"), TimeoutError("Probe timed out")):
            with self.subTest(error=error):
                self.vpn.side_effect = error
                with self.assertRaisesRegex(NeedsAttention, "VPN connectivity check failed") as raised:
                    await run(self.ctx)
                self.assertIs(raised.exception.__cause__, error)
        self.browser.new_page.assert_not_awaited()

    async def test_invalid_flag_is_rejected_before_probe_or_navigation(self):
        for flag in ('"false"', "1"):
            with self.subTest(flag=flag):
                self.write_manifest(flag)
                with self.assertRaisesRegex(ValueError, "use_vpn must be a boolean"):
                    await run(self.ctx)
        self.vpn.assert_not_awaited()
        self.browser.new_page.assert_not_awaited()

    async def test_probe_cancellation_propagates_without_navigation(self):
        self.vpn.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await run(self.ctx)
        self.browser.new_page.assert_not_awaited()

    async def test_profile_level_vpn_flag_also_requires_routed_browser(self):
        self.write_manifest("false")
        self.manifest.write_text(self.manifest.read_text().replace('[profile.vpn]', '[profile]\nuse_vpn=true\n[profile.vpn]'))
        self.browser._zepuppeteer_vpn_route = None
        with self.assertRaisesRegex(NeedsAttention, "browser was not launched through its VPN"):
            await run(self.ctx)
        self.browser.new_page.assert_not_awaited()
