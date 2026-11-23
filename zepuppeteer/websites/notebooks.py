"""Shared visible-state checks and evidence for notebook workflows."""

import asyncio
import json
from pathlib import Path
import time

from ..screen import TargetNotFound


def target_region(page, name, default):
    browser = getattr(page, "browser", None)
    target = getattr(browser, "targets", {}).get(name, {})
    if not isinstance(target, dict):
        raise ValueError(f"Screen target {name} must be a TOML table.")
    return target.get("region", default)


async def wait_for_title(page, pattern, *, timeout):
    """Read the OS window title until the notebook finishes loading."""
    deadline = time.monotonic() + max(0, timeout)
    while True:
        title = await page.title()
        if pattern.search(title):
            return title
        if time.monotonic() >= deadline:
            raise TargetNotFound("The notebook window title did not become ready.")
        await asyncio.sleep(0.25)


async def save_notebook_evidence(ctx, page, platform, notebook, requested_url, **state):
    """Save the verified visible state before the workflow closes its tab."""
    directory = Path(ctx.artifact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    screenshot = directory / f"{platform}-verified.png"
    metadata = directory / f"{platform}-verified.json"
    await page.screenshot(path=str(screenshot))
    outputs = {
        "profile": ctx.profile.name,
        "platform": platform,
        "backend": "screen",
        "requested_url": requested_url,
        "notebook_url": notebook.get("notebook_url", requested_url),
        "title": await page.title(),
        "account_verified": True,
        **state,
        "screenshot_path": str(screenshot),
        "metadata_path": str(metadata),
    }
    metadata.write_text(json.dumps(outputs, ensure_ascii=False, indent=2), encoding="utf-8")
    return outputs


async def visual_runtime_state(page, prefix, states):
    """Allow icon-only runtime states to be recognized from configured images."""
    for state in states:
        name = f"{prefix}_runtime_{state}"
        if name not in page.browser.targets:
            continue
        target = page.browser.targets[name]
        if not isinstance(target, dict) or not target.get("region"):
            raise ValueError(f"Screen target {name} requires a region restricted to the runtime controls.")
        try:
            await page.wait_for_target(name, timeout=0)
        except TargetNotFound:
            continue
        return state
    return None
