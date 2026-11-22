"""Command-line entry points for workflows, discovery, history, and legacy tasks."""

import argparse
import asyncio
from dataclasses import asdict
import importlib.util
import inspect
import json
import os
from pathlib import Path
import signal
import sys

from .brave import find_brave, install_brave
from .profiles import discover_profiles, select_profiles
from .runner import run_profiles, run_workflow  # Keep the original public runner import working.
from .state import StateStore, read_history
from .workflows import load_workflow, resolve_tasks, workflow_path


def load_task(filename: str):
    path = Path(filename).expanduser().resolve(strict=True)
    spec = importlib.util.spec_from_file_location("zepuppeteer_user_task", path)
    if spec is None or spec.loader is None:
        raise ValueError("Task must be a Python module (.py file).")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    task = getattr(module, "run", None)
    if not inspect.iscoroutinefunction(task):
        raise ValueError("Task module must define async def run(context, profile).")
    return task


def _profile_arguments(result, *, required=False):
    result.add_argument("--profiles-dir", required=required, default=None if required else "./profiles",
                        help="Directory containing profile folders (default: ./profiles)")
    result.add_argument("--layout", choices=["auto", "isolated", "native"], default="auto")


def _browser_arguments(result):
    result.add_argument("--executable", help="Path or command name of the Brave executable")
    result.add_argument("--no-install", action="store_true", help="Fail instead of installing missing Brave")
    result.add_argument("--headless", action="store_true", help="Run without a desktop window")
    result.add_argument("--dry-run", action="store_true", help="Preview without installing or launching")


def _selection_arguments(result):
    result.add_argument("--config-dir", default="./config", help="Configuration directory (default: ./config)")
    result.add_argument("--group", default="all", help="Named profile group (default: all enabled profiles)")
    result.add_argument("--profile", action="append", help="Intersect with a profile directory name; repeatable")
    result.add_argument("--tag", action="append", help="Require this profile tag; repeatable")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run web automation workflows across groups of persistent Brave profiles.",
        epilog="Legacy mode is also supported: zepuppeteer --profiles-dir PATH [--task FILE]")
    commands = result.add_subparsers(dest="command", required=True)
    workflow = commands.add_parser("run", help="Run an ordered workflow on selected profiles")
    _profile_arguments(workflow)
    _selection_arguments(workflow)
    _browser_arguments(workflow)
    workflow.add_argument("--workflow", required=True, help="Workflow name or path to a .toml file")
    workflow.add_argument("--state-db", default="./data/runs.sqlite", help="Execution history database")
    workflow.add_argument("--artifacts-dir", default="./artifacts", help="Task output and failure evidence root")
    workflow.add_argument("--no-trace", action="store_true", help="Disable browser trace recording")
    profiles = commands.add_parser("profiles", help="List selected profiles as JSON without opening Brave")
    _profile_arguments(profiles)
    _selection_arguments(profiles)
    profiles.add_argument("--include-disabled", action="store_true")
    create = commands.add_parser("createProfile", aliases=["create-profile"],
                                 help="Initialize or reopen a Brave profile for manual login")
    create.add_argument("profile_name", help="Profile directory name (existing configuration is preserved)")
    create.add_argument("--profiles-dir", default="./profiles")
    create.add_argument("--executable", help="Path or command name of the Brave executable")
    create.add_argument("--no-install", action="store_true", help="Fail instead of installing missing Brave")
    create.add_argument("--dry-run", action="store_true", help="Preview without creating files or opening Brave")
    vpn = commands.add_parser("vpnCheck", help="Check one profile's VPN without opening Brave")
    vpn.add_argument("profile_name", help="Profile directory name")
    vpn.add_argument("--profiles-dir", default="./profiles")
    vpn.add_argument("--timeout", type=float, default=15, help="Probe timeout in seconds (default: 15)")
    vpn.add_argument("--url", default="https://1.1.1.1/cdn-cgi/trace", help="HTTPS connectivity probe URL")
    history = commands.add_parser("history", help="Read execution history as JSON")
    history.add_argument("--state-db", default="./data/runs.sqlite")
    history.add_argument("--limit", default=20, type=int)
    history.add_argument("--run-id", help="Show profile and step details for one run")
    return result


