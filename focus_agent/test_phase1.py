"""
test_phase1.py — Full pipeline test. Previews each WhatsApp message.
"""
import asyncio
from credentials import get_credentials
from focus_scraper import FocusScraper
from formatter import format_single_alert
import config

async def test():
    print("=" * 50)
    print("  FULL PIPELINE TEST")
    print("=" * 50)
    config.HEADLESS = False
    username, password = get_credentials(config.CREDENTIALS_FILE)
    scraper = FocusScraper()
    alerts  = await scraper.run(username, password)

    print(f"\n📋 {len(alerts)} order(s) flagged.\n")
    for alert in alerts:
        msg = format_single_alert(alert)
        print("── WHATSAPP MESSAGE ─────────────────────────────")
        print(msg)
        print("─────────────────────────────────────────────────\n")

    print("✅ Test complete!")

if __name__ == "__main__":
    asyncio.run(test())
