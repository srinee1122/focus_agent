"""
fetch_accounts.py — Customers (Account) master fetcher.

OWN COPY of the Master Info machinery (menu search, combobox, select
all, monitored async export) — deliberately duplicated from
fetch_items.py so a change or failure in either fetcher can never
affect the other.

Navigation: menu search (same proven approach as the price book) —
search "Master Info" and open that view.

CURRENT STAGE: opens the Master Info view and stops with screenshots —
the in-page steps (selecting the Item master, export trigger) follow
once the loaded view's details are provided.
"""
from __future__ import annotations
import asyncio
from pathlib import Path

from focus_common import shot, wait_page_ready

SEARCH_INPUT = "#id_menu_search_input"


# ── Page helpers (OWNED by this fetcher — page-specific by design;
#    similar code in other fetchers is deliberately duplicated) ──
# Confirm = this page's OWN: ReadyToExport + the popuptextbox
# question ('Do you want to export report to the Excel?') + broad
# labelled-button fallback with diagnostics.
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

async def confirm_pass(page) -> str:
    """One scan across frames: click whichever export confirmation is
    currently up (ReadyToExport Yes, the popuptextbox question's Yes, or
    a labelled confirm). Returns what was clicked, '' if nothing."""
    return await confirm_export_modal(page, timeout_s=1)


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
        // Text-anchored: the popuptextbox confirmation
        // ("Do you want to export report to the Excel?") — find the
        // question, climb to its popup container, click its Yes/OK.
        // Anchored to the exact text, so it cannot misfire elsewhere.
        if (!el) {
            const lbl = [...document.querySelectorAll(
                '.popuptextbox label, #lblConfirmMessage, label')]
                .find(l => /export report to the excel/i
                           .test(l.textContent || '') && visible(l));
            if (lbl) {
                let root = lbl.closest(
                    '[class*="opup"], .modal, .modal-content')
                    || lbl.parentElement;
                for (let i = 0; i < 6 && root; i++) {
                    const btns = [...root.querySelectorAll(
                        'input[type=button], button')].filter(visible);
                    const yes = btns.find(b =>
                        /^(yes|ok)$/i.test(
                            (b.value || b.textContent || '').trim()));
                    if (yes) { el = yes; break; }
                    root = root.parentElement;
                }
            }
        }
        // Broader matching: OPT-IN ONLY (broad flag) so proven flows
        // (day book, price book) keep their exact original behaviour
        if (!el) {
            const okWords = /^(yes|ok|export|proceed|confirm|continue)$/;
            el = cands.find(b => {
                if (!visible(b)) return false;
                const label = (b.value || b.textContent || '')
                              .trim().toLowerCase();
                if (!okWords.test(label)) return false;
                const cls = b.className || '';
                if (/FButton|FPopup/i.test(cls)) return true;
                const wrap = b.closest(
                    '.modal, .modal-content, [class*="opup"]');
                return !!wrap;
            });
        }
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
    if timeout_s > 2:      # quiet on single-pass polling
        print("   ⚠ Yes button not found in any frame within "
              f"{timeout_s}s — maybe direct download")
    return ""

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
    await check_server_error(page)
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
        # A stale popup (e.g. from a previous export attempt) can sit
        # over the page — clear before looking for the combobox
        await check_server_error(page)
        await dismiss_popup(page)
        try:
            await page.wait_for_selector(MASTER_COMBO, state="attached",
                                         timeout=60_000)
        except Exception:
            # One recovery pass: popups + settle, then a shorter recheck
            await dismiss_popup(page)
            await wait_page_ready(page, settle=2.0)
            await page.wait_for_selector(MASTER_COMBO, state="attached",
                                         timeout=30_000)
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


async def check_server_error(page):
    """Focus can serve an ASP.NET error page (e.g. the anti-forgery
    machine-key error after a server restart). Detect it and fail with
    the truth instead of a misleading selector timeout."""
    try:
        txt = await page.evaluate(
            "() => (document.body ? document.body.innerText : '')"
            ".slice(0, 600)")
    except Exception:
        return
    if "Server Error in '/FocusX'" in txt or \
            "HttpAntiForgeryException" in txt or \
            "An unhandled exception occurred" in txt:
        raise RuntimeError(
            "Focus served a SERVER ERROR page (ASP.NET exception — "
            "likely the Focus host restarted or has a key mismatch). "
            "This is on the Focus server side, not the sync. Wait a few "
            "minutes and try again; if it persists, check Focus in a "
            "normal browser.")


SELECT_ALL = "#selectAllMasters_"


