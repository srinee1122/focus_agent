"""
sales_send.py — Renders a sales report (JSON from the dashboard) to a JPEG
and sends it to the configured WhatsApp groups.

Called by the dashboard as a subprocess with env:
  SALES_REPORT_JSON — path to the report JSON file
  SALES_WA_GROUPS   — JSON list of WhatsApp group names
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# Shared WhatsApp sender lives in focus_agent (interim common location
# until the common/ modules refactor)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "focus_agent"))
from whatsapp_sender import WhatsAppSender

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _tolerant_groups(raw: str) -> list:
    raw = (raw or "").strip()
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else [str(v)]
    except Exception:
        inner = raw.strip("[]").strip()
        if not inner:
            return []
        fixed = [s.strip().strip("'\"") for s in inner.split(",")]
        fixed = [s for s in fixed if s]
        print(f"   ⚠️ Fixed malformed JSON: {raw} → {fixed}")
        return fixed


def _build_html(rep: dict) -> str:
    smen = rep["salesmen"]
    has_targets = any(i["target"] for i in rep["items"])

    def cell(c):
        if c["ctn"]:
            s = f"<b>{c['ctn']:g}</b><span class='sub'>ctn</span>" \
                f"<div class='pcs'>{c['pieces']:g} pcs</div>"
        elif c["pieces"]:
            s = f"<b>{c['pieces']:g}</b><span class='sub'>pcs</span>"
        else:
            s = "<span class='dash'>–</span>"
        if c.get("foc_pieces"):
            s += f"<div class='foc'>FOC {c['foc_pieces']:g}</div>"
        return s

    head = "".join(f"<th>{s}</th>" for s in smen)
    tgt_head = "<th>Target</th><th>Ach%</th>" if has_targets else ""
    rows = ""
    for it in rep["items"]:
        tds = "".join(f"<td>{cell(it['per_salesman'][s])}</td>" for s in smen)
        if has_targets:
            if it["target"]:
                t = it["target"]
                pct = t["achieved_pct"]
                cls = "good" if (pct or 0) >= 100 else ("bad" if (pct or 0) < 60 else "")
                tgt = (f"<td>{t['qty']:g} {t['unit'].lower()}</td>"
                       f"<td class='{cls}'><b>{pct if pct is not None else '–'}%</b></td>")
            else:
                tgt = "<td class='dash'>–</td><td class='dash'>–</td>"
        else:
            tgt = ""
        rows += (f"<tr><td class='item'>{it['item']}</td>{tds}"
                 f"<td class='tot'>{cell(it['total'])}</td>{tgt}</tr>")

    ptargets = ""
    plines = []
    for it in rep["items"]:
        for t in it["salesman_targets"]:
            pct = t["achieved_pct"]
            cls = "good" if (pct or 0) >= 100 else ("bad" if (pct or 0) < 60 else "")
            plines.append(
                f"<tr><td class='item'>{it['item']}</td><td>{t['salesman']}</td>"
                f"<td>{t['actual']:g} / {t['qty']:g} {t['unit'].lower()}</td>"
                f"<td class='{cls}'><b>{pct if pct is not None else '–'}%</b></td></tr>")
    if plines:
        ptargets = ("<h3>Personal targets</h3><table class='mini'>"
                    "<tr><th>Item</th><th>Salesman</th><th>Actual / Target</th>"
                    "<th>Ach%</th></tr>" + "".join(plines) + "</table>")

    zeros = ""
    zlines = [f"<div><b>{it['item']}</b>: {', '.join(it['zero_sellers'])}</div>"
              for it in rep["items"] if it["zero_sellers"]]
    if zlines:
        zeros = "<h3>Zero sellers</h3>" + "".join(zlines)

    rank = "".join(
        f"<div class='rk'><span class='pos'>{i}</span> <b>{r['salesman']}</b>"
        f" — {r['ctn']:g} ctn ({r['pieces']:g} pcs)</div>"
        for i, r in enumerate(rep["ranking"], 1))

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #fff;
         color: #1a2340; margin: 0; padding: 22px; width: fit-content; }}
  .hdr {{ background: linear-gradient(90deg,#0e7d6b,#155e75); color:#fff;
         padding: 14px 20px; border-radius: 10px 10px 0 0; }}
  .hdr h1 {{ margin:0; font-size: 19px; }}
  .hdr .sub2 {{ font-size: 12px; opacity:.85; margin-top:3px; }}
  table {{ border-collapse: collapse; margin-top: 10px; }}
  th {{ background:#1a2340; color:#fff; padding:7px 10px; font-size:11.5px;
       text-align:center; }}
  td {{ border:1px solid #d5dde5; padding:6px 10px; font-size:12px;
       text-align:center; min-width:64px; }}
  td.item {{ text-align:left; font-weight:600; max-width:260px; }}
  td.tot {{ background:#e9f5f2; font-weight:700; }}
  tr:nth-child(even) td {{ background:#f7f9fc; }}
  tr:nth-child(even) td.tot {{ background:#e9f5f2; }}
  .sub {{ font-size:9px; color:#5a6379; margin-left:2px; }}
  .pcs {{ font-size:9.5px; color:#5a6379; }}
  .foc {{ font-size:9.5px; color:#b58a00; font-weight:600; }}
  .dash {{ color:#aab; }}
  .good {{ color:#1e8449; }} .bad {{ color:#c0392b; }}
  h3 {{ margin:16px 0 6px; font-size:13px; color:#155e75; }}
  .mini td, .mini th {{ font-size:11.5px; }}
  .rk {{ font-size:12px; padding:2px 0; }}
  .pos {{ display:inline-block; width:20px; height:20px; line-height:20px;
         border-radius:50%; background:#0e7d6b; color:#fff; text-align:center;
         font-size:11px; font-weight:700; }}
  .ftr {{ margin-top:14px; font-size:10.5px; color:#5a6379;
         border-top:1px solid #d5dde5; padding-top:8px; }}
</style></head><body>
  <div class="hdr"><h1>📊 Sales Report — {rep['group']}</h1>
    <div class="sub2">{rep['date_from']} to {rep['date_to']} · generated {rep['generated_at']}</div>
  </div>
  <table><tr><th style="text-align:left">Item</th>{head}<th>TOTAL</th>{tgt_head}</tr>
  {rows}</table>
  {ptargets}{zeros}
  <h3>Salesman ranking</h3>{rank}
  <div class="ftr">ShopAide Agent 🤖 · Sri Ambikas Pte Ltd</div>
</body></html>"""


