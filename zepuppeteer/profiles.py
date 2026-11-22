"""Discover browser data, read optional profile metadata, and select named groups."""

import re
from pathlib import Path

from .config import read_toml
from .models import Profile
from .profile_transfer import find_profile, read_state


_NATIVE_DIRECTORY = re.compile(r"Default|Profile \d+")


def _named_brave_profile(display_name: str) -> tuple[Path, str]:
    root = (Path.home() / ".config/BraveSoftware/Brave-Browser").resolve()
    try:
        state = read_state(root)
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read installed Brave profiles in {root}: {error}") from error
    directory = find_profile(state, display_name)
    return root, _profile_directory(root, directory)


def _profile_directory(root: Path, configured: str | None) -> str | None:
    if configured is not None:
        if not isinstance(configured, str) or not _NATIVE_DIRECTORY.fullmatch(configured):
            raise ValueError(f"Invalid profile_directory for {root}; use Default or Profile N.")
        if not (root / configured).is_dir() or (root / configured).is_symlink():
            raise ValueError(f"Configured browser profile does not exist or is symlinked: {root / configured}")
        return configured
    if not root.exists():
        return None
    directories = [entry.name for entry in root.iterdir()
                   if entry.is_dir() and not entry.is_symlink() and _NATIVE_DIRECTORY.fullmatch(entry.name)]
    if len(directories) > 1:
        raise ValueError(f"Multiple browser profiles in {root}; set profile_directory in profile.toml.")
    return directories[0] if directories else None


def discover_profiles(directory: str | Path, layout: str = "auto") -> list[Profile]:
    if layout not in {"auto", "native", "isolated"}:
        raise ValueError("Invalid layout. Use auto, native, or isolated.")
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Not a profiles directory: {root}")
    native = layout == "native" or (layout == "auto" and (root / "Local State").is_file())
    if not native and (root / "Preferences").is_file():
        raise ValueError("This is a single Chromium profile. Pass its parent user-data directory with --layout native.")
    folders = [entry for entry in root.iterdir()
               if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith(".")
               and (not native or _NATIVE_DIRECTORY.fullmatch(entry.name))]
    folders.sort(key=lambda entry: [
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", entry.name)
    ])
    profiles = []
    for entry in folders:
        manifest = entry / "profile.toml"
        metadata = read_toml(manifest) if manifest.exists() else {}
        enabled, tags, settings = (metadata.get("enabled", True), metadata.get("tags", []),
                                   metadata.get("settings", {}))
        if type(enabled) is not bool:
            raise ValueError(f"enabled must be a boolean in {manifest}.")
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise ValueError(f"tags must be an array of nonempty strings in {manifest}.")
        if not isinstance(settings, dict):
            raise ValueError(f"settings must be a table in {manifest}.")
        browser_metadata = metadata.get("profile", {})
        if not isinstance(browser_metadata, dict):
            raise ValueError(f"profile must be a table in {manifest}.")
        display_name = browser_metadata.get("brave_browser_displayName")
        if display_name is not None:
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError(f"profile.brave_browser_displayName must be a nonempty string in {manifest}.")
            if metadata.get("profile_directory") is not None:
                raise ValueError(f"Use either profile.brave_browser_displayName or profile_directory in {manifest}.")
            data_root, browser_directory = _named_brave_profile(display_name)
        elif native:
            data_root, browser_directory = root, entry.name
        else:
            # Import/export already uses src/. Metadata marks a new managed profile.
            managed = (entry / "src").exists() or manifest.exists()
            if (entry / "src").is_symlink():
                raise ValueError(f"Browser data directory must not be symlinked: {entry / 'src'}")
            # Existing flat browser data remains usable even after adding metadata.
            flat = (entry / "Local State").is_file() or (entry / "Default").is_dir()
            if managed and flat and (entry / "src").exists():
                raise ValueError(f"Ambiguous browser data: both {entry} and its src/ contain profile data.")
            data_root = entry / "src" if managed and not flat else entry
            if data_root.exists() and not data_root.is_dir():
                raise ValueError(f"Browser data path is not a directory: {data_root}")
            browser_directory = _profile_directory(data_root, metadata.get("profile_directory"))
        profiles.append(Profile(entry.name, data_root, browser_directory, enabled, tuple(tags), settings,
                                display_name, manifest if manifest.exists() else None))
    if not profiles:
        raise ValueError(f"No profiles found in {root}. Create a subdirectory for each profile first.")
    return profiles


def select_profiles(profiles: list[Profile], *, group: str | None = None,
                    groups_file: str | Path = "config/groups.toml", names: list[str] | None = None,
                    tags: list[str] | None = None, include_disabled: bool = False) -> list[Profile]:
    """Intersect group, explicit names, and tags; preserve natural discovery order."""
    selected = set(profile.name for profile in profiles)
    if group and group != "all":
        groups = read_toml(groups_file).get("groups")
        if not isinstance(groups, dict) or group not in groups:
            raise ValueError(f"Unknown group {group!r} in {groups_file}.")
        members = groups[group]
        if not isinstance(members, list) or not all(isinstance(name, str) for name in members):
            raise ValueError(f"Group {group!r} must be an array of profile directory names.")
        missing = set(members) - selected
        if missing:
            raise ValueError(f"Unknown profiles in group {group!r}: {', '.join(sorted(missing))}")
        selected.intersection_update(members)
    if names:
        missing = set(names) - {profile.name for profile in profiles}
        if missing:
            raise ValueError(f"Unknown profiles: {', '.join(sorted(missing))}")
        selected.intersection_update(names)
    result = [profile for profile in profiles if profile.name in selected
              and (include_disabled or profile.enabled) and set(tags or ()).issubset(profile.tags)]
    if not result:
        raise ValueError("No profiles match the selection (disabled profiles are excluded by default).")
    return result
