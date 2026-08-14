"""
focus_common.py — shared Focus ERP browsing for ALL Data Sync fetchers.

ONE place for: credentials (Firestore, via focus_agent/credentials.py),
the FROZEN login copy, session management, menu clicking (direct JS,
hidden-element capable), popup close, export-confirmation Yes, and date
input setting. Individual fetchers (fetch_daybook.py, fetch_pricebook.py,
...) receive a logged-in page and do only their own navigation + import.

The LOGIN block is a VERBATIM copy of focus_agent/focus_scraper.py's
frozen sequence — never modify either.
"""
from __future__ import annotations
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent          # data_sync/
_ROOT = _THIS_DIR.parent


def _resolve_focus_agent_dir() -> Path:
    env = os.environ.get("AGENT_DIR")
    candidates = ([Path(env)] if env else []) + [
        _ROOT / "focus_agent",
        _ROOT,
        _ROOT / "focus_agent_complete" / "focus_agent",
    ]
    for p in candidates:
        if (p / "config.py").exists():
            return p
    return candidates[0]


FOCUS_AGENT_DIR = _resolve_focus_agent_dir()
DASHBOARD_DB = Path(os.environ.get("DASHBOARD_DB")
                    or (_ROOT / "erp_dashboard" / "dashboard.db"))
SALES_AGENT_DIR = _ROOT / "sales_agent"

sys.path.insert(0, str(FOCUS_AGENT_DIR))   # config.py, credentials.py
sys.path.insert(0, str(SALES_AGENT_DIR))   # sales_module.py

import config  # noqa: E402

FOCUS_BASE_URL = getattr(config, "FOCUS_URL",
                         "https://ymt-9.focus9erp.com/focusx")
DOWNLOAD_DIR = _THIS_DIR / "downloads"
DEBUG_ROOT = _THIS_DIR / "sync_debug"


def read_credentials() -> tuple[str, str]:
    """Same source as the low-price agent: Firestore via
    focus_agent/credentials.py (AGENT_AUTH_TOKEN authorises)."""
    from credentials import get_credentials
    return get_credentials()


def new_debug_dir(tag: str) -> Path:
    d = DEBUG_ROOT / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


@asynccontextmanager
async def focus_session(tag: str = "sync"):
    """ONE browser + ONE login, shared by all fetchers in a run.
    Yields (page, debug_dir)."""
    from playwright.async_api import async_playwright
    username, password = read_credentials()
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    debug_dir = new_debug_dir(tag)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=config.HEADLESS)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1920, "height": 1080})
        context.set_default_timeout(60_000)
        page = await context.new_page()
        try:
            await focus_login(page, username, password)
            yield page, debug_dir
        except Exception:
            await shot(page, debug_dir, "FAIL_final")
            raise
        finally:
            await asyncio.sleep(2)
            await browser.close()


async def wait_page_ready(page, label: str = "", timeout: int = 30_000,
                          settle: float = 0.5):
    """Same readiness pattern as the scraper: let AJAX finish + settle."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    if settle:
        await asyncio.sleep(settle)
    if label:
        print(f"      Page ready: {label}")



async def focus_login(page, username: str, password: str):
    """VERBATIM copy of the frozen login block from focus_scraper.py.
    Do not change without changing the original (which is frozen)."""
    print("🌐 Opening Focus ERP...")
    await page.goto(FOCUS_BASE_URL, wait_until="networkidle", timeout=90_000)
    await asyncio.sleep(3)
    print("   Page loaded.")

    print("🔐 Logging in...")
    await page.wait_for_selector("#txtUsername", state="visible",
                                 timeout=30_000)
    await asyncio.sleep(2)
    await page.bring_to_front()

    # Initialise bRemFlag so getCompanySuccess callback doesn't crash
    await page.evaluate("window.bRemFlag = false;")
    await asyncio.sleep(0.5)

    # Type username then Tab to trigger company list AJAX
    await page.click("#txtUsername")
    await asyncio.sleep(0.3)
    await page.keyboard.type(username, delay=50)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Tab")
    await asyncio.sleep(2)

    # Type password
    await page.click("#txtPassword")
    await asyncio.sleep(0.3)
    await page.keyboard.type(password, delay=50)
    await asyncio.sleep(0.5)

    # Click Sign In
    await page.wait_for_selector("#btnSignin", state="visible",
                                 timeout=10_000)
    await page.click("#btnSignin")
    await page.wait_for_load_state("networkidle", timeout=30_000)
    await asyncio.sleep(2)
    print("   Logged in successfully.")



async def shot(page, debug_dir: Path, name: str):
    try:
        await page.screenshot(path=str(debug_dir / f"{name}.png"),
                              timeout=10_000)
    except Exception:
        pass



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



async def confirm_export_modal(page, timeout_s: int = 45,
                               broad: bool = False) -> str:
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
    click_js = """(broad) => {
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
        // Broader matching: OPT-IN ONLY (broad flag) so proven flows
        // (day book, price book) keep their exact original behaviour
        if (!el && broad) {
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
                hit = await frame.evaluate(click_js, broad)
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


