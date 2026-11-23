"""A local screen automation fixture, with no account or network required."""

from pathlib import Path
import tempfile


class DemoSite:
    def __init__(self, page):
        self.page = page
        self.temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-screen-demo-")

    async def open(self):
        path = Path(self.temporary.name) / "demo.html"
        path.write_text('''<!doctype html><title>Screen automation demo</title>
            <style>body{font:24px sans-serif;padding:60px}input,button{font:24px sans-serif;margin:16px}
            #result{margin-top:60px}</style>
            <label for="name">Name</label><input id="name" autocomplete="off">
            <button onclick="document.querySelector('#result').textContent='Result: '+document.querySelector('#name').value">Apply</button>
            <p id="result"></p>''', encoding="utf-8")
        await self.page.goto(path.as_uri())
        await self.page.wait_for_text("Apply")

    async def submit_name(self, name):
        await self.page.click_text("Name")
        await self.page.fill_text(name)
        await self.page.click_text("Apply")
        await self.page.wait_for_text(f"Result: {name}")
        return name

    def close(self):
        self.temporary.cleanup()
