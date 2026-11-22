"""Navigation and readiness checks for Kaggle's notebook editor."""

import asyncio
import re
from urllib.parse import urlsplit

from ..models import NeedsAttention


KAGGLE_HOSTS = {"kaggle.com", "www.kaggle.com"}


def validate_notebook_url(value: str) -> str:
    """Accept existing Kaggle notebooks, preserving editor paths and queries."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("notebook_url must be a nonempty Kaggle HTTPS notebook URL.")
    value = value.strip()
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname in KAGGLE_HOSTS
                 and url.port in (None, 443) and url.username is None and url.password is None
                 and re.fullmatch(r"/code/[\w-]+/[\w-]+(?:/edit)?/?", url.path, re.ASCII))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "notebook_url must use https://www.kaggle.com/code/OWNER/NOTEBOOK "
            "(optionally /edit), without URL credentials."
        )
    return value


class KaggleSite:
    def __init__(self, page, *, timeout_ms: int = 45_000):
        self.page = page
        self.timeout_ms = timeout_ms

    async def _check_attention(self, *, check_dialogs=False) -> None:
        url = urlsplit(self.page.url)
        if (url.hostname in {"accounts.google.com", "consent.google.com"}
                or (url.hostname in KAGGLE_HOSTS and url.path.startswith("/account/"))):
            raise NeedsAttention(
                "Kaggle requires sign-in or account verification. Complete it manually in this profile, then retry."
            )
        if url.hostname not in KAGGLE_HOSTS:
            raise RuntimeError(f"Kaggle redirected to an unexpected website: {self.page.url}")
        permission = self.page.get_by_role("dialog").filter(has_text=re.compile(
            r"you need access|request access|access denied|permission denied|sign in|log in|verify your", re.I)).first
        if await permission.is_visible():
            raise NeedsAttention("This profile needs sign-in, verification, or permission to view the Kaggle notebook.")
        load_error = self.page.get_by_role("dialog").filter(has_text=re.compile(
            r"could not load notebook|unable to load notebook|notebook not found", re.I)).first
        if await load_error.is_visible():
            raise RuntimeError("Kaggle could not load the notebook; inspect the failure screenshot for details.")
        if check_dialogs and await self.page.get_by_role("dialog").first.is_visible():
            raise NeedsAttention("Kaggle is showing a dialog. Complete it manually in this profile, then retry.")

    async def _wait_for_editor(self) -> None:
        # A published notebook's rendered output is not an interactive editor.
        await self.page.get_by_role("button", name=re.compile(
            r"^Save Version(?:\s|$)", re.I)).first.wait_for(state="visible", timeout=self.timeout_ms)
        await self.page.locator(".jp-Notebook, .monaco-editor, .CodeMirror").first.wait_for(
            state="visible", timeout=self.timeout_ms)

    async def open_notebook(self, notebook_url: str) -> dict[str, str]:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        notebook_url = validate_notebook_url(notebook_url)
        # Existing notebook URLs often point at the published viewer. Open the
        # corresponding editor directly, without copying or creating a notebook.
        url = urlsplit(notebook_url)
        path = url.path.rstrip("/")
        if not path.endswith("/edit"):
            path += "/edit"
        editor_url = url._replace(path=path).geturl()
        try:
            response = await self.page.goto(editor_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except PlaywrightError:
            url = urlsplit(self.page.url)
            if (url.hostname in {"accounts.google.com", "consent.google.com"}
                    or (url.hostname in KAGGLE_HOSTS and url.path.startswith("/account/"))):
                await self._check_attention()
            raise
        await self._check_attention()
        if response is not None:
            if response.status in (401, 403):
                raise NeedsAttention(f"Kaggle returned HTTP {response.status}; check this profile's notebook access.")
            if response.status >= 400:
                raise RuntimeError(f"Kaggle returned HTTP {response.status} while opening the notebook.")
        try:
            await asyncio.wait_for(self._wait_for_editor(), timeout=self.timeout_ms / 1000)
        except (asyncio.TimeoutError, PlaywrightTimeoutError) as error:
            await self._check_attention(check_dialogs=True)
            raise RuntimeError("The Kaggle notebook editor did not become ready; inspect the failure evidence.") from error
        await self._check_attention(check_dialogs=True)
        return {"notebook_url": self.page.url, "title": await self.page.title()}