async def _render_image(rep: dict) -> str:
    out_dir = Path("downloads"); out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    safe = "".join(c if c.isalnum() else "_" for c in rep["group"])[:24]
    out = out_dir / f"sales_report_{safe}_{stamp}.jpg"
    html = _build_html(rep)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.set_content(html)
        await asyncio.sleep(0.6)
        el = await page.query_selector("body")
        await el.screenshot(path=str(out), type="jpeg", quality=88)
        await browser.close()

    # Keep the file light so WhatsApp's preview loads within the sender's
    # fixed timing (big/tall images previously previewed too slowly)
    try:
        from PIL import Image as _PIL
        with _PIL.open(out) as img:
            w, h = img.size
            if w > 1100 or h > 3200:
                scale = min(1100 / w, 3200 / h, 1.0)
                img = img.resize((int(w * scale), int(h * scale)),
                                 _PIL.LANCZOS)
            img.convert("RGB").save(str(out), format="JPEG",
                                    quality=82, optimize=True)
    except Exception as e:
        print(f"   ⚠️ Image slimming skipped: {e}")
    import os as _os
    print(f"   📸 Report image: {out} ({_os.path.getsize(out)//1024} KB)")
    return str(out)


async def _send_to_groups(image_path: str, groups: list) -> int:
    """Deliver the report image using the UNMODIFIED WhatsAppSender's own
    methods, replicating the exact browser flow of its proven send():
    same launch args, same chat-list wait, same lock cleanup."""
    sender = WhatsAppSender()
    debug_dir = Path("wa_debug") / datetime.now().strftime("%Y%m%d-%H%M%S")
    debug_dir.mkdir(parents=True, exist_ok=True)

    # Remove Chrome lock files before opening WhatsApp (as send() does)
    import subprocess as _sp
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"]:
        lock_path = Path(sender.session_dir) / lock
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

    groups_sent = 0
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            sender.session_dir,
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
            opened = await sender._open_group(page, group, debug_dir)
            if not opened:
                print(f"   ❌ Could not open group '{group}' — skipping.")
                continue
            try:
                await sender._send_image(page, image_path,
                                         Path(image_path).stem, debug_dir)
                # The sender's internal delivery check only warns; verify
                # here and only count/close once truly delivered.
                confirmed = False
                try:
                    await page.wait_for_selector(
                        'span[data-icon="msg-time"]', state="hidden",
                        timeout=60_000)
                    await page.wait_for_selector(
                        'span[data-icon="msg-check"], '
                        'span[data-icon="msg-dblcheck"]', timeout=30_000)
                    confirmed = True
                except Exception:
                    pass
                if confirmed:
                    groups_sent += 1
                    print(f"   ✅ Delivery confirmed for '{group}'.")
                else:
                    await page.screenshot(
                        path=str(debug_dir / f"UNCONFIRMED_{group[:20]}.png"))
                    print(f"   ⚠️ Could not confirm delivery to '{group}' — "
                          f"screenshot saved, not counted as sent.")
            except Exception as e:
                print(f"   ⚠️ Failed sending to '{group}': {e}")

        await asyncio.sleep(5)
        if groups_sent == len(groups):
            print("\n✅ Report sent to all groups.")
        elif groups_sent:
            print(f"\n⚠️ Sent to {groups_sent}/{len(groups)} group(s).")
        else:
            print("\n❌ NO groups received the report.")
        await ctx.close()
        print("   WhatsApp browser closed.")

    return groups_sent


async def main():
    json_path = os.environ.get("SALES_REPORT_JSON", "")
    groups = _tolerant_groups(os.environ.get("SALES_WA_GROUPS", "[]"))
    if not json_path or not Path(json_path).exists():
        print("❌ SALES_REPORT_JSON missing"); sys.exit(1)
    if not groups:
        print("❌ No WhatsApp groups configured for the Sales Report agent.")
        print("   Set them in the agent's settings on the dashboard.")
        sys.exit(1)

    rep = json.loads(Path(json_path).read_text(encoding="utf-8"))
    print(f"📊 Report: {rep['group']} {rep['date_from']} → {rep['date_to']}")
    print(f"📋 Groups: {groups}")

    image = await _render_image(rep)
    sent = await _send_to_groups(image, groups)
    if sent == 0:
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