async def select_all_masters(page, debug_dir: Path):
    """Tick the 'Select All' checkbox — without it, the export has no
    rows and silently produces nothing. It's a TOGGLE
    (onclick REPORTVIEW.checkAllMasters), so check the current state
    first and only click when unchecked."""
    print("☑️  Selecting all masters...")
    try:
        await page.wait_for_selector(SELECT_ALL, state="attached",
                                     timeout=30_000)
        already = await page.evaluate(
            "() => !!document.querySelector('" + SELECT_ALL + "')?.checked")
        if already:
            print("   Already selected — leaving as is.")
        else:
            clicked = False
            # 1) trusted click on the input itself
            try:
                await page.locator(SELECT_ALL).click(timeout=4_000)
                clicked = True
            except Exception:
                pass
            # 2) the visual span.checkmark next to it (custom checkbox UIs
            #    often only respond there)
            if not clicked:
                try:
                    await page.locator(
                        SELECT_ALL + " + span.checkmark").click(
                            timeout=4_000)
                    clicked = True
                except Exception:
                    pass
            # 3) JS click as last resort
            if not clicked:
                await page.evaluate(
                    "() => document.querySelector('" + SELECT_ALL
                    + "')?.click()")
            await asyncio.sleep(1.5)
            state = await page.evaluate(
                "() => !!document.querySelector('" + SELECT_ALL
                + "')?.checked")
            if not state:
                await shot(page, debug_dir, "FAIL_select_all")
                raise RuntimeError("Could not tick Select All "
                                   "(#selectAllMasters_) — see "
                                   "sync_debug FAIL_select_all.png")
            print("   All masters selected.")
        await wait_page_ready(page, settle=1.0)
        await dismiss_popup(page)
        await shot(page, debug_dir, "4b_select_all")
    except RuntimeError:
        raise
    except Exception:
        await shot(page, debug_dir, "FAIL_select_all")
        raise RuntimeError("Select All checkbox not found — see "
                           "sync_debug FAIL_select_all.png")


