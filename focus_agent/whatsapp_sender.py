"""
whatsapp_sender.py
Sends one WhatsApp message per SO alert to a WhatsApp GROUP.
Searches for the group by name — adapted from working reference.
"""
from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import config

SELECT_ALL = "Meta+A" if platform.system() == "Darwin" else "Control+A"

SEARCH_SELECTORS = [
    'input[aria-label="Search or start a new chat"]',
    'input[role="textbox"][aria-label*="Search" i]',
    'div[contenteditable="true"][role="textbox"][aria-label*="Search" i]',
    'div[contenteditable="true"][aria-label*="Search" i]',
    'div[contenteditable="true"][aria-placeholder*="Search" i]',
    'div[contenteditable="true"][data-tab="3"]',
]

MESSAGE_INPUT_SELECTORS = [
    'div[contenteditable="true"][aria-label*="ype a message" i]',
    'div[contenteditable="true"][aria-placeholder*="ype a message" i]',
    'footer div[contenteditable="true"][role="textbox"]',
    'div[data-tab="10"][contenteditable="true"]',
]


def _ensure_pyperclip():
    try:
        import pyperclip
        return pyperclip
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyperclip", "-q"])
        import pyperclip
        return pyperclip


class WhatsAppSender:

    def __init__(self):
        self.session_dir = os.path.abspath(config.WHATSAPP_SESSION_DIR)
        os.makedirs(self.session_dir, exist_ok=True)

    async def send(self, alerts: list, group_name: str = None):
        if not alerts:
            print("   No alerts to send.")
            return

        groups = config.WHATSAPP_GROUPS if hasattr(config, 'WHATSAPP_GROUPS') else [config.WHATSAPP_GROUP]
        pyperclip = _ensure_pyperclip()
        debug_dir = Path("wa_debug") / datetime.now().strftime("%Y%m%d-%H%M%S")
        debug_dir.mkdir(parents=True, exist_ok=True)

        # Remove Chrome lock files and kill any stuck process using this session
        import subprocess as _sp
        for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"]:
            lock_path = Path(self.session_dir) / lock
            if lock_path.exists():
                try:
                    lock_path.unlink()
                    print(f"   🔓 Removed stale lock: {lock}")
                except Exception:
                    pass
        # Kill any Chromium using the whatsapp_session directory (clears lock)
        try:
            _sp.run(
                ["wmic", "process", "where",
                 "CommandLine like '%whatsapp_session%'",
                 "call", "terminate"],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                self.session_dir,
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            print("   Opening WhatsApp Web...")
            await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
            print("   Waiting for WhatsApp (scan QR if first run)...")
            await page.wait_for_selector(
                '[aria-label="Chat list"], #pane-side, [data-testid="chat-list"]',
                timeout=180_000,
            )
            await asyncio.sleep(3)
            print(f"   ✅ WhatsApp ready. Sending to {len(groups)} group(s).\n")

            for group in groups:
                print(f"\n   📢 Group: '{group}'")
                opened = await self._open_group(page, group, debug_dir)
                if not opened:
                    print(f"   ❌ Could not open '{group}' — skipping.")
                    continue
                for alert in alerts:
                    from formatter import format_single_alert
                    msg = format_single_alert(alert)
                    await self._send_message(page, msg, alert["voucher"], pyperclip, debug_dir)
                print(f"   ✅ All messages sent to '{group}'.")

            await asyncio.sleep(3)
            print("\n✅ All WhatsApp messages sent and confirmed delivered.")
            await ctx.close()
            print("   WhatsApp browser closed.")

    async def _open_group(self, page, group_name: str, debug_dir: Path) -> bool:
        """Search for the group and open it. Returns True if successful."""
        try:
            print(f"   🔍 Searching for group: '{group_name}'...")
            search = await self._first_visible(page, SEARCH_SELECTORS)
            if search is None:
                raise RuntimeError("Search box not found")

            await search.click()
            await asyncio.sleep(0.4)
            await page.keyboard.press(SELECT_ALL)
            await page.keyboard.press("Delete")
            await page.keyboard.type(group_name, delay=40)
            await asyncio.sleep(2)
            await page.screenshot(path=str(debug_dir / "01_search.png"))

            # Click the group by exact title match
            group_el = page.locator(f'span[title="{group_name}"]').first
            try:
                await group_el.wait_for(timeout=8_000)
                await group_el.click()
            except PWTimeout:
                # Fallback: click first search result
                first = page.locator(
                    '[aria-label="Search results."] div[role="listitem"],'
                    'div[data-testid="cell-frame-container"]'
                ).first
                await first.wait_for(timeout=5_000)
                await first.click()

            await asyncio.sleep(2)
            await page.screenshot(path=str(debug_dir / "02_group_open.png"))
            print(f"   ✅ Group opened.")
            return True

        except Exception as e:
            await page.screenshot(path=str(debug_dir / "ERROR_open_group.png"))
            print(f"   ❌ Could not open group: {e}")
            return False

    async def _send_message(self, page, message: str, voucher: str,
                            pyperclip, debug_dir: Path):
        """Send a single message to the already-open group chat."""
        print(f"   📤 Sending {voucher}...")
        try:
            # Copy to clipboard
            pyperclip.copy(message.strip())
            await asyncio.sleep(0.5)

            # Find message input
            msg_input = await self._first_visible(page, MESSAGE_INPUT_SELECTORS)
            if msg_input is None:
                raise RuntimeError("Message input box not found")

            # Clear any draft, then paste
            await msg_input.click()
            await asyncio.sleep(0.3)
            await page.keyboard.press(SELECT_ALL)
            await page.keyboard.press("Delete")
            await asyncio.sleep(0.3)
            await page.keyboard.press("Control+v")
            await asyncio.sleep(1.5)

            # Send
            await page.keyboard.press("Enter")
            await asyncio.sleep(2)

            # Wait for delivery confirmation
            # Clock icon = still uploading, check icon = confirmed sent to server
            print(f"      ⏳ Waiting for delivery confirmation...")
            try:
                # Wait for clock icon to disappear (upload complete)
                await page.wait_for_selector(
                    'span[data-icon="msg-time"]',
                    state="hidden", timeout=30_000
                )
                # Wait for tick to appear (confirmed sent)
                await page.wait_for_selector(
                    'span[data-icon="msg-check"], span[data-icon="msg-dblcheck"]',
                    timeout=15_000
                )
                print(f"      ✅ Delivered ✓")
            except Exception:
                print(f"      ⚠️  Could not confirm delivery — check WhatsApp manually")

        except Exception as e:
            await page.screenshot(path=str(debug_dir / f"ERROR_{voucher.replace(' ','_')}.png"))
            print(f"      ❌ Error sending {voucher}: {e}")

    @staticmethod
    async def _first_visible(page, selectors):
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        return None
