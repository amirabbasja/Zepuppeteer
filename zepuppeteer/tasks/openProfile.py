"""Open an existing account in regular Brave for manual logins."""

import asyncio
import json
import os
from pathlib import Path
import shlex

from ..brave import browser_environment, find_brave, profile_network
from ..models import TaskResult
from ..profiles import load_profile
from ..vpn import vpn_required
from .vpnCheck import _stop


async def openProfile(profile_name: str, *, profiles_dir: str | Path = "./profiles",
                      executable: str | None = None, dry_run: bool = False) -> TaskResult:
    """Launch native Brave with the selected account's existing browser data."""
    if (not isinstance(profile_name, str) or not profile_name.strip()
            or profile_name.startswith(".") or not profile_name.isprintable()
            or "/" in profile_name or "\\" in profile_name):
        raise ValueError("Profile name must be a non-hidden single directory name.")
    root = Path(profiles_dir).expanduser().resolve(strict=True)
    folder = root / profile_name
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"Profile directory does not exist or is symlinked: {folder}")
    profile = load_profile(folder)
    if not profile.user_data_dir.is_dir():
        raise ValueError(f"Browser data does not exist for '{profile_name}'. Run createProfile first.")
    executable = find_brave(executable)
    if executable is None:
        raise RuntimeError("Brave is not installed. Install Brave or supply --executable.")
    command = [executable, f"--user-data-dir={profile.user_data_dir}",
               f"--profile-directory={profile.profile_directory or 'Default'}", "--new-window",
               "--start-fullscreen"]
    outputs = {"profile": profile.name, "user_data_dir": str(profile.user_data_dir),
               "profile_directory": profile.profile_directory or "Default",
               "browser_display_name": profile.browser_display_name, "command": command,
               "use_vpn": vpn_required(profile)}
    if dry_run:
        print(json.dumps(outputs, indent=2))
        return TaskResult(outputs=outputs, message="Manual browser launch preview.")
    if hasattr(os, "getuid") and os.getuid() == 0:
        raise RuntimeError("Run Brave as your normal desktop user, not root.")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("Manual login requires a desktop display. Run in a desktop terminal.")
    print(f"Opening profile '{profile.name}' for manual login:\n{shlex.join(command)}", flush=True)
    async with profile_network(profile, executable) as (browser_executable, route):
        process = await asyncio.create_subprocess_exec(browser_executable, *command[1:], env=browser_environment())
        try:
            returncode = await process.wait()
        except BaseException:
            if route is not None:
                await _stop(process)
            raise
    if returncode:
        raise RuntimeError(f"Brave exited with status {returncode}.")
    return TaskResult(outputs=outputs, message="Brave manual login session finished.")
