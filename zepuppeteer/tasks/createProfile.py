"""Initialize or reopen a Brave profile for the user's manual login session."""

import json
import os
from pathlib import Path

from ..brave import find_brave, install_brave
from ..models import Profile, TaskResult
from ..profiles import load_profile
from ..profile_transfer import atomic_write, ensure_closed, find_profile, read_state, state_bytes
from ..runner import run_profiles
from ..screen import validate_desktop


_MANIFEST = """# Brave browser data and saved logins are stored in src/ beside this file.
enabled = true
tags = []

[settings]
# Add workflow settings here and platform configuration under [profile.platforms].
"""


def _setup_profile(profile_name: str, profiles_dir: str | Path) -> Profile:
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
    if folder.is_symlink():
        raise ValueError(f"Profile directory must not be symlinked: {folder}")
    if folder.exists() and not folder.is_dir():
        raise ValueError(f"Not a profile directory: {folder}")
    return load_profile(folder, initialize=True)


def _initialize_named_profile(profile: Profile) -> None:
    """Register a missing named profile without replacing other Brave accounts."""
    root = profile.user_data_dir
    ensure_closed(root)
    state_path = root / "Local State"
    if state_path.is_symlink():
        raise ValueError(f"Brave browser state must not be symlinked: {state_path}")
    original = state_path.read_bytes() if state_path.exists() else None
    state = read_state(root) if original is not None else {}
    existing = find_profile(state, profile.browser_display_name, required=False)
    if existing is not None:
        if existing != profile.profile_directory:
            raise RuntimeError("Brave profiles changed during setup. Run createProfile again.")
        return
    directory = profile.profile_directory
    if directory in state.get("profile", {}).get("info_cache", {}):
        raise RuntimeError("Brave profiles changed during setup. Run createProfile again.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / directory
    target.mkdir(mode=0o700)  # Exclusive creation; existing browser data is never replaced.
    preferences = target / "Preferences"
    try:
        info = {"name": profile.browser_display_name, "is_using_default_name": False}
        atomic_write(preferences, state_bytes({"profile": info}))
        state.setdefault("profile", {}).setdefault("info_cache", {})[directory] = info
        ensure_closed(root)
        current = state_path.read_bytes() if state_path.exists() else None
        if current != original:
            raise RuntimeError("Brave profiles changed during setup. Run createProfile again.")
        atomic_write(state_path, state_bytes(state))
    except BaseException:
        preferences.unlink(missing_ok=True)
        target.rmdir()
        raise


async def createProfile(profile_name: str, *, profiles_dir: str | Path = "./profiles",
                        executable: str | None = None, no_install: bool = False,
                        dry_run: bool = False) -> TaskResult:
    """Initialize or reopen an account's Brave profile for a manual login session."""
    profile = _setup_profile(profile_name, profiles_dir)
    executable = find_brave(executable)
    outputs = {"profile": profile.name, "manifest_path": str(profile.manifest_path),
               "user_data_dir": str(profile.user_data_dir),
               "profile_directory": profile.profile_directory,
               "browser_display_name": profile.browser_display_name}
    if dry_run:
        print(json.dumps({**outputs, "executable": executable or "Brave will be installed using sudo + APT"},
                         indent=2))
        return TaskResult(outputs=outputs, message="Profile creation preview; no files were written.")
    if hasattr(os, "getuid") and os.getuid() == 0:
        raise RuntimeError("Run Brave as your normal desktop user, not root.")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("Manual login requires a desktop display. Run in a desktop terminal.")
    validate_desktop(ocr=False)
    if not executable:
        if no_install:
            raise RuntimeError("Brave is not installed. Remove --no-install to install it automatically.")
        install_brave()
        executable = find_brave()
        if not executable:
            raise RuntimeError("Installation finished but Brave could not be found on PATH.")

    folder = profile.manifest_path.parent
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir(mode=0o700, exist_ok=True)
    if profile.manifest_path.exists():
        print(f"Using profile configuration: {profile.manifest_path}", flush=True)
    else:
        with profile.manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(_MANIFEST)
        print(f"Created profile configuration: {profile.manifest_path}", flush=True)
    if profile.browser_display_name:
        _initialize_named_profile(profile)
    else:
        profile.user_data_dir.mkdir(mode=0o700, exist_ok=True)
    print("Log in to the required websites in Brave, then close the browser when finished. "
          f"Cookies and browser data are kept in {profile.user_data_dir}.", flush=True)
    await run_profiles([profile], executable)
    print(f"Closed profile: {profile.name}. Browser data retained in {profile.user_data_dir}.", flush=True)
    return TaskResult(outputs=outputs, message="Manual setup session closed; browser data retained.")
