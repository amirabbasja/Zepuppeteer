"""Browser discovery, Ubuntu installation, and persistent profile configuration."""

import os
import json
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
from typing import Callable

from .models import Profile
from .profiles import discover_profiles  # Backwards-compatible public import.
from .profile_transfer import atomic_write, ensure_closed


def find_brave(executable: str | None = None) -> str | None:
    candidates = [executable] if executable else ["brave-browser", "brave-browser-stable", "brave"]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return str(Path(found).absolute())
    if executable:
        raise ValueError(f"Brave executable is missing or not executable: {executable}")
    return None


def run_command(command: list[str]) -> None:
    print(f"> {shlex.join(command)}", flush=True)
    subprocess.run(command, check=True)


def install_brave(run: Callable[[list[str]], None] = run_command) -> None:
    """Use Brave's official Ubuntu/Debian stable APT repository."""
    if platform.system() != "Linux":
        raise RuntimeError("Automatic installation requires Ubuntu/Debian Linux.")
    release = Path("/etc/os-release").read_text()
    if not re.search(r"^(?:ID|ID_LIKE)=.*\b(?:ubuntu|debian)\b", release, re.MULTILINE):
        raise RuntimeError("Automatic installation supports Ubuntu/Debian. Install Brave manually and use --executable.")
    if os.getuid() == 0:
        raise RuntimeError("Run as your normal desktop user; the installer uses sudo when needed.")
    print("Brave is missing. Installing the stable release through its official APT repository.", flush=True)
    run(["sudo", "apt-get", "update"])
    run(["sudo", "apt-get", "install", "-y", "curl", "ca-certificates"])
    base = "https://brave-browser-apt-release.s3.brave.com"
    run(["sudo", "curl", "--proto", "=https", "-fsSLo",
         "/usr/share/keyrings/brave-browser-archive-keyring.gpg",
         f"{base}/brave-browser-archive-keyring.gpg"])
    run(["sudo", "curl", "--proto", "=https", "-fsSLo",
         "/etc/apt/sources.list.d/brave-browser-release.sources",
         f"{base}/brave-browser.sources"])
    run(["sudo", "apt-get", "update"])
    run(["sudo", "apt-get", "install", "-y", "brave-browser"])


def prepare_profile_for_launch(profile: Profile) -> None:
    """Start the selected persistent profile blank, keeping cookies and logins."""
    ensure_closed(profile.user_data_dir)
    preferences = profile.user_data_dir / (profile.profile_directory or "Default") / "Preferences"
    if not preferences.is_file():
        return  # A new profile already starts blank.
    if preferences.is_symlink():
        raise ValueError(f"Browser preferences must not be symlinked: {preferences}")
    original = preferences.read_bytes()
    data = json.loads(original)
    data.setdefault("session", {})["restore_on_startup"] = 5  # New Tab page.
    data.setdefault("profile", {})["exit_type"] = "Normal"
    # Pinned tabs can otherwise be restored even with New Tab startup selected.
    data["pinned_tabs"] = []
    updated = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if json.loads(original) == data:
        return
    backup = preferences.with_name("Preferences.before-zepuppeteer-startup")
    if not backup.exists():
        atomic_write(backup, original)
    atomic_write(preferences, updated)


def browser_environment() -> dict:
    """Preserve the normal desktop/keyring session and use system GTK schemas."""
    environment = dict(os.environ)
    schemas = Path("/usr/share/glib-2.0/schemas")
    if platform.system() == "Linux" and (schemas / "gschemas.compiled").is_file():
        environment["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    return environment


def native_command(executable: str, profile: Profile, *, proxy=None) -> list[str]:
    """Launch the installed browser normally, without automation/debugging flags."""
    command = [executable, f"--user-data-dir={profile.user_data_dir}", "--new-window"]
    if profile.profile_directory:
        command.append(f"--profile-directory={profile.profile_directory}")
    if proxy:
        from urllib.parse import urlsplit
        url = urlsplit(proxy["server"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.username or url.password:
            raise ValueError("The native browser proxy must be an HTTP relay on 127.0.0.1.")
        command += [f"--proxy-server={proxy['server']}", "--proxy-bypass-list=<-loopback>",
                    "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    command.append("about:blank")
    return command
