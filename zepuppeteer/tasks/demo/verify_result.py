from pathlib import Path
from ...models import TaskResult


async def run(ctx):
    if ctx.inputs.get("value") != ctx.inputs.get("expected") or "value" not in ctx.inputs:
        raise RuntimeError("The form result does not match the requested name.")
    if not Path(ctx.inputs["screenshot_path"]).is_file():
        raise RuntimeError("The form's result screenshot is missing.")
    return TaskResult(outputs=ctx.inputs, message="Screen form result and evidence verified.")
