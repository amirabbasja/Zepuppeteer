"""Navigation and readiness checks for Kaggle's notebook editor."""

import re
import time
from urllib.parse import urlsplit

from ..models import NeedsAttention
from .notebooks import target_region, wait_for_title


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


TOOLBAR = (0, 0.08, 1, 0.32)
RUNTIME = (0.5, 0.08, 0.5, 0.35)


class KaggleSite:
    def __init__(self, page, *, timeout_ms=45_000):
        self.page, self.timeout_ms = page, timeout_ms

    async def _check_attention(self, **_):
        url = urlsplit(await self.page.current_url())
        if (url.hostname in {"accounts.google.com", "consent.google.com"}
                or (url.hostname in KAGGLE_HOSTS and url.path.startswith("/account/"))):
            raise NeedsAttention("Kaggle requires sign-in or account verification. Complete it manually, then retry.")
        if url.hostname not in KAGGLE_HOSTS:
            raise RuntimeError(f"Kaggle redirected to an unexpected website: {self.page.url}")

    async def open_notebook(self, notebook_url):
        url = urlsplit(validate_notebook_url(notebook_url))
        path = url.path.rstrip("/")
        if not path.endswith("/edit"):
            path += "/edit"
        await self.page.goto(url._replace(path=path).geturl())
        deadline = time.monotonic() + self.timeout_ms / 1000
        try:
            await self.page.wait_for_text("Save Version", timeout=self.timeout_ms / 1000,
                                          region=target_region(self.page, "kaggle_toolbar", TOOLBAR))
            title = await wait_for_title(self.page, re.compile(r"^(?!(?:Kaggle|Loading)(?:\.\.\.|…)?$)\S.+$", re.I), timeout=deadline - time.monotonic())
        except Exception:
            await self._check_attention()
            raise
        await self._check_attention()
        return {"notebook_url": self.page.url, "title": title}
