"""
focus_common.py — LOGIN AND SESSION ONLY, shared by all fetchers.

Scope rule (by design): this module handles ONLY credentials
(Firestore via focus_agent/credentials.py), the FROZEN login copy,
browser/session management, and neutral diagnostics (screenshots,
page-ready waits). ALL page-specific behaviour — menu clicking, popup
handling, export confirmations, date inputs — lives inside each
fetch_*.py, even when similar, so fixing one report can never affect
another.

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
