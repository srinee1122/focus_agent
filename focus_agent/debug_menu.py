"""
debug_menu.py
Prints all visible links after opening Inventory > Order Management
so we can find the exact menu label for the Sales Order Report.
"""
import asyncio
from credentials import get_credentials
from playwright.async_api import async_playwright
import config

FOCUS_BASE_URL = "https://ymt-9.focus9erp.com/focusx"

async def debug():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        context.set_default_timeout(60_000)
        page = await context.new_page()

        print("🌐 Opening Focus ERP...")
        await page.goto(FOCUS_BASE_URL, wait_until="domcontentloaded", timeout=90_000)
        await asyncio.sleep(3)

        print("🔐 Logging in...")
        username, password = get_credentials(config.CREDENTIALS_FILE)
        await page.wait_for_selector("#txtUsername", timeout=30_000)
        await page.fill("#txtUsername", username)
        await page.fill("#txtPassword", password)
        await page.keyboard.press("Enter")
        await asyncio.sleep(4)
        print("   Logged in.")

        print("📂 Clicking Inventory...")
        await page.get_by_role("link", name="Inventory").first.click()
        await asyncio.sleep(2)

        print("📂 Clicking Order Management...")
        await page.get_by_role("link", name="Order Management").first.click()
        await asyncio.sleep(2)

        # Print ALL visible link texts so we can see exact labels
        links = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a'))
                .map(a => a.innerText.trim())
                .filter(t => t.length > 0);
        }""")

        print("\n── ALL VISIBLE LINKS ────────────────────────────")
        for link in links:
            print(f"  '{link}'")
        print("─────────────────────────────────────────────────")
        print("\nLook for the Sales Order Report label above ↑")
        print("Keeping browser open for 30 seconds...")
        await asyncio.sleep(30)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(debug())
