"""Validate workflow plans and resolve their task implementations before launch."""

import importlib
import inspect
import math
from pathlib import Path
import re

from .config import json_text, read_toml
from .models import Step, Workflow


_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", re.ASCII)


def _duration(value, name: str, *, allow_zero: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{name} must be a finite {'nonnegative' if allow_zero else 'positive'} number.")
    return float(value)


def workflow_path(value: str, config_dir: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.suffix == ".toml":
        return path
    if not _ID.fullmatch(value):
        raise ValueError("Use a workflow name or an explicit .toml path.")
    return Path(config_dir) / "workflows" / f"{value}.toml"


def load_workflow(path: str | Path) -> Workflow:
    data = read_toml(path)
    unknown = set(data) - {"name", "steps", "on_profile_failure"}
    if unknown:
        raise ValueError(f"Unknown workflow options: {', '.join(sorted(unknown))}")
    name = data.get("name", Path(path).stem)
    if not isinstance(name, str) or not _ID.fullmatch(name):
        raise ValueError("Workflow name must contain only letters, numbers, underscores, dots, or hyphens.")
    policy = data.get("on_profile_failure", "continue")
    if policy not in ("continue", "stop"):
        raise ValueError("on_profile_failure must be continue or stop.")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("A workflow requires at least one [[steps]] table.")
    steps, seen = [], set()
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise ValueError("Every workflow step must be a table.")
        unknown = set(raw) - {"id", "task", "params", "input_from", "timeout_seconds",
                              "retries", "retry_safe", "retry_delay_seconds"}
        if unknown:
            raise ValueError(f"Unknown step options: {', '.join(sorted(unknown))}")
        task = raw.get("task")
        if not isinstance(task, str) or not _MODULE.fullmatch(task):
            raise ValueError("Each step needs a dotted Python task name, for example demo.fill_form.")
        step_id = raw.get("id", task)
        if not isinstance(step_id, str) or not _ID.fullmatch(step_id) or step_id in seen:
            raise ValueError(f"Invalid or duplicate step id: {step_id!r}")
        source = raw.get("input_from")
        if source is not None and (not isinstance(source, str) or source not in seen):
            raise ValueError(f"input_from for {step_id} must reference an earlier step id.")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"params for {step_id} must be a table.")
        json_text(params)
        retries, retry_safe = raw.get("retries", 0), raw.get("retry_safe", False)
        if type(retries) is not int or retries < 0 or type(retry_safe) is not bool:
            raise ValueError("retries must be a nonnegative integer and retry_safe a boolean.")
        if retries and not retry_safe:
            raise ValueError(f"Step {step_id} enables retries but does not declare retry_safe = true.")
        steps.append(Step(step_id, task, params, source,
                          _duration(raw.get("timeout_seconds", 120), "timeout_seconds"), retries,
                          retry_safe, _duration(raw.get("retry_delay_seconds", 1), "retry_delay_seconds", allow_zero=True)))
        seen.add(step_id)
    return Workflow(name, tuple(steps), policy)


def resolve_tasks(workflow: Workflow) -> dict:
    tasks = {}
    for step in workflow.steps:
        if step.task in tasks:
            continue
        module = importlib.import_module(f"zepuppeteer.tasks.{step.task}")
        task = getattr(module, "run", None)
        if not inspect.iscoroutinefunction(task):
            raise ValueError(f"Task {step.task} must define async def run(ctx: TaskContext) -> TaskResult.")
        try:
            inspect.signature(task).bind(object())
        except TypeError as error:
            raise ValueError(f"Task {step.task} must accept one TaskContext argument.") from error
        tasks[step.task] = task
    return tasks
