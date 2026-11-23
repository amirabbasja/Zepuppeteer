"""Colab workflows operated exclusively through visible screen controls."""

import asyncio
import re
import time
import random as random_module
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from ...config import read_toml
from ...models import NeedsAttention, TaskContext, TaskResult
from ...websites.notebooks import save_notebook_evidence, target_region, visual_runtime_state
from ...vpn import uses_vpn
from ...websites.colab import ColabSite, DEFAULT_NOTEBOOK_URL, validate_notebook_url, TOOLBAR, RUNTIME


async def check_login(page, expected_email, timeout=15):
    """Open the avatar and read the active account before any notebook action."""
    if urlsplit(await page.current_url()).hostname in {"accounts.google.com", "consent.google.com"}:
        return "logged_out"
    if await page.find_text("Sign in", region=TOOLBAR):
        return "logged_out"
    await page.click_target("google_account", timeout=timeout)
    try:
        region = target_region(page, "google_active_account", (0.5, 0.1, 0.5, 0.5))
        deadline = time.monotonic() + timeout
        while True:
            # Read only the popup. Its top email is the active account; a
            # matching secondary account below it must not pass this check.
            words = sorted(await page.words(region), key=lambda word: (word.box.y, word.box.x))
            emails = re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", " ".join(word.text for word in words))
            if emails:
                return "logged_in" if emails[0].casefold() == expected_email.strip().casefold() else "wrong_account"
            if time.monotonic() >= deadline:
                return "unknown"
            await asyncio.sleep(0.25)
    finally:
        await page.press("esc")


async def check_runtime_connection(page, timeout=15):
    region = target_region(page, "colab_runtime", RUNTIME)
    deadline, state = time.monotonic() + timeout, "unknown"
    while True:
        visual = await visual_runtime_state(page, "colab", ("restarting", "connecting", "disconnected", "connected"))
        text = "" if visual else await page.read_text(region)
        if visual:
            state = visual
            if state == "connected":
                return state
        elif re.search(r"\bRestarting\b", text, re.I):
            state = "restarting"
        elif re.search(r"\b(?:Connecting|Reconnecting|Allocating|Initializing)\b", text, re.I):
            state = "connecting"
        elif re.search(r"\b(?:Connect|Reconnect|Disconnected)\b", text, re.I):
            state = "disconnected"
        elif re.search(r"\bConnected\b", text, re.I) or (re.search(r"\bRAM\b", text) and re.search(r"\bDisk\b", text)):
            return "connected"
        else:
            state = "unknown"
        if time.monotonic() >= deadline:
            return state
        await asyncio.sleep(0.3)


async def get_last_cell_output(page, timeout=15):
    await page.hotkey("ctrl", "end")
    await page.wait_for_target("colab_last_cell_anchor", timeout=timeout)
    target = page.browser.targets.get("colab_last_cell_output", {})
    if "region" not in target:
        raise NeedsAttention("Configure colab_last_cell_output.region to read only the final cell's visible output. OCR cannot read off-screen or hidden text.")
    return await page.read_text(target["region"])


def _require_targets(page, *names):
    for name in names:
        target = page.browser.targets.get(name, {})
        if not target.get("image") and not target.get("text"):
            raise NeedsAttention(f"Configure settings.screen.targets.{name} before this notebook action.")


