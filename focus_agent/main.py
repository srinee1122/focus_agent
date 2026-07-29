"""
main.py — Focus ERP Low Price Monitoring Agent
Sends one WhatsApp message per flagged Sales Order.

Usage:
  python main.py         → run now + schedule daily
  python main.py --now   → run once and exit
"""

import asyncio
import sys
import time
import schedule
from datetime import datetime

import config
from credentials import get_credentials
from focus_scraper import FocusScraper
from formatter import format_single_alert
from whatsapp_sender import WhatsAppSender
import config


async def run_agent():
    print("\n" + "=" * 50)
    print(f"🤖 Focus ERP Agent — {datetime.now().strftime('%d %b %Y %I:%M %p')}")
    print("=" * 50)

    try:
        username, password = get_credentials(config.CREDENTIALS_FILE)

        print("\n📡 Connecting to Focus ERP...")
        scraper = FocusScraper()
        alerts  = await scraper.run(username, password)

        if not alerts:
            print("\nℹ️  No price issues found today. Nothing to send.")
            return

        print(f"\n📋 {len(alerts)} order(s) flagged. Preview:\n")
        for alert in alerts:
            msg = format_single_alert(alert)
            print("── MESSAGE ──────────────────────────────────────")
            print(msg)
            print("─────────────────────────────────────────────────\n")

        # Always save Excel backup first
        from excel_exporter import export_alerts
        print("\n📊 Saving Excel backup...")
        excel_path = export_alerts(alerts, output_dir=config.DOWNLOAD_DIR)

        print(f"\n📲 Sending {len(alerts)} message(s) to group: '{config.WHATSAPP_GROUP}'...")
        import os as _os, json as _json
        send_text  = _os.environ.get("AGENT_SEND_TEXT",  "true").lower() == "true"
        send_image = _os.environ.get("AGENT_SEND_IMAGE", "true").lower() == "true"
        wa_groups_raw = _os.environ.get("AGENT_WA_GROUPS", "")
        print(f"   🔧 AGENT_SEND_TEXT={_os.environ.get('AGENT_SEND_TEXT','not set')}")
        print(f"   🔧 AGENT_SEND_IMAGE={_os.environ.get('AGENT_SEND_IMAGE','not set')}")
        print(f"   🔧 AGENT_WA_GROUPS={wa_groups_raw or 'not set'}")
        if wa_groups_raw:
            # Try strict JSON first, then tolerant parsing
            parsed = None
            try:
                parsed = _json.loads(wa_groups_raw)
            except Exception:
                # Try adding quotes if it looks like [Group Name]
                try:
                    fixed = wa_groups_raw.strip()
                    if fixed.startswith('[') and fixed.endswith(']'):
                        inner = fixed[1:-1].strip()
                        # Wrap each comma-separated item in quotes
                        items = [f'"{x.strip().strip(chr(39)).strip(chr(34))}"' for x in inner.split(',')]
                        parsed = _json.loads('[' + ','.join(items) + ']')
                        print(f"   ⚠️  Fixed malformed JSON: {wa_groups_raw} → {parsed}")
                except Exception as e2:
                    print(f"   ❌ Could not parse groups JSON: {e2}")

            if parsed:
                config.WHATSAPP_GROUPS = parsed
                print(f"   ✅ Groups from dashboard: {config.WHATSAPP_GROUPS}")
            else:
                grps = getattr(config, 'WHATSAPP_GROUPS', [getattr(config, 'WHATSAPP_GROUP', 'not set')])
                print(f"   📋 Falling back to config.py: {grps}")
        else:
            grps = getattr(config, 'WHATSAPP_GROUPS', [getattr(config, 'WHATSAPP_GROUP', 'not set')])
            print(f"   📋 Groups from config.py: {grps}")
        sender = WhatsAppSender()
        await sender.send(alerts, send_text=send_text, send_image=send_image)

        print(f"\n🎉 Done at {datetime.now().strftime('%I:%M %p')}")

    except Exception as e:
        print(f"\n❌ Agent failed: {e}")


def job():
    asyncio.run(run_agent())


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--now" in args:
        asyncio.run(run_agent())
    else:
        print(f"⏰ Scheduled daily at {config.SCHEDULE_TIME}")
        schedule.every().day.at(config.SCHEDULE_TIME).do(job)
        job()
        while True:
            schedule.run_pending()
            time.sleep(60)
