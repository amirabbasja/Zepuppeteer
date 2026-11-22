"""Demonstrate tab creation, typing, clicking, and scrolling on a local page."""

from playwright.async_api import BrowserContext, expect
from zepuppeteer.brave import Profile


async def run(context: BrowserContext, profile: Profile) -> None:
    page = await context.new_page()
    try:
        await page.set_content("""<!doctype html><html><body>
          <label>Name <input id="name"></label>
          <button id="submit" onclick="document.querySelector('#result').textContent = document.querySelector('#name').value">Apply</button>
          <p id="result"></p><div style="height:2000px">Scroll demo</div>
          </body></html>""")
        await page.locator("#name").press_sequentially(profile.name)
        await page.locator("#submit").click()
        await expect(page.locator("#result")).to_have_text(profile.name)
        await page.mouse.wheel(delta_x=0, delta_y=600)
        await page.wait_for_function("window.scrollY > 0")
        print(f"Opened a tab, typed, clicked, and scrolled for {profile.name}.", flush=True)
        # For real tasks: await page.goto("https://example.com")
    finally:
        await page.close()
