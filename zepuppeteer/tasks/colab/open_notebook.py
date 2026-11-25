"""Open a Colab notebook, verify its editor, and save a screenshot and metadata."""

import json
import time
import re
import asyncio
import random as random_module
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ...config import read_toml
from ...models import NeedsAttention, TaskContext, TaskResult
from ...websites.colab import ColabSite, DEFAULT_NOTEBOOK_URL, validate_notebook_url
from ...vpn import vpn_required


async def check_login(page, expected_email: str, timeout=15) -> str:
    """
    Checks whether Colab is signed in with the expected Google account.

    Args:
        page (Page): The Playwright page containing the Colab notebook.
        expected_email (str): The Google account email to verify.
        timeout (float): Maximum number of seconds to check the login status.

    Returns:
        str: logged_in, logged_out, wrong_account, or unknown.
    """
    account = page.locator(
        'a[aria-label^="Google Account:"], '
        'button[aria-label^="Google Account:"]'
    ).first

    sign_in_link = page.get_by_role("link", name="Sign in", exact=True).first
    sign_in_button = page.get_by_role("button", name="Sign in", exact=True).first

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        # Google's sign-in page means logged out in every UI language; do not
        # depend on translated text such as "Sign in" or "Use your Google Account".
        if urlsplit(page.url).hostname == "accounts.google.com":
            return "logged_out"

        # Verify which account is active.
        if await account.is_visible():
            label = await account.get_attribute("aria-label") or ""
            emails = re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", label)

            if emails:
                matches = expected_email.strip().casefold() in {
                    email.casefold() for email in emails
                }
                return "logged_in" if matches else "wrong_account"

        # Explicit signed-out UI.
        if (
            await sign_in_link.is_visible()
            or await sign_in_button.is_visible()
        ):
            return "logged_out"

        await asyncio.sleep(0.25)

    # No reliable signal appeared.
    return "unknown"


async def check_runtime_connection(page, timeout=15) -> str:
    """
    Checks the runtime toolbar without starting or reconnecting a runtime.

    Args:
        page (Page): The Playwright page containing the Colab notebook.
        timeout (float): Maximum number of seconds to check the runtime status.

    Returns:
        str: connected, disconnected, connecting, restarting, or unknown followed
            by JSON debugging details about the page and runtime toolbar.
    """
    toolbar = page.locator("colab-connect-button").first
    connected = toolbar.get_by_role("button", name=re.compile(r"^Connected(?:\s|$)", re.I)).first
    ram = toolbar.get_by_text(re.compile(r"^RAM$", re.I)).first
    disk = toolbar.get_by_text(re.compile(r"^Disk$", re.I)).first
    connect = toolbar.get_by_role("button", name=re.compile(r"^(?:Connect|Reconnect)(?:\s|$)", re.I)).first
    connecting = toolbar.get_by_role("button", name=re.compile(r"^(?:Connecting|Reconnecting|Allocating|Initializing)", re.I)).first
    restarting = toolbar.get_by_role("button", name=re.compile(r"^Restarting", re.I)).first

    deadline = time.monotonic() + timeout
    state = "unknown"
    while time.monotonic() < deadline:
        if await toolbar.is_visible():
            # Check transitional states first: stale resource meters can remain
            # visible briefly after a runtime starts disconnecting/reconnecting.
            if await restarting.is_visible():
                state = "restarting"
            elif await connecting.is_visible():
                state = "connecting"
            elif await connect.is_visible():
                state = "disconnected"
            elif await connected.is_visible() or (await ram.is_visible() and await disk.is_visible()):
                return "connected"
            else:
                state = "unknown"
        else:
            state = "unknown"
        await asyncio.sleep(0.25)
    if state == "unknown":
        diagnostics = {"url": page.url}
        try:
            diagnostics["toolbar_count"] = await page.locator("colab-connect-button").count()
            diagnostics["toolbar_visible"] = await toolbar.is_visible()
            if diagnostics["toolbar_count"]:
                # Accessibility snapshots include button names and shadow DOM text.
                diagnostics["toolbar_snapshot"] = (await toolbar.aria_snapshot(timeout=1000))[:4000]
        except Exception as error:
            # Diagnostics must not replace the unknown result with another failure.
            diagnostics["diagnostic_error"] = f"{type(error).__name__}: {error}"
        return f"unknown: {json.dumps(diagnostics, ensure_ascii=False)}"
    return state


