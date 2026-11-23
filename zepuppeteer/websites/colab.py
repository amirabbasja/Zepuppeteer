"""Navigation and readiness checks for Google Colab's notebook editor."""

import re
import time
from urllib.parse import urlsplit

from ..models import NeedsAttention
from .notebooks import target_region, wait_for_title


DEFAULT_NOTEBOOK_URL = "https://colab.research.google.com/notebooks/intro.ipynb"


def validate_notebook_url(value: str) -> str:
    """Accept Colab HTTPS URLs, including Drive/GitHub paths and account queries."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("notebook_url must be a nonempty Google Colab HTTPS URL.")
    value = value.strip()
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname == "colab.research.google.com"
                 and url.port in (None, 443) and url.username is None and url.password is None)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("notebook_url must use https://colab.research.google.com without URL credentials.")
    return value


# Fractions of the browser window, not fixed click coordinates. Targets can
# override these search regions for the user's window layout and browser zoom.
TOOLBAR = (0, 0.08, 1, 0.28)
RUNTIME = (0.5, 0.08, 0.5, 0.3)


class ColabSite:
    def __init__(self, page, *, timeout_ms=45_000):
        self.page, self.timeout_ms = page, timeout_ms

    async def _check_attention(self, **_):
        host = urlsplit(await self.page.current_url()).hostname
        if host in {"accounts.google.com", "consent.google.com"}:
            raise NeedsAttention("Colab requires Google sign-in or consent. Complete it manually in this profile, then retry.")
        if host != "colab.research.google.com":
            raise RuntimeError(f"Colab redirected to an unexpected website: {self.page.url}")
        text = await self.page.read_text(TOOLBAR)
        if re.search(r"you need access|request access|access denied|permission denied", text, re.I):
            raise NeedsAttention("This profile needs permission to view the Colab notebook.")

    async def open_notebook(self, notebook_url):
        await self.page.goto(validate_notebook_url(notebook_url))
        deadline = time.monotonic() + self.timeout_ms / 1000
        toolbar = target_region(self.page, "colab_toolbar", TOOLBAR)
        try:
            await self.page.wait_for_text("Runtime", timeout=self.timeout_ms / 1000, region=toolbar)
            title = await wait_for_title(self.page, re.compile(r"\S.* - Colab$"), timeout=deadline - time.monotonic())
        except Exception:
            await self._check_attention()
            raise
        await self._check_attention()
        return {"notebook_url": self.page.url, "title": title}

    async def run_all_and_wait_for_last_cell(self, *, timeout_seconds=300):
        from ..screen import TargetNotFound
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds < float("inf"):
            raise ValueError("job_start_timeout_seconds must be a finite positive number.")
        # Require evidence for a specific final-cell running indicator BEFORE
        # starting work. Saved output or a changing timer alone is insufficient.
        targets = self.page.browser.targets
        for name in ("colab_last_cell_running", "colab_last_cell_anchor", "colab_notebook_idle"):
            target = targets.get(name, {})
            if not isinstance(target, dict) or not target.get("region") or not (target.get("image") or target.get("text")):
                raise NeedsAttention(f"Configure screen target {name} with an image/text and a tight region before using Run all.")
        await self.page.wait_for_target("colab_notebook_idle", timeout=10)
        await self.page.hotkey("ctrl", "end")
        await self.page.wait_for_target("colab_last_cell_anchor", timeout=10)
        try:
            await self.page.wait_for_target("colab_last_cell_running", timeout=0.1)
        except TargetNotFound:
            pass
        else:
            raise NeedsAttention("The last cell is already executing; Run all was not clicked.")
        await self.page.hotkey("ctrl", "home")
        await self.page.click_target("colab_run_all", text="Run all", region=TOOLBAR)
        await self.page.hotkey("ctrl", "end")
        await self.page.wait_for_target("colab_last_cell_anchor", timeout=10)
        await self.page.wait_for_target("colab_last_cell_running", timeout=timeout_seconds)
        return "last-visible-code-cell"