def legacy_parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Launch Brave profiles sequentially, optionally running a Python task on each."
    )
    _profile_arguments(result, required=True)
    _browser_arguments(result)
    result.add_argument("--task", help="Python module defining async def run(context, profile)")
    return result


def _profile_json(profile):
    # Settings may contain account-specific data; discovery output omits their contents.
    return {"name": profile.name, "user_data_dir": str(profile.user_data_dir),
            "profile_directory": profile.profile_directory, "enabled": profile.enabled, "tags": profile.tags,
            "browser_display_name": profile.browser_display_name}


async def run(args) -> None:
    command = getattr(args, "command", None)
    if command in {"createProfile", "create-profile"}:
        from .tasks.createProfile import createProfile
        await createProfile(args.profile_name, profiles_dir=args.profiles_dir, executable=args.executable,
                            no_install=args.no_install, dry_run=args.dry_run)
        return
    if command == "vpnCheck":
        from .tasks.vpnCheck import vpnCheck
        await vpnCheck(args.profile_name, profiles_dir=args.profiles_dir, timeout=args.timeout, url=args.url)
        return
    if command == "history":
        print(json.dumps(read_history(args.state_db, limit=args.limit, run_id=args.run_id), indent=2))
        return
    profiles = discover_profiles(args.profiles_dir, args.layout)
    profiles = select_profiles(
        profiles, group=getattr(args, "group", None),
        groups_file=Path(getattr(args, "config_dir", "config")) / "groups.toml",
        names=getattr(args, "profile", None), tags=getattr(args, "tag", None),
        include_disabled=getattr(args, "include_disabled", False))
    if command == "profiles":
        print(json.dumps([_profile_json(profile) for profile in profiles], indent=2))
        return
    workflow = load_workflow(workflow_path(args.workflow, args.config_dir)) if command == "run" else None
    tasks = resolve_tasks(workflow) if workflow else None
    task_filename = getattr(args, "task", None)
    if args.headless and not workflow and not task_filename and not args.dry_run:
        raise ValueError("--headless requires --task so the browser can finish automatically.")
    executable = find_brave(args.executable)
    if args.dry_run:
        plan = {
            "executable": executable or "Brave will be installed using sudo + APT",
            "profiles": [_profile_json(profile) for profile in profiles],
        }
        if workflow:
            plan["workflow"] = asdict(workflow)
            plan["state_db"] = str(Path(args.state_db).expanduser().resolve())
            plan["artifacts_dir"] = str(Path(args.artifacts_dir).expanduser().resolve())
        print(json.dumps(plan, indent=2))
        return
    # Validate task and environment before changing the system.
    task = load_task(task_filename) if task_filename else None
    if hasattr(os, "getuid") and os.getuid() == 0:
        raise RuntimeError("Run Brave as your normal desktop user, not root.")
    if not args.headless and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("No desktop display found. Run in a desktop terminal or use --headless with --task.")
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise RuntimeError("Playwright is missing. Install the project with: python -m pip install -e .") from error
    if not executable:
        if args.no_install:
            raise RuntimeError("Brave is not installed. Remove --no-install to install it automatically.")
        install_brave()
        executable = find_brave()
        if not executable:
            raise RuntimeError("Installation finished but Brave could not be found on PATH.")
    async with async_playwright() as playwright:
        if workflow:
            with StateStore(args.state_db) as state:
                summary = await run_workflow(playwright, profiles, executable, workflow, tasks, state,
                                             args.artifacts_dir, headless=args.headless, trace=not args.no_trace)
            if summary.failed:
                raise RuntimeError(f"Workflow {workflow.name} failed for {summary.failed} profile(s); "
                                   f"inspect history with --run-id {summary.run_id}.")
        else:
            await run_profiles(playwright, profiles, executable, task, args.headless)


def main(argv=None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    modern = not arguments or not arguments[0].startswith("-") or arguments[0] in {"-h", "--help"}
    args = (parser() if modern else legacy_parser()).parse_args(arguments)
    # asyncio.run handles SIGINT by cancelling the runner, executing its finally blocks.
    previous_term = signal.getsignal(signal.SIGTERM)

    def terminate(*_):
        signal.raise_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, terminate)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
