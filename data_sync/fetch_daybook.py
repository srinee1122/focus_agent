"""
fetch_daybook.py — Sales day book fetcher for the Data Sync agent.

Receives a logged-in Focus page from focus_common.focus_session() and:
  Financials → Reports → Sales Register → Sales day book
  → Date Range (today-N .. today) → Default Layout → Excel export (Yes)
  → import via sales_agent parsers (replace-by-voucher/item dedupe).
"""
from __future__ import annotations
import asyncio
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from focus_common import (DASHBOARD_DB, DOWNLOAD_DIR, click_menu,
                          confirm_export_modal, dismiss_popup,
                          set_date_input, shot, wait_page_ready)

FMT = "%d/%m/%Y"    # Focus date format


async def download(page, debug_dir: Path, days: int) -> Path:
    d_to = date.today()
    d_from = d_to - timedelta(days=days)
    print(f"📅 Window: {d_from.strftime(FMT)} → {d_to.strftime(FMT)} "
          f"({days} days)")

    print("📂 Navigating to Sales day book...")
    await wait_page_ready(page, "Focus home", settle=2.0)
    await click_menu(page, "Financials", debug_dir, "1_financials",
                     timeout_s=60)
    await click_menu(page, "Reports", debug_dir, "2_reports")
    await click_menu(page, "Sales Register", debug_dir,
                     "3_sales_register", timeout_s=45)
    await wait_page_ready(page, "Sales Register page", settle=2.5)
    await dismiss_popup(page)

    # Click "Sales day book" and VERIFY the report screen loads
    opened = False
    for attempt in range(1, 5):
        await dismiss_popup(page)
        await click_menu(page, ["Sales day book", "Sales Day Book"],
                         debug_dir, f"4_sales_day_book_a{attempt}",
                         timeout_s=20)
        try:
            await page.wait_for_selector("#DateOptions_", state="attached",
                                         timeout=15_000)
            opened = True
            break
        except Exception:
            print(f"   … report screen not up yet (attempt {attempt}), "
                  f"retrying")
            await wait_page_ready(page, settle=1.5)
    if not opened:
        await shot(page, debug_dir, "FAIL_daybook_open")
        try:
            (debug_dir / "FAIL_daybook_open.html").write_text(
                await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("Clicked 'Sales day book' but the report screen "
                           "(#DateOptions_) never appeared — see sync_debug")
    await wait_page_ready(page, "Sales day book")
    await dismiss_popup(page)
    await shot(page, debug_dir, "5_report_page")

    # ── Date range ──
    print("🗓  Setting date range...")
    await page.select_option("#DateOptions_", "0")     # Date Range
    await asyncio.sleep(1.2)
    await dismiss_popup(page)
    await set_date_input(page, "id_starting_date_", d_from.strftime(FMT))
    await dismiss_popup(page)
    await set_date_input(page, "id_ending_date_", d_to.strftime(FMT))
    await dismiss_popup(page)
    await shot(page, debug_dir, "6_dates_set")
    print(f"   Start {d_from.strftime(FMT)} · End {d_to.strftime(FMT)}")

    # ── Layout ──
    print("🧾 Selecting Default Layout...")
    try:
        await page.wait_for_selector("#RITLayout_", timeout=20_000)
        try:
            await page.select_option("#RITLayout_", label="Default Layout")
        except Exception:
            await page.select_option("#RITLayout_", "181")
        await asyncio.sleep(1.2)
        await dismiss_popup(page)
    except Exception:
        await shot(page, debug_dir, "FAIL_layout")
        raise RuntimeError("Layout selector #RITLayout_ not found — see "
                           "sync_debug screenshot")

    # ── Export (icon → Yes confirmation → download) ──
    print("⬇  Exporting to Excel...")
    await dismiss_popup(page)
    await shot(page, debug_dir, "7_before_export")
    try:
        async with page.expect_download(timeout=180_000) as dl_info:
            await page.locator("i.icon-import-from-excel").first.click()
            await confirm_export_modal(page, timeout_s=45)
        download_obj = await dl_info.value
    except Exception:
        await shot(page, debug_dir, "FAIL_export")
        try:
            (debug_dir / "FAIL_export.html").write_text(
                await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("Excel export did not start — see sync_debug")

    out = DOWNLOAD_DIR / (f"daybook_{d_from.strftime('%Y%m%d')}_"
                          f"{d_to.strftime('%Y%m%d')}.xlsx")
    await download_obj.save_as(str(out))
    print(f"   Downloaded: {out.name} ({out.stat().st_size // 1024} KB)")
    await asyncio.sleep(1.0)
    await dismiss_popup(page)
    await shot(page, debug_dir, "8_after_export")
    return out


_SALES_DDL = """
    CREATE TABLE IF NOT EXISTS sales_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, voucher TEXT, salesman TEXT, customer TEXT,
        item_name TEXT, qty REAL, unit TEXT, qty_pieces REAL,
        qty_ctn REAL, rate REAL, rate_pcs REAL, gross REAL,
        qty_per_ctn REAL, base_link_doc TEXT, segment TEXT,
        is_foc INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_sales_item_date
        ON sales_data(item_name, date);
    CREATE INDEX IF NOT EXISTS idx_sales_voucher_item
        ON sales_data(voucher, item_name);
"""


def import_file(xlsx_path: Path) -> dict:
    """Import with the battle-tested sales_agent parser (dedupe-safe).
    Self-sufficient: creates the sales_data table if the dashboard's
    sales endpoints have never run on this DB (identical DDL)."""
    import sales_module
    rows, stats = sales_module.parse_sales_excel(xlsx_path.read_bytes())
    conn = sqlite3.connect(str(DASHBOARD_DB))
    try:
        conn.executescript(_SALES_DDL)
        result = sales_module.upsert_sales(conn, rows)
    finally:
        conn.close()
    print(f"📥 Imported: {stats['rows']} rows "
          f"({stats['date_from']} → {stats['date_to']}, "
          f"{stats['items']} items, {stats['salesmen']} salesmen)")
    print(f"   DB: {result.get('inserted', 0)} rows written · "
          f"{result.get('replaced', 0)} prior rows replaced")
    return {"parsed": stats, "db": result}


async def run(page, debug_dir: Path, days: int) -> dict:
    xlsx = await download(page, debug_dir, days)
    return import_file(xlsx)
