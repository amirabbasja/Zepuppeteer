"""Open a Kaggle notebook and verify its account, name, and runtime session."""

import asyncio
import re
import time
from urllib.parse import urlsplit

from ...config import read_toml
from ...models import NeedsAttention, TaskContext, TaskResult
from ...websites.kaggle import KAGGLE_HOSTS, KaggleSite, validate_notebook_url, TOOLBAR, RUNTIME
from ...websites.notebooks import save_notebook_evidence, target_region, visual_runtime_state


async def check_login(page, expected_username, timeout=15):
    """Verify the account menu's own profile URL, read through the address bar."""
    url = urlsplit(await page.current_url())
    if url.hostname in {"accounts.google.com", "consent.google.com"} or url.path.startswith("/account/"):
        return "logged_out"
    notebook_title = await page.title()
    await page.click_target("kaggle_account", timeout=timeout)
    navigated = False
    try:
        await page.click_target("kaggle_profile", text="Your Profile", timeout=timeout)
        deadline = time.monotonic() + timeout
        # Reading the address bar sends Escape afterward. Wait for the profile
        # navigation to commit first so Escape cannot cancel an in-flight load.
        while await page.title() == notebook_title:
            if time.monotonic() >= deadline:
                return "unknown"
            await asyncio.sleep(0.25)
        navigated = True
        while True:
            profile_url = urlsplit(await page.current_url())
            navigated = navigated or profile_url.geturl() != url.geturl()
            username = profile_url.path.strip("/")
            if profile_url.hostname in KAGGLE_HOSTS and re.fullmatch(r"[\w-]+", username, re.ASCII):
                return "logged_in" if username.casefold() == expected_username.strip().casefold() else "wrong_account"
            if profile_url.hostname in {"accounts.google.com", "consent.google.com"} or profile_url.path.startswith("/account/"):
                return "logged_out"
            if time.monotonic() >= deadline:
                return "unknown"
            await asyncio.sleep(0.25)
    finally:
        if navigated:
            await page.hotkey("alt", "left")
            await page.wait_for_text("Save Version", timeout=timeout, region=TOOLBAR)
        await page.press("esc")


async def check_runtime_connection(page, timeout=15):
    """Read visible session controls, opening the options menu only if needed."""
    region = target_region(page, "kaggle_runtime", RUNTIME)
    deadline, state, opened_menu = time.monotonic() + timeout, "unknown", False
    try:
        while True:
            visual = await visual_runtime_state(page, "kaggle", ("connecting", "disconnected", "connected"))
            text = "" if visual else await page.read_text(region)
            if visual:
                state = visual
                if state == "connected":
                    return state
            elif re.search(r"\b(?:Starting|Connecting|Reconnecting|Allocating|Initializing|Adding data)\b", text, re.I):
                state = "connecting"
            elif re.search(r"\b(?:Start Session|Connect|Reconnect|Disconnected|Stopped|Inactive|Off|Error)\b", text, re.I):
                state = "disconnected"
            elif re.search(r"\b(?:Running|Active|Connected|Stop Session)\b", text, re.I):
                return "connected"
            else:
                state = "unknown"
                if not opened_menu and "kaggle_session_options" in page.browser.targets:
                    await page.click_target("kaggle_session_options", timeout=max(0.1, deadline - time.monotonic()))
                    opened_menu = True
                    region = target_region(page, "kaggle_session_status", RUNTIME)
                    continue
            if time.monotonic() >= deadline:
                return state
            await asyncio.sleep(0.3)
    finally:
        if opened_menu:
            await page.press("esc")


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
    notebook = await KaggleSite(page).open_notebook(notebook_url)
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

    outputs = await save_notebook_evidence(ctx, page, "kaggle", notebook, notebook_url,
                                           runtime_status=runtime_status, action="verify")
    await page.close()
    return TaskResult(outputs=outputs, message="Kaggle notebook editor loaded and verified.")
