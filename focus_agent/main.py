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
        # Send Specific — pass target vouchers directly to skip grid scan
        import os as _os2
        send_only_raw = _os2.environ.get("AGENT_SEND_ONLY", "").strip()
        target_vouchers = [s.strip() for s in send_only_raw.split(",") if s.strip()] if send_only_raw else None
        alerts = await scraper.run(username, password, target_vouchers=target_vouchers)

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
        # Helper: normalise SO number for comparison
        def _norm_so(v):
            return v.replace("SO : ", "").replace("SO:", "").strip()

        # ── 1. Send Only filter (whitelist) ───────────────────────────────
        send_only_raw = _os.environ.get("AGENT_SEND_ONLY", "")
        if send_only_raw:
            only_list = {s.strip() for s in send_only_raw.split(",") if s.strip()}
            before = len(alerts)
            alerts = [a for a in alerts if _norm_so(a["voucher"]) in only_list]
            print(f"   🎯 Send-only mode: keeping {len(alerts)}/{before} SO(s) matching {send_only_raw}")

        # ── 2. Skip SO numbers (blacklist) ────────────────────────────────
        skip_orders_raw = _os.environ.get("AGENT_SKIP_ORDERS", "")
        if skip_orders_raw:
            skip_list = {s.strip() for s in skip_orders_raw.split(",") if s.strip()}
            before = len(alerts)
            alerts = [a for a in alerts if _norm_so(a["voucher"]) not in skip_list]
            if before - len(alerts):
                print(f"   🚫 Skipped {before - len(alerts)} SO(s) by SO number")

        # ── 3. Skip by customer name ──────────────────────────────────────
        skip_customers_raw = _os.environ.get("AGENT_SKIP_CUSTOMERS", "")
        if skip_customers_raw:
            skip_cust = {s.strip().lower() for s in skip_customers_raw.split(",") if s.strip()}
            before = len(alerts)
            alerts = [a for a in alerts if a.get("party","").strip().lower() not in skip_cust]
            if before - len(alerts):
                print(f"   🚫 Skipped {before - len(alerts)} SO(s) by customer name")

        # ── Filter already-sent SOs ──────────────────────────────────────
        # Send Specific mode always bypasses sent history
        send_only_active = bool(_os.environ.get("AGENT_SEND_ONLY", "").strip())
        skip_sent = (not send_only_active) and (_os.environ.get("AGENT_SKIP_SENT", "true").lower() == "true")
        if send_only_active:
            print("   🎯 Send Specific mode — bypassing sent history check")
        dashboard_db= _os.environ.get("DASHBOARD_DB", "")
        print(f"   🔧 skip_sent_sos={skip_sent}")

        if skip_sent and dashboard_db and _os.path.exists(dashboard_db):
            import sqlite3 as _sq
            conn = _sq.connect(dashboard_db)
            # Load already-sent voucher+item combos for today
            already_sent = set(
                (row[0], row[1]) for row in conn.execute("""
                    SELECT voucher, item_name FROM sent_alerts
                    WHERE agent='low_price'
                      AND date(sent_at) = date('now', 'localtime')
                """).fetchall()
            )
            conn.close()

            filtered_alerts = []
            for alert in alerts:
                voucher = alert["voucher"]
                # Keep only items not yet sent for this SO today
                new_items = [
                    item for item in alert["items"]
                    if (voucher, item["item"]) not in already_sent
                ]
                if not new_items:
                    print(f"   ⏭️  {voucher} — all items already sent today, skipping.")
                elif len(new_items) < len(alert["items"]):
                    skipped_items = [i["item"] for i in alert["items"] if i not in new_items]
                    print(f"   ⚡ {voucher} — {len(new_items)} new item(s), "
                          f"{len(skipped_items)} already sent today.")
                    filtered_alerts.append({**alert, "items": new_items})
                else:
                    filtered_alerts.append(alert)

            alerts = filtered_alerts
            if not alerts:
                print("   ✅ All SO items already sent today — nothing to do.")
        # ─────────────────────────────────────────────────────────────────

        if alerts:
            sender = WhatsAppSender()
            await sender.send(alerts, send_text=send_text, send_image=send_image)

            # Record sent SOs in dashboard DB
            if dashboard_db and _os.path.exists(dashboard_db):
                import sqlite3 as _sq
                conn = _sq.connect(dashboard_db)
                rows = []
                for a in alerts:
                    for item in a.get("items", []):
                        rows.append(("low_price", a["voucher"], item["item"]))
                from datetime import datetime as _dt
                now_local = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.executemany(
                    "INSERT INTO sent_alerts (agent, voucher, item_name, sent_at) VALUES (?,?,?,?)",
                    [(r[0], r[1], r[2], now_local) for r in rows]
                )
                conn.commit()
                conn.close()
                print(f"   📝 Recorded {len(rows)} sent item(s) across {len(alerts)} SO(s).")

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
