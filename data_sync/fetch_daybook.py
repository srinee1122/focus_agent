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

from focus_common import (DASHBOARD_DB, DOWNLOAD_DIR, shot,
                          wait_page_ready)

FMT = "%d/%m/%Y"    # Focus date format


# ── Page helpers (OWNED by this fetcher — page-specific by design;
#    similar code in other fetchers is deliberately duplicated) ──
# Confirm = the PROVEN ReadyToExport flow for this page.
async def click_menu(page, names, debug_dir: Path, step: str,
                      timeout_s: int = 30):
    """Direct approach: one JS pass that finds the element by exact text
    (case-insensitive) among menu-like tags and clicks it — works even
    when Focus keeps the element hidden (e.g. report list anchors).
    Polls every 0.5s up to timeout_s."""
    if isinstance(names, str):
        names = [names]
    wanted = [n.strip().lower() for n in names]
    js = """(wanted) => {
        const tags = ['a', 'span', 'li', 'button', 'div'];
        // Strict priority: only exact text matches, first name first.
        for (const w of wanted) {
            let hidden = null;
            for (const tag of tags) {
                for (const el of document.querySelectorAll(tag)) {
                    const t = (el.textContent || '').trim().toLowerCase();
                    if (t !== w) continue;
                    if (el.offsetParent !== null) {           // visible
                        el.click();
                        return (el.textContent || '').trim().slice(0, 60);
                    }
                    if (!hidden) hidden = el;                  // remember
                }
            }
            if (hidden) {
                hidden.click();
                return (hidden.textContent || '').trim().slice(0, 60);
            }
        }
        return null;
    }"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            hit = await page.evaluate(js, wanted)
        except Exception:
            hit = None
        if hit:
            print(f"   ▸ {hit}")
            await asyncio.sleep(1.5)
            return
        await asyncio.sleep(0.5)
    await shot(page, debug_dir, f"FAIL_menu_{step}")
    try:
        (debug_dir / f"FAIL_menu_{step}.html").write_text(
            await page.content(), encoding="utf-8")
    except Exception:
        pass
    raise RuntimeError(
        f"Could not find/click '{names[0]}' within {timeout_s}s — see "
        f"sync_debug ({debug_dir.name}/FAIL_menu_{step}.png / .html)")

async def dismiss_popup(page, quiet: bool = True) -> bool:
    """Dismiss the 'Export Report Data' modal (or any visible modal close
    control) if present. Returns True if something was dismissed."""
    js = """() => {
        // NOTE: the modal header is position:fixed, for which offsetParent
        // is null — so visibility must be judged by geometry, not
        // offsetParent.
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const cs = getComputedStyle(el);
            return cs.display !== 'none' && cs.visibility !== 'hidden';
        };
        for (const h of document.querySelectorAll('.modal-header')) {
            if (!visible(h)) continue;
            const title = (h.textContent || '').trim();
            if (!/export report data/i.test(title)) continue;
            // Click every plausible close control, innermost first
            const targets = [
                h.querySelector('.icon-close'),
                h.querySelector('[data-bs-dismiss="modal"]'),
                h.querySelector('[data-dismiss="modal"]'),
            ].filter(Boolean);
            for (const t of targets) {
                try { t.click(); } catch (e) {}
                try {
                    t.dispatchEvent(new MouseEvent('click',
                        {bubbles: true, cancelable: true, view: window}));
                } catch (e) {}
            }
            return targets.length ? 'Export Report Data' : null;
        }
        return null;
    }"""
    still_js = """() => {
        for (const h of document.querySelectorAll('.modal-header')) {
            const r = h.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 &&
                /export report data/i.test((h.textContent || ''))) {
                return true;
            }
        }
        return false;
    }"""
    try:
        hit = await page.evaluate(js)
    except Exception:
        hit = None
    if hit:
        await asyncio.sleep(0.8)
        try:
            if await page.evaluate(still_js):
                # Fallbacks: Escape key, then Playwright's own click
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.6)
                if await page.evaluate(still_js):
                    try:
                        await page.locator(
                            ".modal-header .icon-close").first.click(
                                force=True, timeout=3_000)
                    except Exception:
                        pass
                    await asyncio.sleep(0.6)
            closed = not await page.evaluate(still_js)
        except Exception:
            closed = True
        print(f"   ✖ Dismissed popup: {hit}" if closed
              else "   ⚠ Popup still visible after dismissal attempts")
        return closed
    return False

async def confirm_export_modal(page, timeout_s: int = 45) -> str:
    """Click the export confirmation Yes:
      <input type="button" class="FButton-Primary FPopupChildren"
             value="Yes" onclick="REPORTVIEW.ReadyToExport();">
    Searches EVERY frame (not just the main one), tries a trusted
    Playwright click AND a JS click AND the direct function call, and
    logs what it can see so failures are diagnosable."""
    find_js = """() => {
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const cands = [...document.querySelectorAll(
            'input[type=button], button')];
        const info = [];
        for (const b of cands) {
            if (!visible(b)) continue;
            const oc = b.getAttribute('onclick') || '';
            const val = (b.value || b.textContent || '').trim();
            info.push({val: val.slice(0, 25), oc: oc.slice(0, 45),
                       cls: (b.className || '').slice(0, 45)});
            if (info.length >= 15) break;
        }
        return info;
    }"""
    click_js = """() => {
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const cands = [...document.querySelectorAll(
            'input[type=button], button')];
        let el = cands.find(b =>
            (b.getAttribute('onclick') || '').includes('ReadyToExport')
            && visible(b));
        if (!el) el = cands.find(b =>
            (b.getAttribute('onclick') || '').includes('ReadyToExport'));
        if (el) {
            el.click();
            return 'js-click: ' + (el.value || el.textContent ||
                                   'Yes').trim().slice(0, 30);
        }
        try {
            if (window.REPORTVIEW &&
                typeof window.REPORTVIEW.ReadyToExport === 'function') {
                window.REPORTVIEW.ReadyToExport();
                return 'direct: REPORTVIEW.ReadyToExport()';
            }
        } catch (e) { return 'error: ' + e.message; }
        return null;
    }"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    reported = False
    while asyncio.get_event_loop().time() < deadline:
        for frame in page.frames:
            # 1) Trusted Playwright click on the exact element
            try:
                loc = frame.locator(
                    'input[onclick*="ReadyToExport"], '
                    'input.FPopupChildren[value="Yes"]').first
                if await loc.count() > 0:
                    try:
                        await loc.click(timeout=3_000)
                        print("   ☑ Export confirmed "
                              "(playwright click: Yes)")
                        return "playwright"
                    except Exception:
                        try:
                            await loc.click(force=True, timeout=3_000)
                            print("   ☑ Export confirmed "
                                  "(forced click: Yes)")
                            return "forced"
                        except Exception:
                            pass
            except Exception:
                pass
            # 2) JS click / direct function call in this frame
            try:
                hit = await frame.evaluate(click_js)
            except Exception:
                hit = None
            if hit and not str(hit).startswith("error"):
                print(f"   ☑ Export confirmed ({hit})")
                return str(hit)
            # One-time diagnostic of what this frame can see
            if not reported:
                try:
                    info = await frame.evaluate(find_js)
                    if info:
                        print(f"   🔎 visible buttons in frame "
                              f"{frame.url[:50]}: {info}")
                        reported = True
                except Exception:
                    pass
        await asyncio.sleep(0.5)
    print("   ⚠ Yes button not found in any frame within "
          f"{timeout_s}s — maybe direct download")
    return ""

async def set_date_input(page, input_id: str, value: str):
    """Set a Focus date input directly and fire its events — more reliable
    than driving the calendar popup."""
    ok = await page.evaluate(
        """([id, val]) => {
            const el = document.getElementById(id);
            if (!el) return false;
            el.value = val;
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur',   {bubbles: true}));
            return true;
        }""", [input_id, value])
    if not ok:
        raise RuntimeError(f"Date input #{input_id} not found on page")
    await asyncio.sleep(0.6)

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