async def export_excel(page, debug_dir: Path, tag: str) -> Path:
    """Same report infrastructure as the day book (RIT* controls), so the
    export should be the same: excel icon + 'Export Report Data' Yes."""
    from focus_common import DOWNLOAD_DIR
    from datetime import datetime as _dt
    print("⬇  Exporting to Excel...")
    await dismiss_popup(page)
    await shot(page, debug_dir, "5_before_export")

    # This page exports ASYNCHRONOUSLY: the server builds the file and
    # announces it over SignalR (SendFileToClient) → the client then
    # downloads it. So: wrap SendFileToClient to capture the filename,
    # answer every confirmation dialog, and if the push arrives but no
    # browser download follows, trigger the download ourselves.
    downloads = []
    page.on("download", lambda d: downloads.append(d))

    def _on_new_page(p):
        try:
            p.on("download", lambda d: downloads.append(d))
        except Exception:
            pass

    page.context.on("page", _on_new_page)

    hub_state = await page.evaluate("""() => {
        try {
            window.__exportedFiles = [];
            const cli = $.connection.signalRHub.client;
            const orig = cli.SendFileToClient;
            cli.SendFileToClient = function (sid, fn) {
                try { window.__exportedFiles.push(fn); } catch (e) {}
                return orig.apply(this, arguments);
            };
            return ($.connection.hub && $.connection.hub.state !== undefined)
                   ? String($.connection.hub.state) : 'no-hub';
        } catch (e) { return 'err: ' + e.message; }
    }""")
    print(f"   SignalR hub state: {hub_state} "
          f"(1 = connected; export needs this push channel)")

    seen = set()

    async def _keep_confirming():
        while True:
            try:
                hit = await confirm_pass(page)
                if hit and hit not in seen:
                    seen.add(hit)
            except Exception:
                pass
            await asyncio.sleep(0.8)

    confirm_task = asyncio.create_task(_keep_confirming())
    download_obj = None
    manually_triggered = False
    try:
        await page.locator("i.icon-import-from-excel").first.click()

        # MONITOR across ALL frames, re-arming instrumentation if the
        # SPA replaces the JS context (observed: wrappers installed
        # pre-click vanish after the export click). While the server
        # builds the file the page is legitimately idle — so there is NO
        # idle give-up: only a download, an announcement, or the hard
        # cap ends the wait.
        arm_js = """() => {
            try {
                if (window.__exportArmed) return 'armed';
                window.__exportedFiles = window.__exportedFiles || [];
                if (typeof $ === 'undefined' || !$.connection ||
                    !$.connection.signalRHub) return 'no-hub';
                const cli = $.connection.signalRHub.client;
                const orig = cli.SendFileToClient;
                cli.SendFileToClient = function (sid, fn) {
                    try { window.__exportedFiles.push(fn); } catch (e) {}
                    return orig ? orig.apply(this, arguments) : undefined;
                };
                window.__exportArmed = true;
                return 'armed-now';
            } catch (e) { return 'err:' + e.message; }
        }"""
        peek_js = """() => ({
            announced: (window.__exportedFiles || []).slice(),
            hub: (typeof $ !== 'undefined' && $.connection &&
                  $.connection.hub)
                 ? String($.connection.hub.state) : 'none',
            hasRenderer: typeof REPORTRENDERNEW !== 'undefined' &&
                         !!REPORTRENDERNEW.GetExportedFile_Success
        })"""
        tick = 0
        HARD_CAP = 900          # safety net only
        rearm_note = set()
        while tick < HARD_CAP:
            await asyncio.sleep(1)
            tick += 1
            if downloads:
                download_obj = downloads[0]
                break
            if tick == 10:
                await shot(page, debug_dir, "8_during_wait")
            if tick % 5 == 0:
                announced = []
                for fr in page.frames:
                    try:
                        armed = await fr.evaluate(arm_js)
                        if armed == "armed-now" and tick > 10 \
                                and fr.url not in rearm_note:
                            rearm_note.add(fr.url)
                            print(f"   🔁 context replaced — re-armed "
                                  f"capture in {fr.url[:50]}")
                        peek = await fr.evaluate(peek_js)
                        announced.extend(peek.get("announced") or [])
                    except Exception:
                        continue
                if announced and not manually_triggered and tick > 8:
                    print(f"   📨 Server announced: {announced[0][-60:]}"
                          f" — triggering the download directly")
                    for fr in page.frames:
                        try:
                            ok = await fr.evaluate(
                                """(fn) => {
                                    if (typeof REPORTRENDERNEW !==
                                        'undefined' &&
                                        REPORTRENDERNEW
                                        .GetExportedFile_Success) {
                                        REPORTRENDERNEW
                                        .GetExportedFile_Success(fn);
                                        return true;
                                    }
                                    return false;
                                }""", announced[0])
                            if ok:
                                break
                        except Exception:
                            continue
                    manually_triggered = True
            if tick % 30 == 0:
                states = []
                for fr in page.frames[:4]:
                    try:
                        p = await fr.evaluate(peek_js)
                        states.append(f"hub={p['hub']}"
                                      f"{'+r' if p['hasRenderer'] else ''}")
                    except Exception:
                        states.append("?")
                print(f"   ⏳ {tick}s elapsed — waiting for the server "
                      f"build (frames: {', '.join(states)})")
    finally:
        confirm_task.cancel()
        try:
            page.context.remove_listener("page", _on_new_page)
        except Exception:
            pass

    if download_obj is None:
        await shot(page, debug_dir, "FAIL_export")
        try:
            (debug_dir / "FAIL_export.html").write_text(
                await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("Excel export did not start on Master Info "
                           "even with continuous dialog confirmation — "
                           "see sync_debug FAIL_export.png/.html")
    print("   ⬇ Download started — waiting for it to finish...")
    await download_obj.path()          # blocks until fully downloaded
    out = DOWNLOAD_DIR / f"{tag}_{_dt.now().strftime('%Y%m%d')}.xlsx"
    await download_obj.save_as(str(out))
    print(f"   ✅ Download complete: {out.name} "
          f"({out.stat().st_size // 1024} KB)")
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


async def _open_and_select(page, debug_dir: Path, which: str):
    """Open Master Info and select the master type, with one full retry
    (view flakiness observed during Focus server instability)."""
    for attempt in (1, 2):
        try:
            await open_view(page, debug_dir)
            await select_master(page, debug_dir, which)
            return
        except Exception:
            if attempt == 2:
                raise
            print("   ↻ View/selection failed — retrying once from the "
                  "menu search...")
            try:
                from focus_common import FOCUS_BASE_URL
                await page.goto(FOCUS_BASE_URL,
                                wait_until="domcontentloaded")
                await asyncio.sleep(3)
            except Exception:
                pass


async def run(page, debug_dir: Path, **kwargs) -> dict:
    """Customers master: SAME page and machinery, 'Account' selected —
    the select-all checkbox and async export behave identically
    (data-text 'Unselect All Account' confirmed it governs both)."""
    await _open_and_select(page, debug_dir, "Account")
    await select_all_masters(page, debug_dir)
    xlsx = await export_excel(page, debug_dir, "accounts")

    print("📥 Updating the Customers master (same path as manual "
          "upload)...")
    stats = upload_to_dashboard(xlsx,
                                "api/sales/masters/customers/upload")
    print(f"   ✅ Customers: {stats.get('customers', 0)} · "
          f"Segments: {stats.get('segments', 0)} · "
          f"Areas: {stats.get('areas', 0)} "
          f"(non-customer accounts skipped: "
          f"{stats.get('non_customer_accounts', 0)})")
    return {"customers": stats.get("customers", 0),
            "segments": stats.get("segments", 0),
            "areas": stats.get("areas", 0)}
