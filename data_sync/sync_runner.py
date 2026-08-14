"""
sync_runner.py — Data Sync orchestrator (thin).

ONE login via focus_common.focus_session(), then the requested fetchers
run in sequence on the same session:

    python sync_runner.py --what daybook --days 60
    python sync_runner.py --what daybook --import-file path\\to\\book.xlsx
    python sync_runner.py --what pricebook          (pending details)
    python sync_runner.py --what all --days 60

Each dataset lives in its own fetch_*.py; shared browsing (frozen-login
copy, menus, popups, export Yes) lives in focus_common.py.
"""
from __future__ import annotations
import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

import focus_common
import fetch_daybook
import fetch_pricebook
import fetch_items


def _mark_last_sync(what: str, summary: str):
    try:
        conn = sqlite3.connect(str(focus_common.DASHBOARD_DB))
        conn.execute(
            "INSERT INTO agent_settings (agent, key, value, label, category) "
            "VALUES ('data_sync', ?, ?, 'Last sync', 'status') "
            "ON CONFLICT(agent, key) DO UPDATE SET value = excluded.value",
            (f"last_{what}",
             f"{datetime.now().strftime('%d %b %Y %I:%M %p')} — {summary}"))
        conn.commit()
        conn.close()
    except Exception:
        pass


async def run_sync(what: str, days: int, import_file: str = None) -> int:
    # Import-only shortcut (no browser)
    if import_file:
        if what != "daybook":
            print("❌ --import-file currently supports daybook only")
            return 1
        print(f"📄 Import-only mode: {import_file}")
        result = fetch_daybook.import_file(Path(import_file))
        s = result["parsed"]
        _mark_last_sync("daybook",
                        f"{s['rows']} rows {s['date_from']}→{s['date_to']}")
        print("✅ Day book sync complete.")
        return 0

    targets = (["daybook", "pricebook", "items"] if what == "all"
               else [what])
    failures = 0
    async with focus_common.focus_session(tag=what) as (page, debug_dir):
        for t in targets:
            print(f"\n═══ {t.upper()} ═══")
            try:
                if t == "daybook":
                    result = await fetch_daybook.run(page, debug_dir, days)
                    s = result["parsed"]
                    _mark_last_sync("daybook",
                                    f"{s['rows']} rows "
                                    f"{s['date_from']}→{s['date_to']}")
                    print("✅ Day book sync complete.")
                elif t == "pricebook":
                    result = await fetch_pricebook.run(page, debug_dir)
                    _mark_last_sync("pricebook", str(result)[:120])
                    print("✅ Price book sync complete.")
                elif t == "items":
                    result = await fetch_items.run(page, debug_dir)
                    _mark_last_sync("items", str(result)[:120])
                    print("✅ Items master sync complete.")
            except NotImplementedError as e:
                print(f"⏸ {t}: {e}")
                failures += 1
            except Exception as e:
                print(f"❌ {t} failed: {e}")
                import traceback
                traceback.print_exc()
                failures += 1
            # Back to Focus home for the next fetcher
            if len(targets) > 1 and t != targets[-1]:
                try:
                    await page.goto(focus_common.FOCUS_BASE_URL,
                                    wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                except Exception:
                    pass
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Focus Data Sync runner")
    ap.add_argument("--what", default="daybook",
                    choices=["daybook", "pricebook", "items", "all"])
    ap.add_argument("--days", type=int, default=60,
                    help="Day book window: today-N .. today")
    ap.add_argument("--import-file", default=None,
                    help="Skip the browser; import this xlsx (daybook)")
    args = ap.parse_args()

    print("━" * 52)
    print(f"▶  DATA SYNC — {args.what}  —  "
          f"{datetime.now().strftime('%d %b %Y %I:%M:%S %p')}")
    print("━" * 52)
    print(f"   Focus agent dir: {focus_common.FOCUS_AGENT_DIR}")
    print(f"   Dashboard DB:    {focus_common.DASHBOARD_DB}")

    return asyncio.run(run_sync(args.what, args.days, args.import_file))


if __name__ == "__main__":
    sys.exit(main())
