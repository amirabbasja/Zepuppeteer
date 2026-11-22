"""Create a managed Brave profile and wait for the user's manual login session."""

import json
import os
from pathlib import Path

from ..brave import find_brave, install_brave
from ..models import Profile, TaskResult
from ..runner import run_profiles


_MANIFEST = """# Brave browser data and saved logins are stored in src/ beside this file.
enabled = true
tags = []

[settings]
# Add workflow settings here and platform configuration under [profile.platforms].
"""


def _new_profile(profile_name: str, profiles_dir: str | Path) -> Profile:
    if (not isinstance(profile_name, str) or not profile_name.strip()
            or profile_name.startswith(".") or not profile_name.isprintable()
            or "/" in profile_name or "\\" in profile_name):
        raise ValueError("Profile name must be a non-hidden single directory name.")
    root = Path(profiles_dir).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"Not a profiles directory: {root}")
    if (root / "Local State").exists() or (root / "Preferences").exists():
        raise ValueError("Use a managed profiles directory, not a native Brave browser data directory.")
    folder = root / profile_name
    if folder.exists() or folder.is_symlink():
        raise ValueError(f"Profile already exists: {folder}. Choose a new profile name.")
    return Profile(profile_name, folder / "src", manifest_path=folder / "profile.toml")


async def createProfile(profile_name: str, *, profiles_dir: str | Path = "./profiles",
                        executable: str | None = None, no_install: bool = False,
                        dry_run: bool = False) -> TaskResult:
    """Create profile.toml + src/, then keep visible Brave open until it is closed."""
    profile = _new_profile(profile_name, profiles_dir)
    executable = find_brave(executable)
    outputs = {"profile": profile.name, "manifest_path": str(profile.manifest_path),
               "user_data_dir": str(profile.user_data_dir)}
    if dry_run:
        print(json.dumps({**outputs, "executable": executable or "Brave will be installed using sudo + APT"},
                         indent=2))
        return TaskResult(outputs=outputs, message="Profile creation preview; no files were written.")
    if hasattr(os, "getuid") and os.getuid() == 0:
        raise RuntimeError("Run Brave as your normal desktop user, not root.")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("Manual login requires a desktop display. Run in a desktop terminal.")
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

    folder = profile.manifest_path.parent
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir(mode=0o700)  # Exclusive creation; never replace an existing profile.
    profile.user_data_dir.mkdir(mode=0o700)
    with profile.manifest_path.open("x", encoding="utf-8") as stream:
        stream.write(_MANIFEST)
    print(f"Created profile configuration: {profile.manifest_path}", flush=True)
    print("Log in to the required websites in Brave, then close the browser when finished. "
          f"Cookies and browser data are kept in {profile.user_data_dir}.", flush=True)
    async with async_playwright() as playwright:
        await run_profiles(playwright, [profile], executable)
    print(f"Closed profile: {profile.name}. Browser data retained in {profile.user_data_dir}.", flush=True)
    return TaskResult(outputs=outputs, message="Manual setup session closed; browser data retained.")
