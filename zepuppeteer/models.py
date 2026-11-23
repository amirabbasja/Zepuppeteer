"""Contracts shared by profile discovery, tasks, and the workflow runner."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .screen import ScreenBrowser
    from .vpn import VPNConnection


@dataclass(frozen=True)
class Profile:
    # Identity is the directory name, so changing a display name cannot retarget a group.
    name: str
    user_data_dir: Path
    profile_directory: str | None = None
    enabled: bool = True
    tags: tuple[str, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)
    browser_display_name: str | None = None
    manifest_path: Path | None = None


@dataclass(frozen=True)
class Step:
    id: str
    task: str
    params: dict[str, Any] = field(default_factory=dict)
    input_from: str | None = None
    timeout_seconds: float = 120
    retries: int = 0
    retry_safe: bool = False
    retry_delay_seconds: float = 1


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Step, ...]
    on_profile_failure: str = "continue"


@dataclass(frozen=True)
class TaskContext:
    browser: "ScreenBrowser"
    profile: Profile
    params: dict[str, Any]
    inputs: dict[str, Any]
    artifact_dir: Path
    run_id: str
    step_id: str
    vpn: "VPNConnection | None" = None


@dataclass(frozen=True)
class TaskResult:
    # Tasks return success only after checking the website's resulting state.
    outputs: dict[str, Any] = field(default_factory=dict)
    status: str = "success"
    message: str = ""


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    status: str
    succeeded: int
    failed: int
    skipped: int
    artifact_dir: Path


class NeedsAttention(RuntimeError):
    """Login, MFA, or another manual prerequisite prevents unattended execution."""