async def get_last_cell_output(page, timeout=15) -> str:
    """
    Gets the current text output of the last rendered code cell, including iframes.
    Does not run cells or wait for execution to finish.

    Args:
        page (Page): The Playwright page containing the Colab notebook.
        timeout (float): Maximum number of seconds for each output read or scroll.

    Returns:
        str: The cell's text output, or an empty string if there is no output.
    """
    cells = page.locator(".cell.code")
    if not await cells.count():
        raise NeedsAttention("The notebook has no rendered code cells to read output from.")

    last_cell = cells.last
    await last_cell.scroll_into_view_if_needed(timeout=timeout * 1000)
    parts = []
    for output in await last_cell.locator(".output").all():
        text = (await output.inner_text(timeout=timeout * 1000)).strip()
        if text:
            parts.append(text)
        for iframe in await output.locator("iframe").all():
            text = (await iframe.content_frame.locator("body").inner_text(
                timeout=timeout * 1000,
            )).strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


async def keepAlive(page, task_number=None, *, random=False) -> int:
    """
    Performs one keepAlive task through the Colab interface.

    Tasks:
        1. Click File, wait two seconds, and click Save.
        2. Click Additional connection options, wait two seconds, and View resources.
        3. Click Insert, wait two seconds, click Code cell, wait two seconds,
            activate the new cell, and delete it using its trash button.
        4. Click Insert, wait two seconds, click Code cell, scroll to it, and type
            print("status update") with variable delays, delete the new cell,
            and save through File. The new cell is not executed.
        5. Scroll to the first code cell, click it, wait one second, and replace everything
            below its # Status update marker with a comment containing the current
            time, training status, random percentage, and random epoch count.
            The marker may have trailing text, such as # Status update Comments.
            Type the comment with variable delays, wait two seconds, click File,
            wait two seconds, and click Save.

    Args:
        page (Page): The Playwright page containing the Colab notebook.
        task_number (int | None): The task to perform, from 1 to 5.
        random (bool): If True, randomly selects a task instead of task_number.

    Returns:
        int: The number of the task that was performed.
    """
    if not isinstance(random, bool):
        raise ValueError("random must be a boolean.")
    if random:
        if task_number is not None:
            raise ValueError("Specify task_number or random=True, not both.")
        task_number = random_module.choice((1, 2, 3, 4, 5))
    elif type(task_number) is not int or task_number not in (1, 2, 3, 4, 5):
        raise ValueError("task_number must be an integer from 1 to 5, or use random=True.")

    async def save(menu_delay=2):
        """
        Saves the notebook using the File menu after the specified pause.

        Args:
            menu_delay (float): Seconds to wait between clicking File and Save.

        Returns:
            None
        """
        await page.get_by_role("menubar").get_by_text("File", exact=True).click(timeout=10_000)
        await asyncio.sleep(menu_delay)
        await page.get_by_role("menuitem", name=re.compile(r"^Save(?:\s+(?:Ctrl|⌘|Command|Control).*)?$", re.I)).click(timeout=10_000)

    if task_number == 1:
        await save()
    elif task_number == 2:
        await page.get_by_role("button", name="Additional connection options", exact=True).click(timeout=10_000)
        await asyncio.sleep(2)
        await page.get_by_role("menuitem", name=re.compile(r"^View resources(?:\s|$)", re.I)).click(timeout=10_000)
    elif task_number == 5:
        cell = page.locator(".cell.code").first
        if not await cell.count():
            raise NeedsAttention("The notebook has no rendered code cells to update.")
        await cell.scroll_into_view_if_needed(timeout=10_000)
        await cell.evaluate("cell => cell.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
        await cell.click(timeout=10_000)
        await asyncio.sleep(1)
        # Monaco's textarea can contain only part of the source. Read its model
        # so a marker outside the visible editor cannot cause the wrong deletion.
        source = await cell.evaluate("""cell => {
            if (cell.querySelector('.monaco-editor')) {
                const editors = globalThis.monaco?.editor?.getEditors?.() || [];
                const editor = editors.find(editor => cell.contains(editor.getDomNode()));
                return editor?.getModel()?.getValue() ?? null;
            }
            const codeMirror = cell.querySelector('.CodeMirror')?.CodeMirror;
            if (codeMirror) return codeMirror.getValue();
            const input = cell.querySelector('textarea, [contenteditable="true"]');
            if (!input) return null;
            return input.tagName === 'TEXTAREA' ? input.value : input.innerText;
        }""")
        if source is None:
            raise NeedsAttention("Could not read the first Colab code cell's full source; no text was changed.")
        marker = re.search(r"^[^\S\r\n]*#[^\S\r\n]*Status[^\S\r\n]+update\b[^\r\n]*\r?$", source, re.I | re.M)
        if marker is None:
            raise NeedsAttention(
                "The first Colab code cell has no comment line starting with # Status update; no text was changed."
            )

        timestamp = datetime.now(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S %z")
        comment = (
            f"# Time: {timestamp} | CNN training: running good | "
            f"percent done: {random_module.randint(0, 100)}% | "
            f"epoches done : {random_module.randint(1000, 10000)}"
        )
        editor = cell.locator("textarea, [contenteditable='true']").first
        # Preserve the prefix verbatim rather than navigating by visual lines,
        # which may wrap and place the cursor above the marker.
        preserved_source = source[:marker.end()].removesuffix("\r") + "\n"
        await editor.press("ControlOrMeta+A", timeout=10_000)
        await page.keyboard.insert_text(preserved_source)
        for character in comment:
            await editor.press_sequentially(character, delay=random_module.randint(60, 180))
        await asyncio.sleep(2)
        await save(menu_delay=2)
    else:
        cells = page.locator(".cell.code")
        previous_ids = await cells.evaluate_all("cells => cells.map(cell => cell.id)")
        await page.get_by_role("menubar").get_by_text("Insert", exact=True).click(timeout=10_000)
        await asyncio.sleep(2)
        await page.get_by_role("menuitem", name=re.compile(r"^Code\s*cell(?:\s|$)", re.I)).click(timeout=10_000)
        added = await page.wait_for_function(
            """previous => {
                const added = [...document.querySelectorAll('.cell.code')]
                    .filter(cell => cell.id && !previous.includes(cell.id));
                return added.length ? added.map(cell => cell.id) : false;
            }""",
            arg=previous_ids, timeout=10_000,
        )
        new_ids = await added.json_value()
        if len(new_ids) != 1:
            raise NeedsAttention("Could not uniquely identify the newly inserted Colab code cell.")
        cell = page.locator(f".cell.code[id={json.dumps(new_ids[0])}]")
        if task_number == 3:
            await asyncio.sleep(2)
            await cell.click(timeout=10_000)
        else:
            editor = cell.locator("textarea, [contenteditable='true']").first
            await cell.scroll_into_view_if_needed(timeout=10_000)
            await cell.evaluate("cell => cell.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
            # Monaco renders its text over the input textarea, intercepting
            # pointer clicks. Focus the input directly before sending keystrokes.
            await editor.focus(timeout=10_000)
            for character in 'print("status update")':
                await editor.press_sequentially(character, delay=random_module.randint(60, 180))
        await cell.hover(timeout=10_000)
        await asyncio.sleep(1)
        await cell.get_by_role("button", name=re.compile(
            r"^(?:Delete cell|Remove cell|Delete)(?:\s|$)", re.I,
        )).click(timeout=10_000)
        await cell.wait_for(state="detached", timeout=10_000)
        if task_number == 4:
            await asyncio.sleep(1)
            await save()
    
    await asyncio.sleep(10)  # Allow the save to finish before returning.
    
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


async def getRemainingQuota(page, timeout=15) -> dict:
    """
    Opens View resources using keepAlive task 2 and reads the runtime quota.

    Args:
        page (Page): The Playwright page containing the Colab notebook.
        timeout (float): Seconds to wait for resource information after opening
            the panel, in addition to task 2's two-second menu pause.

    Returns:
        dict: name (google_colab), remaining_quota (compute units), time_remaining
            (seconds), end_timestamp (Unix seconds), and end_time (Tehran ISO 8601).
            Missing values are None. The runtime end is an estimate from Colab's
            "may last up to" duration, measured when the information is read.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout < float("inf")):
        raise ValueError("timeout must be a finite positive number of seconds.")
    await keepAlive(page, task_number=2)
    deadline = time.monotonic() + timeout
    resource_text = page.get_by_text(re.compile(
        r"You\s+currently\s+have\s+[\s\S]*?compute\s+units?\s+available|this\s+runtime\s+may\s+last", re.I,
    ))
    while True:
        parts = []
        for element in await resource_text.all():
            # Notebook source or saved output must not masquerade as resources.
            if (await element.is_visible()
                    and not await element.evaluate("""element => {
                        for (let node = element; node; node = node.getRootNode().host) {
                            if (node.closest('.cell')) return true;
                        }
                        return false;
                    }""")):
                # The amount or duration may be in sibling spans of the sentence.
                # Avoid widening the read to the notebook or the whole page.
                parent_text = await element.evaluate("""element => {
                    const parent = element.parentElement;
                    if (!parent || parent.matches('body, html') ||
                        parent.closest('.cell') || parent.querySelector('.cell')) return '';
                    return parent.innerText;
                }""")
                if parent_text:
                    parts.append(parent_text)
                parts.append(await element.inner_text(timeout=1000))
        result = _parse_runtime_resources("\n".join(parts))
        if result["remaining_quota"] is not None and result["time_remaining"] is not None:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if result["remaining_quota"] is not None or result["time_remaining"] is not None:
                return result
            raise NeedsAttention(
                "Could not parse compute units or runtime duration from Colab's View resources panel. "
                f"Page: {page.url}. Resource text: {' '.join(parts)[:2000]!r}"
            )
        await asyncio.sleep(min(0.25, remaining))


def print_state(profileName, txt, verbose=True):
    """
    Prints the state of the automation to the python console. Useful for debugging.
    
    Args:
        profileName (str): The name of the profile being processed.
        txt (str): The text to print.
        verbose (bool): If True, prints the text. If False, does nothing.
        
    Returns:
        None
    """
    
    if verbose:
        print(f"[{profileName}] {txt}", flush=True)

async def run(ctx: TaskContext) -> TaskResult:
    """
    Opens the configured Colab notebook, checks its account and runtime, and
    starts notebook execution when the runtime connection flow completes.

    Args:
        ctx (TaskContext): Browser, profile, parameters, and task output context.

    Returns:
        TaskResult: The task status and execution details on successful completion.
    """
    # For debugging
    verbose = ctx.params.get("verbose", True)
    
    # Check colab settings integrity
    settings = ctx.profile.settings.get("colab", {})
    if not isinstance(settings, dict):
        raise ValueError("Profile settings.colab must be a TOML table.")

    # Get the profile data and its respective colab platform data
    profileData = read_toml(ctx.profile.manifest_path) if ctx.profile.manifest_path else {}
    colabData = profileData.get("profile", {}).get("platforms", {}).get("colab")
    if ctx.profile.manifest_path and not colabData and not settings and not ctx.params:
        return TaskResult(
            status="skipped",
            message=f"Colab platform data not found in profile '{ctx.profile.name}'.",
        )
    if colabData is not None and not isinstance(colabData, dict):
        raise ValueError("Colab platform data must be a TOML table.")
    
    colabData = colabData or {}
    expected_email = colabData.get("login_username")
    if colabData and (not isinstance(expected_email, str) or not expected_email.strip()):
        raise ValueError("Colab platform data requires email or login_username for the login check.")
    notebook_url = colabData.get("notebook_url")

    # Verify the live tunnel used by this browser, rather than a temporary probe.
    if vpn_required(ctx.profile, platforms={"colab"}):
        print_state(ctx.profile.name, "Checking VPN connectivity", verbose=verbose)
        route = getattr(ctx.browser, "_zepuppeteer_vpn_route", None)
        if route is None:
            raise NeedsAttention(
                f"Profile '{ctx.profile.name}': VPN is required, but this browser was not launched through its VPN."
            )
        try:
            await route.check_connection()
        except (OSError, ValueError, RuntimeError) as error:
            raise NeedsAttention(
                f"Profile '{ctx.profile.name}': VPN connectivity check failed: {error}"
            ) from error

    # Check login status
    print_state(ctx.profile.name, "Checking login", verbose = verbose)
    page = await ctx.browser.new_page()
    try:
        notebook = await ColabSite(page).open_notebook(notebook_url)
    except NeedsAttention as error:
        # A Google sign-in redirect can stop notebook loading before check_login.
        # The hostname identifies English and non-English login pages alike.
        if urlsplit(page.url).hostname == "accounts.google.com":
            raise NeedsAttention(
                f"Profile '{ctx.profile.name}': Colab login check returned logged_out. "
                "Sign in manually in this profile, then retry."
            ) from error
        raise
    if expected_email:
        loginStatus = await check_login(page, expected_email)

        if loginStatus != "logged_in":
            raise NeedsAttention(f"Profile '{ctx.profile.name}': Colab login check returned {loginStatus}.")

    # Check the notebook name configured in profile.toml.
    print_state(ctx.profile.name, "Getting the notebook status", verbose = verbose)
    expected_notebook_name = colabData.get("notebook_name", None)
    if expected_notebook_name is not None:
        if not isinstance(expected_notebook_name, str) or not expected_notebook_name.strip():
            raise ValueError("Colab notebook_name must be a nonempty string.")
        notebookName = (await page.title()).removesuffix(" - Colab").strip()
        if notebookName != expected_notebook_name.strip():
            raise NeedsAttention(
                f"Notebook name mismatch for profile '{ctx.profile.name}': "
                f"expected {expected_notebook_name!r}, found {notebookName!r}."
            )
    else:
        raise NeedsAttention(
            f"Profile '{ctx.profile.name}' does not specify a notebook_name in its Colab platform data."
        )

    # Sleep briefly to allow the runtime toolbar to update its state before checking the connection.
    await asyncio.sleep(30)
    print_state(ctx.profile.name, "Getting the colab runtime status...", verbose = verbose)
    
    # Check that this notebook is connected to a Colab runtime.
    runtime_status = await check_runtime_connection(page)
    if runtime_status == "disconnected":
        if colabData.get("auto_connect", False):
            # Connect to a hosted runtime if the notebook is not already connected.
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            print_state(ctx.profile.name, "Attempting to start a runtime...", verbose = verbose)

            # A request already in progress only needs time to finish.
            if runtime_status != "connecting":
                try:
                    await page.get_by_role(
                        "button", name="Additional connection options", exact=True,
                    ).click(timeout=10_000)
                    
                    await asyncio.sleep(2)  # Wait for the menu to appear before clicking the menu item.
                    
                    await page.get_by_role(
                        "menuitem", name=re.compile(r"^Connect to a hosted runtime(?:\b|:)", re.I),
                    ).click(timeout=10_000)
                except PlaywrightTimeoutError as error:
                    raise NeedsAttention(
                        f"Profile '{ctx.profile.name}': could not select Connect to a hosted runtime. "
                        "Inspect the connection menu or any blocking dialog."
                    ) from error

            print_state(ctx.profile.name, f"Waiting for the Colab runtime to connect... | Current status: {runtime_status}", verbose = verbose)
            
            # One bounded five-minute connection wait; stop as soon as connected.
            runtime_status = await check_runtime_connection(page, timeout=300)
            
            if runtime_status != "connected":
                raise NeedsAttention(
                    f"Profile '{ctx.profile.name}': Colab runtime is {runtime_status} "
                    "after waiting up to 300 seconds. Manual supervision is needed."
                )
        else:
            raise NeedsAttention(
                f"Profile '{ctx.profile.name}': Colab runtime is {runtime_status}. "
                "Set auto_connect = true in the profile.toml to automatically connect."
            )
        
        # No need to run the keepAlive function here, since we just started the runtime
    elif runtime_status == "connecting":
        # The runtime is already up. Wait for it to load and start the keepAlive function
        # Sometimes, google colab is stuck in "connecting" state while the runtime is already
        # up and connected. We can infer this by the last cell's output change.
        outputBefore = await get_last_cell_output(page)
        for i in range(60):
            outputAfter = await get_last_cell_output(page)
            if outputBefore != outputAfter:
                runtime_status = "connected"
                print_state(ctx.profile.name, f"Runtime connected", verbose = verbose)
                break
            await asyncio.sleep(1)
            
            if i == 59:
                raise NeedsAttention(
                    f"Profile '{ctx.profile.name}': Colab runtime is stuck at {runtime_status}"
                    "without any output change. Manual supervision is needed."
                )
        
        print_state(ctx.profile.name, f"Colab runtime is already connected.", verbose = verbose)
        print_state(ctx.profile.name, f"Starting the keepAlive tasks...", verbose = verbose)
        
        await keepAlive(page, task_number = 5)
        print()
        exit()
        pass
    else:
        raise NeedsAttention(
            f"Profile '{ctx.profile.name}': Unknown colab runtime status: {runtime_status}"
        )
    print_state(ctx.profile.name, f"Colab runtime is connected.", verbose = verbose)

    # click the "run all" button and wait for the main job to start
    last_cell_id = await ColabSite(page).run_all_and_wait_for_last_cell(
        timeout_seconds=colabData.get("job_start_timeout_seconds", 300),
    )
    print_state(ctx.profile.name, f"Last code cell {last_cell_id} has started executing.", verbose = verbose)
    
    # On failure the page stays open for runner screenshots and trace collection.
    await page.close()
    return TaskResult(
            outputs={"last_cell_id": last_cell_id, "job_started": True},
            status="success",
            message="Colab notebook verified; Run all started the last code cell."
        )
