"""Open an existing Brave profile for manual work until the user closes it."""

import json
import os
from pathlib import Path

from ..brave import find_brave, install_brave
from ..models import Profile, TaskResult
from ..profiles import load_profile
from ..runner import run_profiles


def _existing_profile(profile_name: str, profiles_dir: str | Path) -> Profile:
    if (not isinstance(profile_name, str) or not profile_name.strip()
            or profile_name.startswith(".") or not profile_name.isprintable()
            or "/" in profile_name or "\\" in profile_name):
        raise ValueError("Profile name must be a non-hidden single directory name.")
    root = Path(profiles_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Not a profiles directory: {root}")
    if (root / "Local State").exists() or (root / "Preferences").exists():
        raise ValueError("Use a managed profiles directory, not a native Brave browser data directory.")
    folder = root / profile_name
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"Profile directory does not exist or is symlinked: {profile_name}. "
                         "Run createProfile first to set up a new account.")
    return load_profile(folder)


async def openProfile(profile_name: str, *, profiles_dir: str | Path = "./profiles",
                      executable: str | None = None, no_install: bool = False,
                      dry_run: bool = False) -> TaskResult:
    """Open one existing account in visible Brave and wait for manual closure."""
    profile = _existing_profile(profile_name, profiles_dir)
    executable = find_brave(executable)
    outputs = {"profile": profile.name,
               "manifest_path": str(profile.manifest_path) if profile.manifest_path else None,
               "user_data_dir": str(profile.user_data_dir),
               "profile_directory": profile.profile_directory,
               "browser_display_name": profile.browser_display_name}
    if dry_run:
        print(json.dumps({**outputs, "executable": executable or "Brave will be installed using sudo + APT"},
                         indent=2))
        return TaskResult(outputs=outputs, message="Manual session preview; no files were written.")
    if hasattr(os, "getuid") and os.getuid() == 0:
        raise RuntimeError("Run Brave as your normal desktop user, not root.")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("Manual work requires a desktop display. Run in a desktop terminal.")
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise RuntimeError("Playwright is missing. Install the project with: python -m pip install -e .") from error
    if not executable:
        if no_install:
            raise RuntimeError("Brave is not installed. Remove --no-install to install it automatically.")
        install_brave()
        executable = find_brave()
        if not executable:
            raise RuntimeError("Installation finished but Brave could not be found on PATH.")

    print(f"Work in Brave using profile {profile.name}, then close the browser when finished. "
          f"Cookies and browser data are kept in {profile.user_data_dir}.", flush=True)
    async with async_playwright() as playwright:
        await run_profiles(playwright, [profile], executable)
    print(f"Closed profile: {profile.name}. Browser data retained in {profile.user_data_dir}.", flush=True)
    return TaskResult(outputs=outputs, message="Manual session closed; browser data retained.")
