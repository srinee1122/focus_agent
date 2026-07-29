"""
whatsapp_sender.py
Sends WhatsApp alerts — text, image, or both — to configured groups.
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

    async def send(self, alerts: list, group_name: str = None,
                   send_text: bool = None, send_image: bool = None):
        if not alerts:
            print("   No alerts to send.")
            return

        # Resolve toggles (from config unless overridden)
        use_text  = send_text  if send_text  is not None else getattr(config, "SEND_TEXT",  True)
        use_image = send_image if send_image is not None else getattr(config, "SEND_IMAGE", True)

        # Enforce: at least one must be on
        if not use_text and not use_image:
            print("   ⚠️  Both text and image are off — defaulting to text.")
            use_text = True

        groups    = config.WHATSAPP_GROUPS if hasattr(config, "WHATSAPP_GROUPS") else [config.WHATSAPP_GROUP]
        print(f"   📋 WhatsApp groups from config: {groups}")
        print(f"   🔀 Send text: {use_text} | Send image: {use_image}")
        pyperclip = _ensure_pyperclip()
        debug_dir = Path("wa_debug") / datetime.now().strftime("%Y%m%d-%H%M%S")
        debug_dir.mkdir(parents=True, exist_ok=True)

        # Pre-generate images if needed (headless, before opening WhatsApp)
        images = {}
        if use_image:
            from image_generator import generate_alert_image
            print("   📸 Generating alert images...")
            for alert in alerts:
                path = await generate_alert_image(alert)
                images[alert["voucher"]] = path

        # Remove Chrome lock files before opening WhatsApp
        import subprocess as _sp
        for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"]:
            lock_path = Path(self.session_dir) / lock
            if lock_path.exists():
                try:
                    lock_path.unlink()
                    print(f"   🔓 Removed stale lock: {lock}")
                except Exception:
                    pass
        try:
            _sp.run(["wmic", "process", "where",
                     "CommandLine like '%whatsapp_session%'",
                     "call", "terminate"],
                    capture_output=True, timeout=5)
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
            print("   Waiting for WhatsApp to load (scan QR if first run)...")
            await page.wait_for_selector(
                '[aria-label="Chat list"], #pane-side, [data-testid="chat-list"]',
                timeout=180_000,
            )
            await asyncio.sleep(3)

            for group in groups:
                print(f"\n   📢 Sending to: '{group}'")
                opened = await self._open_group(page, group, debug_dir)
                if not opened:
                    print(f"   ❌ Could not open group '{group}' — skipping.")
                    continue

                for alert in alerts:
                    voucher = alert["voucher"]

                    # Send image first if enabled
                    if use_image and voucher in images:
                        await self._send_image(page, images[voucher], voucher, debug_dir)

                    # Send text (summary if image also sent, full detail if image only off)
                    if use_text:
                        if use_image:
                            from formatter import format_text_summary
                            msg = format_text_summary(alert)
                        else:
                            from formatter import format_single_alert
                            msg = format_single_alert(alert)
                        await self._send_message(page, msg, voucher, pyperclip, debug_dir)

                print(f"   ✅ Done sending to '{group}'.")

            await asyncio.sleep(3)
            print("\n✅ All WhatsApp messages sent and confirmed.")
            await ctx.close()
            print("   WhatsApp browser closed.")

        # Clean up temp images
        if use_image:
            for path in images.values():
                try:
                    os.remove(path)
                except Exception:
                    pass

    # ── Open group ─────────────────────────────────────────────────────────
    async def _open_group(self, page, group_name: str, debug_dir: Path) -> bool:
        try:
            print(f"   🔍 Searching: '{group_name}'...")
            search = await self._first_visible(page, SEARCH_SELECTORS)
            if search is None:
                raise RuntimeError("Search box not found")
            await search.click()
            await asyncio.sleep(0.4)
            await page.keyboard.press(SELECT_ALL)
            await page.keyboard.press("Delete")
            await page.keyboard.type(group_name, delay=40)
            await asyncio.sleep(2)

            group_el = page.locator(f'span[title="{group_name}"]').first
            try:
                await group_el.wait_for(timeout=8_000)
                await group_el.click()
            except PWTimeout:
                first = page.locator(
                    '[aria-label="Search results."] div[role="listitem"],'
                    'div[data-testid="cell-frame-container"]'
                ).first
                await first.wait_for(timeout=5_000)
                await first.click()

            await asyncio.sleep(2)
            print(f"   ✅ Group opened.")
            return True
        except Exception as e:
            await page.screenshot(path=str(debug_dir / "ERROR_open_group.png"))
            print(f"   ❌ Could not open group: {e}")
            return False

    # ── Send image ──────────────────────────────────────────────────────────
    async def _send_image(self, page, image_path: str, voucher: str, debug_dir: Path):
        print(f"      📤 Sending image for {voucher}...")
        try:
            from PIL import Image as PILImage
            from pathlib import Path as _Path
            import subprocess as _sp

            # Ensure genuinely JPEG (not just renamed) using PIL
            src = _Path(image_path)
            jpg_path = src.with_name(f"{src.stem}_photo.jpg")
            with PILImage.open(src) as img:
                img.convert("RGB").save(str(jpg_path), format="JPEG", quality=95)
            image_path = str(jpg_path.resolve())
            print(f"      🖼️  Converted to JPEG: {image_path}")

            # Click the attachment (+) button
            attach_btn = await self._first_visible(page, [
                'span[data-icon="plus-rounded"]',
                'span[data-icon="attach-menu-plus"]',
                'span[data-icon="clip"]',
                'div[title="Attach"]',
            ])
            if attach_btn is None:
                raise RuntimeError("Attachment button not found")
            await attach_btn.click()
            await asyncio.sleep(0.8)

            # Use file chooser — WhatsApp picks the correct input after "Photos & videos"
            print(f"      🔍 Waiting for file chooser...")
            async with page.expect_file_chooser(timeout=8_000) as fc_info:
                # Click "Photos & videos" menu item
                photo_btn = await self._first_visible(page, [
                    'li[data-testid="mi-attach-photo-video"]',
                    'li[data-testid="mi-attach-image"]',
                ])
                if photo_btn:
                    await photo_btn.click()
                else:
                    # Fallback: click by visible text
                    await page.get_by_text("Photos", exact=False).first.click()

            file_chooser = await fc_info.value
            await file_chooser.set_files(image_path)
            print(f"      ✅ File set via chooser")
            await asyncio.sleep(2.5)

            # Send
            await page.keyboard.press("Enter")
            await asyncio.sleep(2)

            # Clean up converted file
            try:
                jpg_path.unlink()
            except Exception:
                pass

            # Wait for delivery confirmation
            print(f"      ⏳ Waiting for image delivery...")
            try:
                await page.wait_for_selector(
                    'span[data-icon="msg-time"]', state="hidden", timeout=30_000)
                await page.wait_for_selector(
                    'span[data-icon="msg-check"], span[data-icon="msg-dblcheck"]',
                    timeout=15_000)
                print(f"      ✅ Image delivered ✓")
            except Exception:
                print(f"      ⚠️  Could not confirm image delivery — check WhatsApp manually")

        except Exception as e:
            await page.screenshot(path=str(debug_dir / f"ERROR_img_{voucher}.png"))
            print(f"      ❌ Image send error ({voucher}): {e}")

    # ── Send text ───────────────────────────────────────────────────────────
    async def _send_message(self, page, message: str, voucher: str,
                            pyperclip, debug_dir: Path):
        print(f"      📤 Sending text for {voucher}...")
        try:
            pyperclip.copy(message.strip())
            await asyncio.sleep(0.5)

            msg_input = await self._first_visible(page, MESSAGE_INPUT_SELECTORS)
            if msg_input is None:
                raise RuntimeError("Message input not found")
            await msg_input.click()
            await asyncio.sleep(0.3)
            await page.keyboard.press(SELECT_ALL)
            await page.keyboard.press("Delete")
            await asyncio.sleep(0.3)
            await page.keyboard.press("Control+v")
            await asyncio.sleep(1.5)
            await page.keyboard.press("Enter")
            await asyncio.sleep(2)

            # Delivery confirmation
            print(f"      ⏳ Waiting for text delivery...")
            try:
                await page.wait_for_selector(
                    'span[data-icon="msg-time"]', state="hidden", timeout=30_000)
                await page.wait_for_selector(
                    'span[data-icon="msg-check"], span[data-icon="msg-dblcheck"]',
                    timeout=15_000)
                print(f"      ✅ Text delivered ✓")
            except Exception:
                print(f"      ⚠️  Could not confirm text delivery")

        except Exception as e:
            await page.screenshot(path=str(debug_dir / f"ERROR_txt_{voucher}.png"))
            print(f"      ❌ Text send error ({voucher}): {e}")

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
