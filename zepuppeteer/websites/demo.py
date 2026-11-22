"""A local website adapter for smoke tests; no account or network is required."""


class DemoSite:
    def __init__(self, page):
        self.page = page

    async def open(self):
        await self.page.set_content("""<!doctype html><html><body>
            <label>Name <input id="name"></label>
            <button onclick="document.querySelector('#result').textContent = document.querySelector('#name').value">Apply</button>
            <p id="result" role="status"></p>
            </body></html>""")

    async def submit_name(self, name: str) -> str:
        from playwright.async_api import expect

        await self.page.get_by_label("Name", exact=True).fill(name)
        await self.page.get_by_role("button", name="Apply", exact=True).click()
        result = self.page.get_by_role("status")
        await expect(result).to_have_text(name)
        return await result.inner_text()
