"""Navigation and readiness checks for Google Colab's notebook editor."""

import asyncio
import re
from urllib.parse import urlsplit

from ..models import NeedsAttention


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


class ColabSite:
    def __init__(self, page, *, timeout_ms: int = 45_000):
        self.page = page
        self.timeout_ms = timeout_ms

    async def run_all_and_wait_for_last_cell(self, *, timeout_seconds: float = 300) -> str:
        """Click Run all once and require the last code cell to be executing."""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds < float("inf")):
            raise ValueError("job_start_timeout_seconds must be a finite positive number.")
        cells = self.page.locator(".cell.code")
        if not await cells.count():
            raise NeedsAttention("The notebook has no rendered code cells to run.")
        # Avoid restarting work that is already executing or queued.
        if await cells.locator("colab-run-button .cell-execution.running, "
                               "colab-run-button .cell-execution.pending").count():
            raise NeedsAttention("The notebook already has running or queued cells; inspect the existing job before Run all.")
        last_cell = cells.last
        cell_id = await last_cell.get_attribute("id")
        await last_cell.scroll_into_view_if_needed(timeout=self.timeout_ms)
        try:
            await self.page.get_by_role("button", name=re.compile(r"^Run all(?:\s|$)", re.I)).click(
                timeout=self.timeout_ms)
        except PlaywrightTimeoutError as error:
            await self._check_attention(check_dialogs=True)
            raise NeedsAttention("Could not click Colab's Run all button; inspect the notebook controls.") from error

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        running = last_cell.locator("colab-run-button .cell-execution.running")
        while asyncio.get_running_loop().time() < deadline:
            await self._check_attention(check_dialogs=True)
            if cell_id and await last_cell.get_attribute("id") != cell_id:
                raise NeedsAttention("The last code cell changed after Run all; inspect the notebook before retrying.")
            # The pending/queued marker and saved output do not prove execution.
            if await running.count():
                return cell_id or "last-code-cell"
            await asyncio.sleep(0.25)
        raise NeedsAttention(
            f"The last code cell did not start executing within {timeout_seconds:g} seconds after Run all. "
            "Inspect earlier cells for errors, long-running setup, or input requests."
        )

    async def _check_attention(self, *, check_dialogs=False) -> None:
        host = urlsplit(self.page.url).hostname
        if host in {"accounts.google.com", "consent.google.com"}:
            raise NeedsAttention("Colab requires Google sign-in or consent. Complete it manually in this profile, then retry.")
        if host != "colab.research.google.com":
            raise RuntimeError(f"Colab redirected to an unexpected website: {self.page.url}")
        # Public notebooks have a normal Sign in link; its presence is not an error.
        # Restrict text checks to dialogs so notebook contents cannot trigger them.
        permission = self.page.get_by_role("dialog").filter(has_text=re.compile(
            r"you need access|request access|access denied|permission denied|sign in to continue", re.I)).first
        if await permission.is_visible():
            raise NeedsAttention("This profile needs sign-in or permission to view the Colab notebook.")
        load_error = self.page.get_by_role("dialog").filter(has_text=re.compile(
            r"could not load notebook|unable to load notebook|notebook not found", re.I)).first
        if await load_error.is_visible():
            raise RuntimeError("Colab could not load the notebook; inspect the failure screenshot for details.")
        if check_dialogs and await self.page.get_by_role("dialog").first.is_visible():
            raise NeedsAttention("Colab is showing a dialog. Inspect the screenshot and complete it manually in this profile.")

    async def _wait_for_editor(self) -> None:
        from playwright.async_api import expect

        # Structural roles are independent of the account's UI language. A title
        # containing the notebook name distinguishes the loaded editor from its shell.
        await self.page.get_by_role("menubar").wait_for(state="visible", timeout=self.timeout_ms)
        await self.page.get_by_role("main").wait_for(state="visible", timeout=self.timeout_ms)
        await expect(self.page).to_have_title(re.compile(r"\S.* - Colab$"), timeout=self.timeout_ms)

    async def open_notebook(self, notebook_url: str) -> dict[str, str]:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        notebook_url = validate_notebook_url(notebook_url)
        try:
            response = await self.page.goto(notebook_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except PlaywrightError:
            # An interrupted/slow sign-in navigation is still a manual prerequisite.
            if urlsplit(self.page.url).hostname in {"accounts.google.com", "consent.google.com"}:
                await self._check_attention()
            raise
        await self._check_attention()
        if response is not None:
            if response.status in (401, 403):
                raise NeedsAttention(f"Colab returned HTTP {response.status}; check this profile's notebook access.")
            if response.status >= 400:
                raise RuntimeError(f"Colab returned HTTP {response.status} while opening the notebook.")
        try:
            # One readiness budget, rather than a fresh timeout for each locator.
            await asyncio.wait_for(self._wait_for_editor(), timeout=self.timeout_ms / 1000)
        except (asyncio.TimeoutError, PlaywrightTimeoutError, AssertionError) as error:
            await self._check_attention(check_dialogs=True)
            raise RuntimeError("The Colab notebook editor did not become ready; inspect the failure evidence.") from error
        await self._check_attention(check_dialogs=True)
        return {"notebook_url": self.page.url, "title": await self.page.title()}
