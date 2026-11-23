from ...models import TaskResult
from ...websites.demo import DemoSite


async def run(ctx):
    page = await ctx.browser.new_page()
    site = DemoSite(page)
    expected = str(ctx.params.get("prefix", "")) + ctx.profile.name
    try:
        await site.open()
        result = await site.submit_name(expected)
        screenshot = ctx.artifact_dir / "result.png"
        await page.screenshot(path=str(screenshot))
        await page.close()
        return TaskResult(outputs={"value": result, "expected": expected, "screenshot_path": str(screenshot)})
    finally:
        site.close()
