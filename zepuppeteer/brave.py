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


_WEBDRIVER_INIT_SCRIPT = """
Object.defineProperty(Navigator.prototype, "webdriver", {
    get: () => undefined,
    configurable: true,
});
"""


async def hide_webdriver(context) -> None:
    """Return undefined for navigator.webdriver in current and future documents."""
    await context.add_init_script(script=_WEBDRIVER_INIT_SCRIPT)
    # Persistent contexts can already have tabs and frames when launch returns.
    # Init scripts only run for newly created or navigated documents.
    for page in context.pages:
        if page.is_closed():
            continue
        for frame in page.frames:
            if frame.is_detached():
                continue
            try:
                await frame.evaluate(_WEBDRIVER_INIT_SCRIPT)
            except Exception as error:
                # Restored tabs can close or navigate while startup is running.
                # Future documents are already covered by the init script.
                if (not page.is_closed() and not frame.is_detached()
                        and "Execution context was destroyed" not in str(error)):
                    raise
    browser = getattr(context, "browser", None)
    if browser is not None and not browser.is_connected():
        raise RuntimeError("Brave disconnected during browser setup.")


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


def launch_options(executable: str, profile: Profile, headless: bool = False) -> dict:
    args = ["--no-first-run", "--hide-crash-restore-bubble"]
    if profile.profile_directory:
        args.append(f"--profile-directory={profile.profile_directory}")
    options = {
        "executable_path": executable,
        "user_data_dir": str(profile.user_data_dir),
        "headless": headless,
        "args": args,
        # Imported profiles need the same desktop keyring as regular Brave.
        # Playwright's test defaults otherwise make saved cookies unreadable.
        "ignore_default_args": ["--password-store=basic", "--use-mock-keychain"],
        "no_viewport": True,
        # Playwright disables the Chromium sandbox by default; keep it enabled.
        "chromium_sandbox": True,
        # The runner owns signal handling and closes the active profile itself.
        "handle_sigint": False,
        "handle_sigterm": False,
    }
    # Brave is a system application. Virtual environments and packaged terminals
    # can expose older GNOME schemas that crash GTK during visible startup.
    # Scope this override to the browser; preserve display/session variables.
    schemas = Path("/usr/share/glib-2.0/schemas")
    if platform.system() == "Linux" and (schemas / "gschemas.compiled").is_file():
        options["env"] = {**os.environ, "GSETTINGS_SCHEMA_DIR": str(schemas)}
    return options
