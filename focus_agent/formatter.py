"""
formatter.py — WhatsApp alert formatter for low-price items
"""
from datetime import datetime

def format_whatsapp_alert(alerts: list) -> str:
    if not alerts:
        return "✅ No low-price issues found in today's Sales Orders."

    now   = datetime.now().strftime("%d %b %Y, %I:%M %p")
    lines = []
    lines.append("🚨 *LOW PRICE ALERT*")
    lines.append(f"🗓️ {now}")
    lines.append(f"📦 {len(alerts)} order(s) with price issue(s)")
    lines.append("━" * 32)

    for alert in alerts:
        lines.append(f"\n📋 *{alert['voucher']}*")
        lines.append(f"🏢 {alert['party']}")
        lines.append(f"👤 Salesman: {alert.get('salesman', 'Unknown')}")
        lines.append("")

        for item in alert["items"]:
            margin = item["margin"]
            if margin < 0:
                indicator = "🔴 BELOW COST"
            elif margin < 5:
                indicator = "🟡 THIN"
            elif margin < 15:
                indicator = "🟢 OK"
            else:
                indicator = "✅ HEALTHY"

            ppu = item["pieces_per_unit"]
            unit_note = f" ({ppu} pcs/CTN)" if ppu > 1 else ""

            lines.append(f"  *{item['item']}*")
            lines.append(f"  📦 Sold As          : {item['unit'].lstrip('.')}{unit_note}")
            lines.append(f"  💰 Cost/pc          : ${item['pricebook']:.2f}")
            lines.append(f"  🚚 Landing Cost/pc  : ${item['landing']:.2f}  _({item.get('landing_label','+5%')})_")
            lines.append(f"  📅 Prev. Price      : ${item['prev_price']:.2f}")
            lines.append(f"  🔢 Qty Ordered      : {item['quantity']:.0f} {item['unit'].lstrip('.')}")
            lines.append(f"  💸 Rate/pc          : ${item['rate_per_piece']:.2f}")
            lines.append(f"  📊 Margin           : {margin:.1f}%  {indicator}")
            lines.append("")

        lines.append("─" * 32)

    lines.append("\n_Sent by Focus ERP Agent 🤖_")
    return "\n".join(lines)


def format_single_alert(alert: dict) -> str:
    """Format one SO as a standalone WhatsApp message."""
    from datetime import datetime
    now   = datetime.now().strftime("%d %b %Y, %I:%M %p")
    lines = []

    lines.append(f"🚨 *LOW PRICE ALERT*  |  {now}")
    lines.append("━" * 32)
    lines.append(f"📋 *{alert['voucher']}*")
    lines.append(f"🏢 {alert['party']}")
    lines.append(f"👤 Salesman: {alert.get('salesman', 'Unknown')}")
    lines.append("")

    for item in alert["items"]:
        margin = item["margin"]
        if margin < 0:
            indicator = "🔴 BELOW COST"
        elif margin < 5:
            indicator = "🟡 THIN"
        elif margin < 15:
            indicator = "🟢 OK"
        else:
            indicator = "✅ HEALTHY"

        ppu = item["pieces_per_unit"]
        unit_note = f" ({ppu} pcs/CTN)" if ppu > 1 else ""

        lines.append(f"  *{item['item']}*")
        lines.append(f"  📦 Unit             : {item['unit'].lstrip('.')}{unit_note}")
        lines.append(f"  🔢 Qty Ordered      : {item['quantity']:.0f} {item['unit'].lstrip('.')}")
        lines.append(f"  💰 Cost/pc          : ${item['pricebook']:.2f}")
        lines.append(f"  🚚 Landing Cost/pc  : ${item['landing']:.2f}  _({item.get('landing_label','+5%')})_")
        lines.append(f"  📅 Prev. Price/pc   : ${item['prev_price']:.2f}")
        lines.append(f"  💸 Rate/pc          : ${item['rate_per_piece']:.2f}")
        lines.append(f"  📊 Margin           : {margin:.1f}%  {indicator}")
        lines.append("")

    lines.append("━" * 32)
    lines.append("_Reply to take action — Focus ERP Agent 🤖_")
    return "\n".join(lines)
