"""Own browser lifecycle and execute sequential workflows with bounded attempts."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from .screen import launch_browser
from .config import json_text
from .models import NeedsAttention, RunSummary, TaskContext, TaskResult
from .notifications import notify_attention
from .vpn import colab_uses_vpn, connect_vpn, browser_proxy


async def run_profiles(profiles, executable, task=None, headless=False):
    """Run screen tasks sequentially; manual sessions wait for browser closure."""
    for profile in profiles:
        print(f"Opening profile: {profile.name}", flush=True)
        context = await launch_browser(executable, profile, headless=headless, interactive=task is None)
        try:
            if task:
                await task(context=context, profile=profile)
                print(f"Completed profile: {profile.name}", flush=True)
            else:
                print("Close this browser to continue to the next profile. Ctrl+C stops the runner.", flush=True)
                await context.wait_closed()
        finally:
            await context.close()


def _component(value: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip(".-")[:64] or "item"
    return f"{readable}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


async def _evidence(browser, directory: Path, error: str) -> None:
    """Evidence collection is best effort and must preserve the original failure."""
    try:
        pages = []
        for index, page in enumerate(browser.pages[:5] if browser is not None else []):
            details = {"url": page.url}
            try:
                filename = f"page-{index + 1}.png"
                await page.screenshot(path=str(directory / filename), timeout=5000)
                details["screenshot"] = filename
            except Exception as failure:
                details["screenshot_error"] = _error_text(failure)
            pages.append(details)
        (directory / "failure.json").write_text(
            json.dumps({"error": error, "pages": pages}, indent=2), encoding="utf-8")
    except Exception as failure:
        print(f"Could not save failure evidence: {failure}", file=sys.stderr, flush=True)


async def _stop_trace(browser, started: bool, path: Path | None = None) -> None:
    if started:
        try:
            if path is None:
                await browser.tracing.stop()
            else:
                await browser.tracing.stop(path=str(path))
        except Exception as error:
            print(f"Could not finish browser trace: {error}", file=sys.stderr, flush=True)


async def _steps(browser, profile, workflow, tasks, state, run_id, directory, trace, vpn=None) -> str | None:
    results = {}
    for step in workflow.steps:
        if step.input_from and results[step.input_from].status != "success":
            result = TaskResult(status="skipped", message=f"Input step {step.input_from} was skipped.")
            state.finish_step(run_id, profile.name, step.id, result.status, message=result.message)
            results[step.id] = result
            continue
        for attempt in range(1, step.retries + 2):
            attempt_dir = directory / _component(step.id) / f"attempt-{attempt}"
            attempt_dir.mkdir(parents=True)
            state.start_step(run_id, profile.name, step.id, attempt_dir)
            print(f"[{profile.name}] {step.id} (attempt {attempt})", flush=True)
            tracing = False
            try:
                if trace:
                    try:
                        await browser.tracing.start(screenshots=True, snapshots=True)
                        tracing = True
                    except Exception as error:
                        print(f"Could not start browser trace: {error}", file=sys.stderr, flush=True)
                # A task cannot mutate the next profile's params or an earlier result.
                inputs = results[step.input_from].outputs if step.input_from else {}
                ctx = TaskContext(browser, profile, json.loads(json_text(step.params)),
                                  json.loads(json_text(inputs)), attempt_dir, run_id, step.id, vpn=vpn)
                operation = tasks[step.task](ctx)
                if vpn is not None:
                    operation = vpn.run(operation)
                result = await asyncio.wait_for(operation, timeout=step.timeout_seconds)
                if not isinstance(result, TaskResult):
                    raise TypeError(f"Task {step.task} must return TaskResult, got {type(result).__name__}.")
                if result.status not in ("success", "skipped") or not isinstance(result.message, str):
                    raise ValueError("TaskResult requires status success/skipped and a string message.")
                if not isinstance(result.outputs, dict):
                    raise ValueError("TaskResult.outputs must be a JSON-compatible dictionary.")
                json_text(result.outputs)
            except asyncio.CancelledError:
                state.finish_step(run_id, profile.name, step.id, "cancelled", error="Cancelled")
                await _stop_trace(browser, tracing, attempt_dir / "trace.zip")
                raise
            except Exception as error:
                message = (f"TimeoutError: step exceeded {step.timeout_seconds:g} seconds"
                           if isinstance(error, asyncio.TimeoutError) else _error_text(error))
                state.finish_step(run_id, profile.name, step.id, "failed", error=message)
                if isinstance(error, NeedsAttention):
                    await notify_attention(profile, step.task.split(".", 1)[0], str(error))
                await _evidence(browser, attempt_dir, message)
                await _stop_trace(browser, tracing, attempt_dir / "trace.zip")
                if attempt > step.retries or isinstance(error, NeedsAttention):
                    return f"{step.id}: {message}"
                print(f"[{profile.name}] Retrying {step.id}: {message}", file=sys.stderr, flush=True)
                try:
                    await asyncio.sleep(step.retry_delay_seconds)
                except asyncio.CancelledError:
                    state.finish_step(run_id, profile.name, step.id, "cancelled", error="Cancelled before retry")
                    raise
            else:
                await _stop_trace(browser, tracing)
                state.finish_step(run_id, profile.name, step.id, result.status,
                                  outputs=result.outputs, message=result.message)
                results[step.id] = result
                break
    return None


async def _profile(profile, executable, workflow, tasks, state, run_id,
                   directory, headless, trace) -> str:
    browser, status, error = None, "success", None
    resources, vpn = AsyncExitStack(), None
    state.start_profile(run_id, profile.name)
    print(f"Opening profile: {profile.name}", flush=True)
    try:
        directory.mkdir(parents=True)
        if any(step.task == "colab.open_notebook" for step in workflow.steps) and colab_uses_vpn(profile):
            vpn = await resources.enter_async_context(connect_vpn(profile))
        proxy = await resources.enter_async_context(browser_proxy(vpn.proxy)) if vpn is not None else None
        browser = await launch_browser(executable, profile, headless=headless, proxy=proxy)
        error = await _steps(browser, profile, workflow, tasks, state, run_id, directory, trace, vpn=vpn)
        if error:
            status = "failed"
    except asyncio.CancelledError:
        status, error = "cancelled", "Cancelled"
        state.cancel_running(run_id, profile.name)
        raise
    except Exception as failure:
        status, error = "failed", _error_text(failure)
        state.fail_running(run_id, profile.name, error)
        if isinstance(failure, NeedsAttention):
            await notify_attention(profile, "colab", str(failure))
        await _evidence(browser, directory, error)
    finally:
        cancelled_during_close = False
        if browser is not None or vpn is not None:
            async def close_resources():
                try:
                    if browser is not None:
                        await browser.close()
                finally:
                    await resources.aclose()
            cleanup = asyncio.create_task(close_resources())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                status, error = "cancelled", "Cancelled during browser cleanup"
                state.cancel_running(run_id, profile.name)
                # Finish the already-started close before returning control to the CLI.
                try:
                    await cleanup
                except Exception as failure:
                    error += f"; {_error_text(failure)}"
                cancelled_during_close = True
            except Exception as failure:
                if status != "cancelled":
                    status, error = "failed", f"{error + '; ' if error else ''}Browser cleanup: {_error_text(failure)}"
        state.skip_pending(run_id, profile.name, error or "Workflow finished")
        state.finish_profile(run_id, profile.name, status, error)
        if cancelled_during_close:
            raise asyncio.CancelledError
    if error:
        print(f"[{profile.name}] {error}", file=sys.stderr, flush=True)
    return status


async def run_workflow(profiles, executable, workflow, tasks, state,
                       artifact_root: str | Path = "artifacts", *, headless=False, trace=True) -> RunSummary:
    """One browser per profile, ordered steps, and a committed result after each step."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:12]
    directory = Path(artifact_root).expanduser().resolve() / run_id
    directory.mkdir(parents=True)
    state.begin_run(run_id, workflow, profiles, directory)
    counts = {"success": 0, "failed": 0}
    try:
        for profile in profiles:
            status = await _profile(profile, executable, workflow, tasks, state, run_id,
                                    directory / _component(profile.name), headless, trace)
            counts[status] += 1
            if status == "failed" and workflow.on_profile_failure == "stop":
                break
    except asyncio.CancelledError:
        state.finish_run(run_id, "cancelled")
        raise
    except BaseException:
        state.finish_run(run_id, "failed")
        raise
    status = "failed" if counts["failed"] else "success"
    state.finish_run(run_id, status)
    summary = RunSummary(run_id, status, counts["success"], counts["failed"],
                         len(profiles) - sum(counts.values()), directory)
    print(json.dumps({**asdict(summary), "artifact_dir": str(directory)}, indent=2), flush=True)
    return summary
