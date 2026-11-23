"""Launch a local page, locate its controls on screen, and click/type like a user."""

from zepuppeteer.websites.demo import DemoSite


async def run(context, profile):
    page = await context.new_page()
    site = DemoSite(page)
    try:
        await site.open()
        await site.submit_name(profile.name)
        print(f"Typed and clicked visible controls for {profile.name}.", flush=True)
    finally:
        site.close()
        await page.close()
