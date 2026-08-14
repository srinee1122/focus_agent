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

from focus_common import (click_menu, dismiss_popup, shot,
                          wait_page_ready)

SEARCH_INPUT = "#id_menu_search_input"


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
    from focus_common import DOWNLOAD_DIR, confirm_export_modal
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
