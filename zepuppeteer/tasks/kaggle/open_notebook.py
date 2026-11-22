"""Open a Kaggle notebook and verify its account, name, and runtime session."""

import asyncio
import re
import time
from urllib.parse import urljoin, urlsplit

from ...config import read_toml
from ...models import NeedsAttention, TaskContext, TaskResult
from ...websites.kaggle import KAGGLE_HOSTS, KaggleSite, validate_notebook_url


async def check_login(page, expected_username: str, timeout=15) -> str:
    """Read the account menu's profile link, never the notebook author's link."""
    account_navigation = page.get_by_role("navigation", name="Main menu, search and your account")
    account_button = account_navigation.get_by_role("button", name=re.compile(
        r"^(?:Your profile|Your account|Account menu|User menu|Profile)(?:\b|$)", re.I)).first
    account_image = account_navigation.get_by_role("img", name=re.compile(r"^Your profile$", re.I)).first
    profile_name = re.compile(r"^(?:Your|My) Profile$", re.I)
    profile_link = account_navigation.get_by_role("link", name=profile_name).or_(
        page.get_by_role("menu").locator("a[href]").filter(has_text=profile_name)).first
    sign_in = account_navigation.get_by_role("link", name=re.compile(r"^(?:Sign in|Log in|Login)$", re.I)).first
    deadline = time.monotonic() + timeout
    opened_menu = None
    try:
        while time.monotonic() < deadline:
            url = urlsplit(page.url)
            if (url.hostname in {"accounts.google.com", "consent.google.com"}
                    or (url.hostname in KAGGLE_HOSTS and url.path.startswith("/account/"))):
                return "logged_out"
            if await sign_in.is_visible():
                return "logged_out"
            if await profile_link.is_visible():
                profile_url = urlsplit(urljoin(page.url, await profile_link.get_attribute("href") or ""))
                if profile_url.hostname in KAGGLE_HOSTS:
                    username = profile_url.path.strip("/")
                    if re.fullmatch(r"[\w-]+", username, re.ASCII):
                        return "logged_in" if username.casefold() == expected_username.strip().casefold() else "wrong_account"
            if opened_menu is None:
                if await account_button.is_visible():
                    opened_menu = account_button
                elif await account_image.is_visible():
                    opened_menu = account_image
                if opened_menu is not None:
                    await opened_menu.click(timeout=1000)
            await asyncio.sleep(0.25)
        return "unknown"
    finally:
        if opened_menu is not None:
            await page.keyboard.press("Escape")


async def check_runtime_connection(page, timeout=15) -> str:
    """Inspect session controls without starting, reconnecting, or stopping a runtime."""
    def control(pattern):
        name = re.compile(pattern, re.I)
        return page.get_by_role("button", name=name).or_(page.get_by_role("menuitem", name=name)).first

    prefix = r"^(?:(?:Draft\s+)?Session\s*:?\s*)?"
    connecting = control(prefix + r"(?:Starting|Connecting|Reconnecting|Allocating|Initializing|Adding data)\b")
    disconnected = control(prefix + r"(?:Start(?:\s+Session)?|Connect|Reconnect|Disconnected|Stopped|Inactive|Off|Error)\b")
    connected = control(prefix + r"(?:Running|Active|Connected)\b|^Stop Session\b")
    session_menu = page.get_by_role("button", name=re.compile(r"^Session (?:options|menu)$", re.I)).first
    deadline = time.monotonic() + timeout
    state = "unknown"
    opened_menu = False
    try:
        while time.monotonic() < deadline:
            # Transitional and disconnected controls take precedence over stale state.
            if await connecting.is_visible():
                state = "connecting"
            elif await disconnected.is_visible() and await disconnected.is_enabled():
                state = "disconnected"
            elif await connected.is_visible() and await connected.is_enabled():
                return "connected"
            else:
                state = "unknown"
                # Some editor versions expose state only inside the session menu.
                # Open that menu to read it; never select a session action.
                if not opened_menu and await session_menu.is_visible():
                    await session_menu.click(timeout=1000)
                    opened_menu = True
            await asyncio.sleep(0.25)
        return state
    finally:
        if opened_menu:
            await page.keyboard.press("Escape")


async def run(ctx: TaskContext) -> TaskResult:
    settings = ctx.profile.settings.get("kaggle", {})
    if not isinstance(settings, dict):
        raise ValueError("Profile settings.kaggle must be a TOML table.")

    profile_data = read_toml(ctx.profile.manifest_path) if ctx.profile.manifest_path else {}
    kaggle_data = profile_data.get("profile", {}).get("platforms", {}).get("kaggle")
    if kaggle_data is None:
        return TaskResult(status="skipped", message=f"Kaggle platform data not found in profile '{ctx.profile.name}'.")
    if not isinstance(kaggle_data, dict):
        raise ValueError("Kaggle platform data must be a TOML table.")

    expected_username = kaggle_data.get("login_username")
    if not isinstance(expected_username, str) or not re.fullmatch(r"[\w-]+", expected_username.strip(), re.ASCII):
        raise ValueError("Kaggle platform data requires a Kaggle username in login_username (not an email address).")
    notebook_url = validate_notebook_url(kaggle_data.get("notebook_url"))
    expected_notebook_name = kaggle_data.get("notebook_name")
    if expected_notebook_name is None:
        raise NeedsAttention(f"Profile '{ctx.profile.name}' does not specify a notebook_name in its Kaggle platform data.")
    if not isinstance(expected_notebook_name, str) or not expected_notebook_name.strip():
        raise ValueError("Kaggle notebook_name must be a nonempty string.")

    page = await ctx.browser.new_page()
    await KaggleSite(page).open_notebook(notebook_url)
    login_status = await check_login(page, expected_username)
    if login_status != "logged_in":
        raise NeedsAttention(f"Profile '{ctx.profile.name}': Kaggle login check returned {login_status}.")

    notebook_name = re.sub(r"\s+(?:\||-)\s+Kaggle$", "", await page.title()).strip()
    if notebook_name != expected_notebook_name.strip():
        raise NeedsAttention(
            f"Notebook name mismatch for profile '{ctx.profile.name}': "
            f"expected {expected_notebook_name!r}, found {notebook_name!r}."
        )

    runtime_status = await check_runtime_connection(page)
    if runtime_status != "connected":
        raise NeedsAttention(
            f"Profile '{ctx.profile.name}': Kaggle runtime is {runtime_status}. "
            "Start the notebook session manually, then retry."
        )

    # On failure the page stays open for runner screenshots and trace collection.
    await page.close()
    return TaskResult(outputs={}, status="success", message="Kaggle notebook editor loaded and verified.")
