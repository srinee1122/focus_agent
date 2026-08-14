"""
fetch_pricebook.py — Seller Price Book fetcher for the Data Sync agent.

Navigation approach (differs from the day book): uses Focus's menu
SEARCH box instead of walking menus —

  1. Click #id_menu_search_input and type "Seller Price Book"
     (typing fires GENERAL.searchMenu via oninput)
  2. Click the result entry:
       <li class="treeview labeltext ..."
           onclick="SHORTCUT.openView(this,false,event)">
         <span id="76" ...>Seller Price Book</span></li>

CURRENT STAGE: opens the Seller Price Book view and stops with a
screenshot — export/update steps follow once the view's details are
provided.
"""
from __future__ import annotations
import asyncio
from pathlib import Path

from focus_common import shot, wait_page_ready

SEARCH_INPUT = "#id_menu_search_input"


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

async def open_view(page, debug_dir: Path):
    """Search for and open the Seller Price Book view."""
    print("🔎 Opening Seller Price Book via menu search...")
    await wait_page_ready(page, "Focus home", settle=2.0)
    await dismiss_popup(page)

    # 1. Focus the search box and type the view name (fires searchMenu)
    try:
        await page.wait_for_selector(SEARCH_INPUT, state="attached",
                                     timeout=45_000)
        await page.click(SEARCH_INPUT)
        await asyncio.sleep(0.4)
        await page.keyboard.type("Seller Price Book", delay=60)
        await asyncio.sleep(2.0)          # let suggestions render
    except Exception:
        await shot(page, debug_dir, "FAIL_menu_search")
        try:
            (debug_dir / "FAIL_menu_search.html").write_text(
                await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("Menu search input not found/typeable — see "
                           "sync_debug FAIL_menu_search.png")

    await shot(page, debug_dir, "1_search_results")

    # 2. Click the result (exact text; handles span/li, hidden fallback)
    await click_menu(page, "Seller Price Book", debug_dir,
                     "2_seller_price_book", timeout_s=25)

    # 3. Let the view load
    await asyncio.sleep(2.5)
    await wait_page_ready(page, "Seller Price Book view", settle=2.0)
    await dismiss_popup(page)
    await shot(page, debug_dir, "3_pricebook_view")
    print("✅ Seller Price Book view opened.")


PB_NAME = "SriAmbikasSellingPriceBook"
PB_INPUT = "#ctrlOptionProPriceBookH"
EXPORT_BTN = "#btnExporttoExcel"


async def select_pricebook(page, debug_dir: Path, name: str = PB_NAME):
    """Type the price book name into the option-control and commit it.
    The control loads its data on leave (PRICEBOOK.loadPBDataonLeave),
    so: type → Enter (pick suggestion) → Tab (leave) → wait for load."""
    print(f"📖 Selecting price book: {name}")
    try:
        await page.wait_for_selector(PB_INPUT, state="attached",
                                     timeout=45_000)
        await page.click(PB_INPUT)
        await asyncio.sleep(0.5)
        # Clear any existing value first
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.3)
        await page.keyboard.type(name, delay=50)
        await asyncio.sleep(2.0)                # suggestions from the API
        await shot(page, debug_dir, "4_pb_suggestions")
        await page.keyboard.press("Enter")      # take the match
        await asyncio.sleep(1.0)
        await page.keyboard.press("Tab")        # leave → loads PB data
        await asyncio.sleep(1.5)
        await wait_page_ready(page, "price book data", settle=2.5)
        await dismiss_popup(page)
        await shot(page, debug_dir, "5_pb_loaded")
    except Exception:
        await shot(page, debug_dir, "FAIL_pb_select")
        try:
            (debug_dir / "FAIL_pb_select.html").write_text(
                await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(f"Could not select price book '{name}' — see "
                           f"sync_debug FAIL_pb_select.png")


async def export_excel(page, debug_dir: Path) -> Path:
    """Click Export To Excel (confirming a Yes modal if one appears)
    and save the download."""
    from focus_common import DOWNLOAD_DIR
    from datetime import datetime as _dt
    print("⬇  Exporting price book to Excel...")
    await dismiss_popup(page)
    await shot(page, debug_dir, "6_before_export")
    try:
        async with page.expect_download(timeout=180_000) as dl_info:
            await page.locator(EXPORT_BTN).click()
            await confirm_export_modal(page, timeout_s=20)
        download_obj = await dl_info.value
    except Exception:
        await shot(page, debug_dir, "FAIL_pb_export")
        try:
            (debug_dir / "FAIL_pb_export.html").write_text(
                await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("Price book export did not start — see "
                           "sync_debug FAIL_pb_export.png")
    out = DOWNLOAD_DIR / f"pricebook_{_dt.now().strftime('%Y%m%d')}.xlsx"
    await download_obj.save_as(str(out))
    print(f"   Downloaded: {out.name} ({out.stat().st_size // 1024} KB)")
    await asyncio.sleep(1.0)
    await dismiss_popup(page)
    await shot(page, debug_dir, "7_after_export")
    return out


def upload_to_dashboard(xlsx_path: Path) -> dict:
    """Feed the downloaded export through the dashboard's OWN
    /api/pricebook/import endpoint — the exact code path of a manual
    Price Book upload (name+unit matching, purchase_type preserved,
    unmatched reported). Zero duplicated logic."""
    import json
    import os
    import urllib.request

    base = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8000/")
    if not base.endswith("/"):
        base += "/"
    token = os.environ.get("AGENT_AUTH_TOKEN", "")

    data = xlsx_path.read_bytes()
    boundary = "----syncpb"
    body = (f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; "
            f"filename=\"{xlsx_path.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(base + "api/pricebook/import",
                                 data=body, method="POST")
    req.add_header("Content-Type",
                   f"multipart/form-data; boundary={boundary}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


async def run(page, debug_dir: Path, **kwargs) -> dict:
    await open_view(page, debug_dir)
    await select_pricebook(page, debug_dir)
    xlsx = await export_excel(page, debug_dir)

    print("📥 Updating the Price Book (same path as manual upload)...")
    result = upload_to_dashboard(xlsx)
    print(f"   ✅ Updated: {result.get('updated', 0)} · "
          f"Added: {result.get('added', 0)} · "
          f"In Price Book but not in export: "
          f"{result.get('unmatched_count', 0)}")
    for u in (result.get("unmatched") or [])[:10]:
        print(f"      · not in export: {u.get('item_name')} "
              f"({u.get('unit_name') or '-'})")
    if result.get("unmatched_count", 0) > 10:
        print(f"      … and {result['unmatched_count'] - 10} more "
              f"(see the Price Book import panel)")
    return {"updated": result.get("updated", 0),
            "added": result.get("added", 0),
            "unmatched": result.get("unmatched_count", 0)}
