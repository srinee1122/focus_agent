"""
formatter.py — WhatsApp message formatting for Low Price Alerts
"""
from datetime import datetime


def _margin_indicator(margin: float) -> str:
    if margin < 0:    return "🔴 BELOW COST"
    if margin < 5:    return "🟡 THIN"
    if margin < 15:   return "🟢 OK"
    return "✅ HEALTHY"


def format_text_summary(alert: dict) -> str:
    """
    Short text header sent alongside (or instead of) the image.
    """
    raw_voucher = alert.get("voucher", "")
    voucher = raw_voucher.replace("SO : ", "").replace("SO:", "").strip()
    party    = alert.get("party", "")
    salesman = alert.get("salesman", "")
    items    = alert.get("items", [])
    ts       = datetime.now().strftime("%d %b %Y, %I:%M %p")

    below = [i for i in items if i["margin"] < 0]
    thin  = [i for i in items if 0 <= i["margin"] < 5]

    flags = []
    if below: flags.append(f"*{len(below)} below cost*")
    if thin:  flags.append(f"{len(thin)} thin margin")
    flag_str = " · ".join(flags) if flags else "margins OK"

    lines = [
        f"🚨 *LOW PRICE ALERT*  |  {ts}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📋 *SO : {voucher}*",
        f"🏢 {party}",
        f"👤 Salesman: {salesman}",
        f"⚠️ {flag_str}  ({len(items)} item{'s' if len(items)!=1 else ''})",
        f"_Reply to take action — ShopAide Agent 🤖_",
    ]
    return "\n".join(lines)


def format_single_alert(alert: dict) -> str:
    """
    Full detailed text message (used when image is OFF or as fallback).
    """
    raw_voucher = alert.get("voucher", "")
    voucher = raw_voucher.replace("SO : ", "").replace("SO:", "").strip()
    party    = alert.get("party", "")
    salesman = alert.get("salesman", "")
    items    = alert.get("items", [])
    ts       = datetime.now().strftime("%d %b %Y, %I:%M %p")

    lines = [
        f"🚨 *LOW PRICE ALERT*  |  {ts}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📋 *SO : {voucher}*",
        f"🏢 {party}",
        f"👤 Salesman: {salesman}",
        "",
    ]

    for item in items:
        margin     = item["margin"]
        diff       = item.get("diff", 0)
        indicator  = _margin_indicator(margin)
        diff_sign  = "+" if diff >= 0 else ""
        mgn_sign   = "+" if margin >= 0 else ""

        unit_note  = f" ({item['pieces_per_unit']} pcs/{item['unit']})" \
                     if item["pieces_per_unit"] > 1 else ""

        lines += [
            f"  *{item['item']}*",
            f"  📦 Unit             : {item['unit']}{unit_note}",
            f"  🔢 Qty Ordered      : {item['quantity']:.0f} {item['unit']}",
            f"  🏷️ Purchase Cost/pc : ${item['pricebook']:.2f}",
            f"  🚚 Landing Cost/pc  : ${item['landing']:.2f}  _({item.get('landing_label','+5%')})_",
            f"  📅 Prev. Price/pc   : ${item['prev_price']:.2f}",
            f"  💸 Rate/pc          : ${item['rate_per_piece']:.2f}",
            f"  📊 Diff (Rate−Land) : {diff_sign}${abs(diff):.2f}",
            f"  📈 Margin           : {mgn_sign}{margin:.1f}%  {indicator}",
            "",
        ]

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Reply to take action — ShopAide Agent 🤖_",
    ]
    return "\n".join(lines)
