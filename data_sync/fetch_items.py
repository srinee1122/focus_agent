"""
fetch_items.py — Item master fetcher for the Data Sync agent.

Navigation: menu search (same proven approach as the price book) —
search "Master Info" and open that view.

CURRENT STAGE: opens the Master Info view and stops with screenshots —
the in-page steps (selecting the Item master, export trigger) follow
once the loaded view's details are provided.
"""
from __future__ import annotations
import asyncio
from pathlib import Path

from focus_common import (click_menu, dismiss_popup, shot,
                          wait_page_ready)

SEARCH_INPUT = "#id_menu_search_input"


async def open_view(page, debug_dir: Path):
    """Search for and open the Master Info view."""
    print("🔎 Opening Master Info via menu search...")
    await wait_page_ready(page, "Focus home", settle=2.0)
    await dismiss_popup(page)

    try:
        await page.wait_for_selector(SEARCH_INPUT, state="attached",
                                     timeout=45_000)
        await page.click(SEARCH_INPUT)
        await asyncio.sleep(0.4)
        await page.keyboard.type("Master Info", delay=60)
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

    await click_menu(page, "Master Info", debug_dir,
                     "2_master_info", timeout_s=25)

    await asyncio.sleep(2.5)
    await wait_page_ready(page, "Master Info view", settle=2.0)
    await dismiss_popup(page)
    await shot(page, debug_dir, "3_master_info_view")
    print("✅ Master Info view opened.")


MASTER_COMBO = "#RITCombobox__1"


async def select_master(page, debug_dir: Path, which: str = "Item"):
    """Master Info serves both masters via a combobox:
    <select id="RITCombobox__1"> Account(1) / Item(2) — selecting fires
    REPORTVIEW.comboSelectionChange."""
    value = {"Account": "1", "Item": "2"}[which]
    print(f"🗂  Selecting master type: {which}")
    try:
        await page.wait_for_selector(MASTER_COMBO, state="attached",
                                     timeout=45_000)
        await page.select_option(MASTER_COMBO, value)
        await asyncio.sleep(2.0)
        await wait_page_ready(page, f"{which} master", settle=2.0)
        await dismiss_popup(page)
        await shot(page, debug_dir, f"4_{which.lower()}_selected")
    except Exception:
        await shot(page, debug_dir, "FAIL_master_combo")
        try:
            (debug_dir / "FAIL_master_combo.html").write_text(
                await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(f"Could not select '{which}' in "
                           f"{MASTER_COMBO} — see sync_debug "
                           f"FAIL_master_combo.png")


async def export_excel(page, debug_dir: Path, tag: str) -> Path:
    """Same report infrastructure as the day book (RIT* controls), so the
    export should be the same: excel icon + 'Export Report Data' Yes."""
    from focus_common import DOWNLOAD_DIR, confirm_export_modal
    from datetime import datetime as _dt
    print("⬇  Exporting to Excel...")
    await dismiss_popup(page)
    await shot(page, debug_dir, "5_before_export")
    try:
        async with page.expect_download(timeout=180_000) as dl_info:
            await page.locator("i.icon-import-from-excel").first.click()
            await confirm_export_modal(page, timeout_s=30, broad=True)
        download_obj = await dl_info.value
    except Exception:
        await shot(page, debug_dir, "FAIL_export")
        try:
            (debug_dir / "FAIL_export.html").write_text(
                await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("Excel export did not start on Master Info — "
                           "see sync_debug FAIL_export.png/.html (the "
                           "export trigger may differ on this page)")
    out = DOWNLOAD_DIR / f"{tag}_{_dt.now().strftime('%Y%m%d')}.xlsx"
    await download_obj.save_as(str(out))
    print(f"   Downloaded: {out.name} ({out.stat().st_size // 1024} KB)")
    await asyncio.sleep(1.0)
    await dismiss_popup(page)
    await shot(page, debug_dir, "6_after_export")
    return out


def upload_to_dashboard(xlsx_path: Path, endpoint: str) -> dict:
    """Feed the export through the dashboard's OWN masters upload
    endpoint — identical to a manual upload (proven parser, upsert by
    name)."""
    import json
    import os
    import urllib.request

    base = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8000/")
    if not base.endswith("/"):
        base += "/"
    token = os.environ.get("AGENT_AUTH_TOKEN", "")

    data = xlsx_path.read_bytes()
    boundary = "----syncmaster"
    body = (f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; "
            f"filename=\"{xlsx_path.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(base + endpoint.lstrip("/"),
                                 data=body, method="POST")
    req.add_header("Content-Type",
                   f"multipart/form-data; boundary={boundary}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


async def run(page, debug_dir: Path, **kwargs) -> dict:
    await open_view(page, debug_dir)
    await select_master(page, debug_dir, "Item")
    xlsx = await export_excel(page, debug_dir, "items")

    print("📥 Updating the Items master (same path as manual upload)...")
    stats = upload_to_dashboard(xlsx, "api/sales/masters/items/upload")
    print(f"   ✅ Items: {stats.get('items', 0)} · "
          f"Brands: {stats.get('brands', 0)}")
    return {"items": stats.get("items", 0),
            "brands": stats.get("brands", 0)}
