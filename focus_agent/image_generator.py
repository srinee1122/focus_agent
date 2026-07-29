"""
image_generator.py
Renders a Sales Order alert as a clean PNG table image using Playwright.
"""
import asyncio
import os
from datetime import datetime
from pathlib import Path


def _build_html(alert: dict) -> str:
    voucher  = alert.get("voucher", "")
    party    = alert.get("party", "")
    salesman = alert.get("salesman", "")
    items    = alert.get("items", [])
    ts       = datetime.now().strftime("%d %b %Y  %I:%M %p")

    rows = ""
    for item in items:
        margin = item["margin"]
        diff   = item.get("diff", 0)

        if margin < 0:
            row_cls = 'class="below-cost"'
        elif margin < 5:
            row_cls = 'class="thin-margin"'
        else:
            row_cls = ''

        diff_cls   = "diff-neg"   if diff   < 0 else "diff-pos"
        margin_cls = "margin-neg" if margin < 0 else ("margin-thin" if margin < 5 else "margin-ok")
        indicator  = "🔴" if margin < 0 else ("🟡" if margin < 5 else "🟢")
        diff_sign  = "+" if diff >= 0 else ""
        mgn_sign   = "+" if margin >= 0 else ""

        unit_note = f"&nbsp;<small>({item['pieces_per_unit']}pcs)</small>" \
                    if item["pieces_per_unit"] > 1 else ""

        rows += f"""
        <tr {row_cls}>
          <td class="item-name">{item['item']}</td>
          <td>{item['unit'].lstrip('.')}{unit_note}</td>
          <td class="num">{int(item['quantity'])}</td>
          <td class="num">${item['landing']:.2f}</td>
          <td class="num">${item['rate_per_piece']:.2f}</td>
          <td class="num {diff_cls}">{diff_sign}${abs(diff):.2f}</td>
          <td class="num {margin_cls}">{mgn_sign}{margin:.1f}%&nbsp;{indicator}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f4f4f8;
       display:inline-block;padding:16px}}
  .card{{width:780px;background:#fff;border-radius:14px;overflow:hidden;
         box-shadow:0 4px 24px rgba(0,0,0,.13)}}
  .hdr{{background:#12122a;color:#fff;padding:15px 20px}}
  .hdr-top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px}}
  .badge{{background:#e74c3c;color:#fff;padding:4px 14px;border-radius:20px;
          font-size:12px;font-weight:700;letter-spacing:.04em}}
  .dt{{color:#8888aa;font-size:12px}}
  .so-line{{font-size:13px;color:#bbb}}
  .so-num{{font-weight:700;font-size:16px;color:#fff}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{background:#f0f0f6;color:#777;font-size:10px;text-transform:uppercase;
      letter-spacing:.06em;padding:9px 12px;border-bottom:2px solid #e4e4ee}}
  th.num{{text-align:right}}
  td{{padding:9px 12px;border-bottom:1px solid #f0f0f6;color:#2a2a3a;vertical-align:middle}}
  td.item-name{{font-weight:500;max-width:260px;word-break:break-word}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums}}
  .below-cost{{background:#fff5f5}}
  .thin-margin{{background:#fffdf0}}
  .diff-neg{{color:#c0392b;font-weight:700}}
  .diff-pos{{color:#27ae60;font-weight:600}}
  .margin-neg{{color:#c0392b;font-weight:700}}
  .margin-thin{{color:#e67e22;font-weight:600}}
  .margin-ok{{color:#27ae60}}
  .footer{{background:#f8f8fc;padding:9px 18px;display:flex;
           justify-content:space-between;font-size:11px;color:#aaa;
           border-top:1px solid #eee}}
</style>
</head>
<body>
<div class="card">
  <div class="hdr">
    <div class="hdr-top">
      <span class="badge">🚨 LOW PRICE ALERT</span>
      <span class="dt">{ts}</span>
    </div>
    <div class="so-line">
      <span class="so-num">SO: {voucher}</span>
      &nbsp;·&nbsp; {party} &nbsp;·&nbsp; Salesman: {salesman}
    </div>
  </div>
  <table>
    <thead><tr>
      <th>Item</th>
      <th>Unit</th>
      <th class="num">Qty</th>
      <th class="num">Landing $</th>
      <th class="num">Rate $</th>
      <th class="num">Diff $</th>
      <th class="num">Margin</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="footer">
    <span>ShopAide Agent 🤖</span>
    <span>Sri Ambikas Pte Ltd</span>
  </div>
</div>
</body>
</html>"""


async def generate_alert_image(alert: dict, output_dir: str = "downloads") -> str:
    """
    Render alert as PNG. Returns path to saved image.
    Uses headless Playwright — no visible browser window.
    """
    from playwright.async_api import async_playwright

    os.makedirs(output_dir, exist_ok=True)
    voucher   = str(alert.get("voucher", "SO")).replace(" ", "_").replace(":", "")
    filename  = f"alert_{voucher}_{datetime.now().strftime('%H%M%S')}.jpg"
    out_path  = str(Path(output_dir) / filename)

    html = _build_html(alert)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page(viewport={"width": 900, "height": 800})
        await page.set_content(html, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)
        card = page.locator(".card")
        # Save as JPEG — no transparency, WhatsApp sends as regular photo not sticker
        await card.screenshot(path=out_path, type="jpeg", quality=92)
        await browser.close()

    print(f"   📸 Image saved: {out_path}")
    return out_path