async def keepAlive(page, task_number=None, *, random=False):
    if type(random) is not bool:
        raise ValueError("random must be a boolean.")
    if random:
        if task_number is not None:
            raise ValueError("Specify task_number or random=True, not both.")
        task_number = random_module.choice((1, 2, 3, 4, 5))
    elif type(task_number) is not int or task_number not in (1, 2, 3, 4, 5):
        raise ValueError("task_number must be an integer from 1 to 5, or use random=True.")

    async def save():
        await page.click_target("colab_file", text="File", region=TOOLBAR)
        await page.click_target("colab_save", text=re.compile(r"^Save(?:\s+(?:Ctrl|Command).*)?$", re.I))
        # Do not report success until a visible saved-state signal appears.
        await page.wait_for_target("colab_saved", timeout=15)

    if task_number == 1:
        _require_targets(page, "colab_saved")
        await save()
    elif task_number == 2:
        _require_targets(page, "colab_connection_options")
        await page.click_target("colab_connection_options")
        await page.click_text("View resources")
        await page.wait_for_text("Resources", timeout=10)
    elif task_number in (3, 4):
        _require_targets(page, "colab_new_cell", "colab_delete_cell", "colab_saved")
        await page.click_target("colab_insert", text="Insert", region=TOOLBAR)
        await page.click_text("Code cell")
        await page.click_target("colab_new_cell")
        if task_number == 4:
            await page.type_text('print("status update")')
        await page.click_target("colab_delete_cell")
        await save()
    else:
        _require_targets(page, "colab_first_cell_editor", "colab_saved")
        await page.hotkey("ctrl", "home")
        await page.click_target("colab_first_cell_editor")
        source = await page.copy_editor_text()
        marker = re.search(r"^[^\S\r\n]*#[^\S\r\n]*Status[^\S\r\n]+update\b[^\r\n]*\r?$", source, re.I | re.M)
        if marker is None:
            raise NeedsAttention("The first code cell has no # Status update marker; no text was changed.")
        timestamp = datetime.now(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S %z")
        comment = (f"# Time: {timestamp} | CNN training: running good | "
                   f"percent done: {random_module.randint(0, 100)}% | epoches done : {random_module.randint(1000, 10000)}")
        updated = source[:marker.end()].removesuffix("\r") + "\n" + comment
        await page.type_text(updated)
        if await page.copy_editor_text() != updated:
            raise NeedsAttention("The editor text did not match after typing; inspect the cell before saving.")
        await page.press("right")
        await save()
    return task_number


def _parse_runtime_resources(text: str) -> dict:
    """
    Parses compute units and the estimated runtime duration from resource text.

    Args:
        text (str): Visible text from Colab's runtime resources panel.

    Returns:
        dict: name, remaining_quota in compute units, time_remaining in seconds,
            end_timestamp in Unix seconds, and end_time as Tehran ISO 8601 text.
            Unavailable values are None. End times are estimates, not guarantees.
    """
    quota_match = re.search(
        r"\bYou\s+currently\s+have\s+(zero|no|\d[\d,]*(?:\.\d+)?)\s+compute\s+units?\s+available\b",
        text, re.I,
    )
    quota = None
    if quota_match:
        amount = quota_match[1].lower()
        quota = 0.0 if amount in {"zero", "no"} else float(amount.replace(",", ""))

    duration_match = re.search(
        r"\bthis\s+runtime\s+may\s+last(?:\s+up\s+to)?\s+"
        r"((?:\d+(?:\.\d+)?\s*(?:days?|hours?|minutes?|seconds?)\b[\s,]*(?:and\s+)?)+)",
        text, re.I,
    )
    seconds = None
    end_timestamp = None
    end_time = None
    if duration_match:
        unit_seconds = {"day": 86400, "hour": 3600, "minute": 60, "second": 1}
        seconds = int(sum(
            float(amount) * unit_seconds[unit.lower().rstrip("s")]
            for amount, unit in re.findall(
                r"(\d+(?:\.\d+)?)\s*(days?|hours?|minutes?|seconds?)\b", duration_match[1], re.I,
            )
        ))
        timezone = ZoneInfo("Asia/Tehran")
        end_timestamp = int(datetime.now(timezone).timestamp()) + seconds
        end_time = datetime.fromtimestamp(end_timestamp, timezone).isoformat(timespec="seconds")
    return {
        "name": "google_colab",
        "remaining_quota": quota,
        "time_remaining": seconds,
        "end_timestamp": end_timestamp,
        "end_time": end_time,
    }


async def getRemainingQuota(page, timeout=15):
    if type(timeout) not in (int, float) or not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a finite positive number of seconds.")
    target = page.browser.targets.get("colab_resources", {})
    if "region" not in target:
        raise NeedsAttention("Configure colab_resources.region around the resources panel to exclude notebook output.")
    await keepAlive(page, task_number=2)
    deadline = time.monotonic() + timeout
    while True:
        result = _parse_runtime_resources(await page.read_text(target["region"]))
        if result["remaining_quota"] is not None and result["time_remaining"] is not None:
            return result
        if time.monotonic() >= deadline:
            if result["remaining_quota"] is not None or result["time_remaining"] is not None:
                return result
            raise NeedsAttention("Could not read compute units or duration in the visible resources panel.")
        await asyncio.sleep(0.3)


def print_state(profileName, txt, verbose=True):
    if verbose:
        print(f"[{profileName}] {txt}", flush=True)


async def run(ctx: TaskContext) -> TaskResult:
    """Verify the visible account/notebook, then connect or maintain its runtime."""
    settings = ctx.profile.settings.get("colab", {})
    if not isinstance(settings, dict):
        raise ValueError("Profile settings.colab must be a TOML table.")
    metadata = read_toml(ctx.profile.manifest_path) if ctx.profile.manifest_path else {}
    data = metadata.get("profile", {}).get("platforms", {}).get("colab")
    if data is None and not settings and not ctx.params:
        return TaskResult(status="skipped", message=f"Colab platform data not found in profile '{ctx.profile.name}'.")
    if data is not None and not isinstance(data, dict):
        raise ValueError("Colab platform data must be a TOML table.")
    data = data or {}
    expected_email = data.get("login_username")
    if not isinstance(expected_email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", expected_email.strip()):
        raise ValueError("Colab platform data requires an email address in login_username.")
    notebook_url = validate_notebook_url(data.get("notebook_url", data.get("worker_url",
        ctx.params.get("notebook_url", settings.get("notebook_url", DEFAULT_NOTEBOOK_URL)))))
    if "run_all" in ctx.params:
        raise ValueError("run_all is not a supported workflow parameter.")
    expected_name = data.get("notebook_name")
    if expected_name is None:
        raise NeedsAttention(f"Profile '{ctx.profile.name}' does not specify a notebook_name in its Colab platform data.")
    if not isinstance(expected_name, str) or not expected_name.strip():
        raise ValueError("Colab notebook_name must be a nonempty string.")
    auto_connect = data.get("auto_connect", False)
    if type(auto_connect) is not bool:
        raise ValueError("Colab auto_connect must be a boolean.")
    job_timeout = data.get("job_start_timeout_seconds", 300)
    if type(job_timeout) not in (int, float) or not 0 < job_timeout < float("inf"):
        raise ValueError("job_start_timeout_seconds must be a finite positive number.")
    vpn_required = uses_vpn(data)
    if vpn_required:
        if ctx.vpn is None or ctx.vpn.profile_name != ctx.profile.name:
            raise NeedsAttention(f"Profile '{ctx.profile.name}': VPN is not connected. Run the Colab workflow to connect before Brave starts.")
        await ctx.vpn.heartbeat()

    page = await ctx.browser.new_page()
    site = ColabSite(page)
    notebook = await site.open_notebook(notebook_url)
    login_status = await check_login(page, expected_email)
    if login_status != "logged_in":
        raise NeedsAttention(f"Profile '{ctx.profile.name}': Colab login check returned {login_status}.")
    name = (await page.title()).removesuffix(" - Colab").strip()
    if name != expected_name.strip():
        raise NeedsAttention(f"Notebook name mismatch for profile '{ctx.profile.name}': expected {expected_name!r}, found {name!r}.")

    runtime_status = await check_runtime_connection(page)
    existing_runtime = runtime_status == "connected"
    if runtime_status in {"disconnected", "connecting", "restarting"}:
        if runtime_status == "disconnected":
            if not auto_connect:
                raise NeedsAttention(f"Profile '{ctx.profile.name}': Colab runtime is disconnected. Set auto_connect = true to connect automatically.")
            await page.click_target("colab_connection_options")
            await page.click_target("colab_connect_hosted", text="Connect to a hosted runtime")
        runtime_status = await check_runtime_connection(page, timeout=300)
    if runtime_status != "connected":
        raise NeedsAttention(f"Profile '{ctx.profile.name}': Colab runtime is {runtime_status}; inspect the screen.")

    if existing_runtime:
        await keepAlive(page, task_number=5)
        state = {"runtime_status": "connected", "job_started": False, "action": "keep_alive"}
        message = "Existing Colab runtime verified and notebook saved."
    else:
        if vpn_required:
            await ctx.vpn.heartbeat()
        last_cell = await site.run_all_and_wait_for_last_cell(timeout_seconds=job_timeout)
        state = {"runtime_status": "connected", "job_started": True, "last_cell_id": last_cell, "action": "run_all"}
        message = "Colab notebook verified; last cell executing indicator observed."
    outputs = await save_notebook_evidence(ctx, page, "colab", notebook, notebook_url, **state)
    await page.close()
    return TaskResult(outputs=outputs, message=message)
