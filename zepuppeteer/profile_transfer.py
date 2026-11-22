"""Move a named profile between Brave and this repository, preserving other profiles."""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def ensure_closed(root: Path) -> None:
    lock = root / "SingletonLock"
    if not lock.is_symlink():
        if lock.exists():
            raise RuntimeError(f"Unrecognized browser lock in {root}; close Brave before transferring.")
        return
    hostname, _, pid_text = os.readlink(lock).rpartition("-")
    if hostname != socket.gethostname() or not pid_text.isdigit():
        raise RuntimeError(f"Cannot establish whether Brave is closed: {root}")
    try:
        os.kill(int(pid_text), 0)
    except ProcessLookupError:
        return  # Stale lock; leave it alone.
    except PermissionError:
        pass
    raise RuntimeError(f"Brave is running for {root}. Close it before transferring.")


def read_state(root: Path) -> dict:
    state = json.loads((root / "Local State").read_text(encoding="utf-8"))
    if not isinstance(state.get("profile", {}).get("info_cache", {}), dict):
        raise ValueError(f"Invalid profile metadata in {root}")
    return state


def find_profile(state: dict, display_name: str, required: bool = True) -> str | None:
    matches = [folder for folder, info in state.get("profile", {}).get("info_cache", {}).items()
               if info.get("name") == display_name]
    if len(matches) > 1:
        raise ValueError(f"Multiple Brave profiles have the display name {display_name!r}.")
    if not matches:
        if required:
            raise ValueError(f"No Brave profile has the display name {display_name!r}.")
        return None
    folder = matches[0]
    if not re.fullmatch(r"Default|Profile \d+", folder):
        raise ValueError(f"Unsupported internal profile folder: {folder!r}")
    return folder


def without_profile(state: dict, folder: str) -> dict:
    state = deepcopy(state)
    profile = state["profile"]
    profile["info_cache"].pop(folder)
    remaining = list(profile["info_cache"])
    if profile.get("last_used") == folder:
        if remaining:
            profile["last_used"] = remaining[0]
        else:
            profile.pop("last_used", None)
    for key in ("last_active_profiles", "profiles_order"):
        if key in profile:
            profile[key] = [item for item in profile[key] if item != folder]
    return state


def atomic_write(path: Path, data: bytes) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=".transfer-state-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def state_bytes(state: dict) -> bytes:
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(path: Path) -> bytes:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.digest()


def verify_copy(source: Path, destination: Path) -> int:
    originals = {path.relative_to(source) for path in source.rglob("*")}
    copies = {path.relative_to(destination) for path in destination.rglob("*")}
    if originals != copies:
        raise RuntimeError("Profile contents changed during copying. Transfer cancelled.")
    count = 0
    for relative in originals:
        original, copied = source / relative, destination / relative
        if original.is_symlink():
            valid = copied.is_symlink() and os.readlink(original) == os.readlink(copied)
        elif original.is_file():
            valid = digest(original) == digest(copied)
            count += 1
        else:
            valid = copied.is_dir()
        if not valid:
            raise RuntimeError(f"Copied profile failed verification: {relative}")
    return count


def transfer_profile(direction: str, display_name: str, profile_name: str, *,
                     brave_dir: Path | None = None, profiles_dir: Path | None = None,
                     replace: bool = False, copy: bool = False, dry_run: bool = False) -> dict:
    if direction not in {"import", "export"}:
        raise ValueError("Direction must be import or export.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", profile_name):
        raise ValueError("Repository profile name must be a folder name, for example acc-5.")
    base = (profiles_dir or REPOSITORY_ROOT / "profiles").expanduser().resolve()
    local = (base / profile_name / "src").resolve()
    if not local.is_relative_to(base):
        raise ValueError("Destination profile resolves outside profiles/.")
    brave = (brave_dir or Path.home() / ".config/BraveSoftware/Brave-Browser").expanduser().resolve()
    if local == brave or local.is_relative_to(brave) or brave.is_relative_to(local):
        raise ValueError("Brave and repository profile directories must not overlap.")
    source, destination = (brave, local) if direction == "import" else (local, brave)
    ensure_closed(source)
    ensure_closed(destination)
    source_state = read_state(source)
    source_folder = find_profile(source_state, display_name)
    source_path = source / source_folder
    if source_path.is_symlink() or not source_path.is_dir():
        raise ValueError(f"Source profile is missing or symlinked: {source_path}")
    destination_state_path = destination / "Local State"
    destination_state = read_state(destination) if destination_state_path.exists() else None
    existing = find_profile(destination_state, display_name, required=False) if destination_state else None
    if existing and not replace:
        raise FileExistsError("Destination already contains this profile. Use --replace to back it up and replace it.")
    if direction == "import":
        if destination_state and set(destination_state.get("profile", {}).get("info_cache", {})) - {existing}:
            raise ValueError("Repository src contains other profiles; choose a different repository profile name.")
        if not destination_state and destination.exists() and any(destination.iterdir()):
            raise ValueError("Repository src is nonempty and has no Local State; refusing to overwrite it.")
    destination_folder = existing or source_folder
    destination_cache = (destination_state or {}).get("profile", {}).get("info_cache", {})
    if not existing and ((destination / destination_folder).exists() or destination_folder in destination_cache):
        if direction == "import":
            raise FileExistsError(f"Destination folder already exists: {destination / destination_folder}")
        number = 1
        while (destination / f"Profile {number}").exists() or f"Profile {number}" in destination_cache:
            number += 1
        destination_folder = f"Profile {number}"
    destination_path = destination / destination_folder
    if destination_path.is_symlink():
        raise ValueError("Refusing to replace a symlinked destination profile.")
    if destination_state is None:
        updated = deepcopy(source_state)
        updated["profile"]["info_cache"] = {}
        updated["profile"]["last_active_profiles"] = []
        updated["profile"]["profiles_order"] = []
    else:
        # Keep the destination browser's global settings, encryption metadata, and other accounts.
        updated = deepcopy(destination_state)
    info = deepcopy(source_state["profile"]["info_cache"][source_folder])
    updated.setdefault("profile", {}).setdefault("info_cache", {})[destination_folder] = info
    updated["profile"]["last_used"] = destination_folder
    for key in ("last_active_profiles", "profiles_order"):
        if key in updated["profile"]:
            values = updated["profile"][key]
            if destination_folder not in values:
                values.append(destination_folder)
    result = {"operation": "copy" if copy else "move", "display_name": display_name,
              "source": str(source_path), "destination": str(destination_path), "dry_run": dry_run}
    if dry_run:
        return result

    source_state_path = source / "Local State"
    original_source_state = source_state_path.read_bytes()
    original_destination_state = destination_state_path.read_bytes() if destination_state_path.exists() else None
    if (json.loads(original_source_state) != source_state or
            (json.loads(original_destination_state) if original_destination_state is not None else None) != destination_state):
        raise RuntimeError("Browser metadata changed while planning. Transfer cancelled.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid4().hex
    staging = Path(tempfile.mkdtemp(prefix=".profile-transfer-", dir=destination))
    destination_backup = destination / f".{destination_folder}.backup-{token}"
    source_recovery = source / f".{source_folder}.moving-{token}"
    installed = False
    moved_source = False
    backed_up = False
    committing = False
    try:
        shutil.copytree(source_path, staging / "profile", symlinks=True)
        result["verified_files"] = verify_copy(source_path, staging / "profile")
        # Abort if a browser opened or metadata changed while the snapshot was being copied.
        ensure_closed(source)
        ensure_closed(destination)
        current_destination_state = destination_state_path.read_bytes() if destination_state_path.exists() else None
        if source_state_path.read_bytes() != original_source_state or current_destination_state != original_destination_state:
            raise RuntimeError("Browser metadata changed during copying. Transfer cancelled.")
        # Keep recovery snapshots of any metadata being changed.
        if original_destination_state is not None:
            atomic_write(destination / f"Local State.backup-{token}", original_destination_state)
        if not copy:
            atomic_write(source / f"Local State.backup-{token}", original_source_state)
        committing = True
        if destination_path.exists():
            destination_path.rename(destination_backup)
            backed_up = True
        (staging / "profile").rename(destination_path)
        installed = True
        if not copy:
            source_path.rename(source_recovery)
            moved_source = True
        atomic_write(destination_state_path, state_bytes(updated))
        if not copy:
            atomic_write(source_state_path, state_bytes(without_profile(source_state, source_folder)))
    except BaseException:
        if committing:
            if moved_source:
                source_recovery.rename(source_path)
            if installed:
                shutil.rmtree(destination_path)
            if backed_up:
                destination_backup.rename(destination_path)
            if not copy:
                atomic_write(source_state_path, original_source_state)
            if original_destination_state is None:
                destination_state_path.unlink(missing_ok=True)
            else:
                atomic_write(destination_state_path, original_destination_state)
        raise
    finally:
        shutil.rmtree(staging)
    if moved_source:
        try:
            shutil.rmtree(source_recovery)
        except OSError:
            result["cleanup_pending"] = str(source_recovery)
    if backed_up:
        result["replaced_profile_backup"] = str(destination_backup)
    return result


def transfer_main(direction: str) -> None:
    parser = argparse.ArgumentParser(
        description=f"{direction.capitalize()} a Brave profile by display name (moves data by default)."
    )
    parser.add_argument("display_name", help="Brave display name, for example AI-acc5")
    parser.add_argument("profile_name", help="Folder under profiles/, for example acc-5")
    parser.add_argument("--brave-dir", type=Path, help="Override the Ubuntu Brave user-data root")
    parser.add_argument("--profiles-dir", type=Path, help="Override the repository profiles/ root")
    parser.add_argument("--copy", action="store_true", help="Keep the source profile instead of moving it")
    parser.add_argument("--replace", action="store_true", help="Back up and replace an existing destination profile")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing any files")
    args = parser.parse_args()
    try:
        print(json.dumps(transfer_profile(direction, **vars(args)), indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Error: {error}\n")
