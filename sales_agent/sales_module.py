"""
sales_module.py — Sales Report agent logic (Phase 1: uploaded day books).

Responsibilities:
  - parse_sales_excel(): read a Focus "Sales day book" export into row dicts
  - upsert_sales(): store rows with (voucher, item) replace-dedupe
  - build_report(): salesman x item quantity matrix with targets/FOC
  - report_pdf(): render a report dict to a PDF file

Kept separate from main.py so the logic is testable standalone.
"""
from __future__ import annotations
import io
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

# ────────────────────────────────────────────────────────────────────
# Parsing
# ────────────────────────────────────────────────────────────────────

# Header names as they appear in the Focus export (row with "Date")
_COLMAP = {
    "date":          ["date"],
    "voucher":       ["voucher"],
    "salesman":      ["salesman name", "salesman"],
    "customer":      ["customer account name", "customer"],
    "item_name":     ["item"],
    "qty":           ["quantity"],
    "qty_pieces":    ["quantity in base unit"],
    "unit":          ["unit"],
    "rate":          ["rate"],
    "rate_pcs":      ["s.rate / pcs", "s.rate/pcs", "rate / pcs"],
    "gross":         ["gross"],
    "qty_per_ctn":   ["qty per ctn"],
    "qty_ctn":       ["sal qty in ctn"],
    "base_link_doc": ["base link doc", "base link doc  number", "base link doc number"],
    "segment":       ["customer segment name", "customer segment"],
}


def _num(v) -> float:
    s = str(v if v is not None else "").replace(",", "").replace("\xa0", "").strip()
    if not s or s.lower() == "nan":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_sales_excel(data: bytes) -> tuple[list[dict], dict]:
    """Parse the Sales day book Excel. Returns (rows, stats).
    Finds the header row dynamically (the row whose first cells include
    'Date' and 'Voucher'), so leading report banners don't matter."""
    raw = pd.read_excel(io.BytesIO(data), sheet_name=0, header=None)

    header_idx = None
    for i in range(min(15, len(raw))):
        vals = [str(x).strip().lower() for x in raw.iloc[i].tolist()]
        if "date" in vals and any("voucher" in v for v in vals):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Header row not found — is this a Sales day book export?")

    headers = [str(x).strip() for x in raw.iloc[header_idx].tolist()]
    df = raw.iloc[header_idx + 1:].copy()
    df.columns = headers

    # Resolve each of our fields to the actual column name
    def find_col(cands):
        low = {str(c).strip().lower(): c for c in df.columns}
        for cand in cands:
            if cand in low:
                return low[cand]
        # substring fallback
        for cand in cands:
            for k, orig in low.items():
                if cand in k:
                    return orig
        return None

    resolved = {field: find_col(cands) for field, cands in _COLMAP.items()}
    missing = [f for f in ("date", "voucher", "salesman", "item_name", "qty")
               if not resolved[f]]
    if missing:
        raise ValueError(f"Missing required columns: {missing}. "
                         f"Found: {list(df.columns)}")

    rows = []
    for _, r in df.iterrows():
        item = str(r.get(resolved["item_name"], "") or "").replace("\xa0", " ").strip()
        voucher = str(r.get(resolved["voucher"], "") or "").strip()
        if not item or item.lower() == "nan" or not voucher or voucher.lower() == "nan":
            continue  # footer/summary rows

        # Date normalisation → YYYY-MM-DD
        dval = r.get(resolved["date"])
        try:
            d = pd.to_datetime(dval).strftime("%Y-%m-%d")
        except Exception:
            continue

        unit = str(r.get(resolved["unit"], "") or "").replace("\xa0", " ").strip()
        qty = _num(r.get(resolved["qty"]))
        qty_pieces = _num(r.get(resolved["qty_pieces"])) if resolved["qty_pieces"] else 0.0
        qty_per_ctn = _num(r.get(resolved["qty_per_ctn"])) if resolved["qty_per_ctn"] else 0.0
        qty_ctn = _num(r.get(resolved["qty_ctn"])) if resolved["qty_ctn"] else 0.0

        # Fallbacks when helper columns are blank or absent in the export
        up = unit.lstrip(".").lower()
        is_pieces_unit = any(p in up for p in ("pcs", "pc", "piece"))
        if qty_pieces == 0.0 and qty:
            if is_pieces_unit:
                qty_pieces = qty
            elif qty_per_ctn:
                qty_pieces = qty * qty_per_ctn
        # Sal Qty in CTN = Quantity in base unit / Qty Per CTN
        if qty_ctn == 0.0 and qty_pieces and qty_per_ctn:
            qty_ctn = qty_pieces / qty_per_ctn

        rate = _num(r.get(resolved["rate"])) if resolved["rate"] else 0.0
        rate_pcs = _num(r.get(resolved["rate_pcs"])) if resolved["rate_pcs"] else 0.0
        # S.RATE / PCS = Rate / Qty Per CTN (CTN-type rows);
        # for pcs-unit rows the Rate is already per piece
        if rate_pcs == 0.0 and rate:
            if is_pieces_unit:
                rate_pcs = rate
            elif qty_per_ctn:
                rate_pcs = round(rate / qty_per_ctn, 4)

        rows.append({
            "date": d,
            "voucher": voucher,
            "salesman": str(r.get(resolved["salesman"], "") or "").strip() or "UNKNOWN",
            "customer": str(r.get(resolved["customer"], "") or "").strip(),
            "item_name": item,
            "qty": qty,
            "unit": unit,
            "qty_pieces": qty_pieces,
            "qty_ctn": qty_ctn,
            "rate": rate,
            "rate_pcs": rate_pcs,
            "gross": _num(r.get(resolved["gross"])) if resolved["gross"] else 0.0,
            "qty_per_ctn": qty_per_ctn,
            "base_link_doc": str(r.get(resolved["base_link_doc"], "") or "").strip()
                             if resolved["base_link_doc"] else "",
            "segment": str(r.get(resolved["segment"], "") or "").strip()
                       if resolved["segment"] else "",
            "is_foc": 1 if "foc" in unit.lower() else 0,
        })

    if not rows:
        raise ValueError("No data rows found in the file.")

    dates = sorted(r["date"] for r in rows)
    stats = {
        "rows": len(rows),
        "date_from": dates[0],
        "date_to": dates[-1],
        "items": len({r["item_name"] for r in rows}),
        "salesmen": len({r["salesman"] for r in rows}),
        "foc_rows": sum(r["is_foc"] for r in rows),
    }
    return rows, stats


def upsert_sales(conn: sqlite3.Connection, rows: list[dict]) -> dict:
    """Replace-by-(voucher,item) dedupe: delete existing rows for every
    (voucher, item) pair present in the upload, then insert all upload rows.
    Safe for brand-filtered partial files and for Focus corrections."""
    pairs = {(r["voucher"], r["item_name"]) for r in rows}
    deleted = 0
    for v, i in pairs:
        cur = conn.execute(
            "DELETE FROM sales_data WHERE voucher=? AND item_name=?", (v, i))
        deleted += cur.rowcount
    conn.executemany("""
        INSERT INTO sales_data
            (date, voucher, salesman, customer, item_name, qty, unit,
             qty_pieces, qty_ctn, rate, rate_pcs, gross, qty_per_ctn,
             base_link_doc, segment, is_foc)
        VALUES (:date,:voucher,:salesman,:customer,:item_name,:qty,:unit,
                :qty_pieces,:qty_ctn,:rate,:rate_pcs,:gross,:qty_per_ctn,
                :base_link_doc,:segment,:is_foc)
    """, rows)
    conn.commit()
    return {"inserted": len(rows), "replaced": deleted}


# ────────────────────────────────────────────────────────────────────
# Report building
# ────────────────────────────────────────────────────────────────────

def build_report(conn: sqlite3.Connection, group_id: int,
                 date_from: str, date_to: str) -> dict:
    """Salesman x item quantity report for one product group + date range.

    Returns:
    {
      group, date_from, date_to, generated_at,
      salesmen: [names sorted by total pieces desc],
      items: [{
         item, unit_ctn (qty_per_ctn observed),
         per_salesman: {name: {pieces, ctn, foc_pieces}},
         total: {pieces, ctn, foc_pieces},
         target: {qty, unit, achieved_pct} | None,
         salesman_targets: [{salesman, qty, unit, actual, achieved_pct}],
         zero_sellers: [names]
      }],
      grand: {pieces, ctn, foc_pieces},
      ranking: [{salesman, pieces, ctn}]
    }
    """
    g = conn.execute("SELECT name FROM product_groups WHERE id=?",
                     (group_id,)).fetchone()
    if not g:
        raise ValueError("Group not found")
    group_name = g[0]

    item_rows = conn.execute(
        "SELECT item_name FROM group_items WHERE group_id=? ORDER BY item_name",
        (group_id,)).fetchall()
    items = [r[0] for r in item_rows]
    if not items:
        raise ValueError("Group has no items")

    # Salesmen config: names flagged 0 (or unknown, once configured)
    # roll up into one DIRECT SALES bucket
    _cfg = {r[0]: r[1] for r in conn.execute(
        "SELECT name, is_salesman FROM salesmen_config").fetchall()}
    _configured = len(_cfg) > 0

    def _bucket(name: str) -> str:
        if not _configured:
            return name
        return name if _cfg.get(name, 0) else "DIRECT SALES"

    q_marks = ",".join("?" * len(items))
    data = conn.execute(f"""
        SELECT item_name, salesman, is_foc,
               SUM(qty_pieces) AS pieces, SUM(qty_ctn) AS ctn,
               MAX(qty_per_ctn) AS per_ctn
        FROM sales_data
        WHERE item_name IN ({q_marks}) AND date >= ? AND date <= ?
        GROUP BY item_name, salesman, is_foc
    """, (*items, date_from, date_to)).fetchall()
    data = [(it, _bucket(sm), foc, p, ctn, pc)
            for (it, sm, foc, p, ctn, pc) in data]

    targets = conn.execute("""
        SELECT item_name, salesman, target_qty, target_unit
        FROM group_targets WHERE group_id=?
    """, (group_id,)).fetchall()

    # Salesmen = anyone who sold anything in range for these items
    salesmen_set = {r[1] for r in data}
    # plus anyone with a personal target (so their 0 shows)
    for t in targets:
        if t[1]:
            salesmen_set.add(t[1])

    # Accumulate
    acc = {}   # item -> salesman -> {pieces, ctn, foc}
    per_ctn_map = {}
    for item, sman, is_foc, pieces, ctn, per_ctn in data:
        cell = acc.setdefault(item, {}).setdefault(
            sman, {"pieces": 0.0, "ctn": 0.0, "foc_pieces": 0.0})
        if is_foc:
            cell["foc_pieces"] += pieces or 0
        else:
            cell["pieces"] += pieces or 0
            cell["ctn"] += ctn or 0
        if per_ctn:
            per_ctn_map[item] = max(per_ctn_map.get(item, 0), per_ctn)

    # Salesman ranking by total sold pieces
    totals_by_sman = {}
    for item, smap in acc.items():
        for sman, cell in smap.items():
            t = totals_by_sman.setdefault(sman, {"pieces": 0.0, "ctn": 0.0})
            t["pieces"] += cell["pieces"]
            t["ctn"] += cell["ctn"]
    ranking = sorted(
        ({"salesman": s, "pieces": round(v["pieces"], 1),
          "ctn": round(v["ctn"], 2)} for s, v in totals_by_sman.items()),
        key=lambda x: -x["pieces"])
    salesmen = [r["salesman"] for r in ranking]
    for s in sorted(salesmen_set - set(salesmen)):
        salesmen.append(s)  # target-only people with zero sales at the end

    def _ach(actual_pieces, actual_ctn, tqty, tunit):
        actual = actual_ctn if (tunit or "CTN").upper() == "CTN" else actual_pieces
        if not tqty:
            return None
        return round(actual / tqty * 100, 1)

    out_items = []
    grand = {"pieces": 0.0, "ctn": 0.0, "foc_pieces": 0.0}
    for item in items:
        smap = acc.get(item, {})
        per_salesman = {}
        tot = {"pieces": 0.0, "ctn": 0.0, "foc_pieces": 0.0}
        for sman in salesmen:
            cell = smap.get(sman, {"pieces": 0.0, "ctn": 0.0, "foc_pieces": 0.0})
            per_salesman[sman] = {
                "pieces": round(cell["pieces"], 1),
                "ctn": round(cell["ctn"], 2),
                "foc_pieces": round(cell["foc_pieces"], 1),
            }
            for k in tot:
                tot[k] += cell[k]
        for k in grand:
            grand[k] += tot[k]

        # Item-level (overall) target = row with salesman NULL/''
        overall_t = next((t for t in targets
                          if t[0] == item and not t[1]), None)
        target = None
        if overall_t:
            target = {
                "qty": overall_t[2], "unit": (overall_t[3] or "CTN").upper(),
                "achieved_pct": _ach(tot["pieces"], tot["ctn"],
                                     overall_t[2], overall_t[3]),
            }

        sman_targets = []
        for t in targets:
            if t[0] == item and t[1]:
                cell = per_salesman.get(t[1],
                        {"pieces": 0.0, "ctn": 0.0, "foc_pieces": 0.0})
                unit = (t[3] or "CTN").upper()
                actual = cell["ctn"] if unit == "CTN" else cell["pieces"]
                sman_targets.append({
                    "salesman": t[1], "qty": t[2], "unit": unit,
                    "actual": actual,
                    "achieved_pct": _ach(cell["pieces"], cell["ctn"], t[2], t[3]),
                })
        sman_targets.sort(key=lambda x: (x["achieved_pct"] is None,
                                         -(x["achieved_pct"] or 0)))

        zero = [s for s in salesmen
                if per_salesman[s]["pieces"] == 0 and per_salesman[s]["foc_pieces"] == 0]

        out_items.append({
            "item": item,
            "qty_per_ctn": per_ctn_map.get(item, 0),
            "per_salesman": per_salesman,
            "total": {k: round(v, 2) for k, v in tot.items()},
            "target": target,
            "salesman_targets": sman_targets,
            "zero_sellers": zero,
        })

    return {
        "group": group_name,
        "group_id": group_id,
        "date_from": date_from,
        "date_to": date_to,
        "generated_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "salesmen": salesmen,
        "items": out_items,
        "grand": {k: round(v, 2) for k, v in grand.items()},
        "ranking": ranking,
    }


# ────────────────────────────────────────────────────────────────────
# PDF
# ────────────────────────────────────────────────────────────────────

def report_pdf(report: dict, out_path: str) -> str:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle)

    INK = colors.HexColor("#1a2340")
    ACCENT = colors.HexColor("#0e7d6b")
    SOFT = colors.HexColor("#e9f5f2")
    GREY = colors.HexColor("#5a6379")
    LINE = colors.HexColor("#d5dde5")
    RED = colors.HexColor("#c0392b")
    GREEN = colors.HexColor("#1e8449")

    ss = getSampleStyleSheet()
    H = ParagraphStyle("H", parent=ss["Title"], fontName="Helvetica-Bold",
                       fontSize=17, textColor=INK, spaceAfter=2)
    S = ParagraphStyle("S", parent=ss["Normal"], fontSize=9.5, textColor=GREY,
                       spaceAfter=8)
    H2 = ParagraphStyle("H2", parent=ss["Normal"], fontName="Helvetica-Bold",
                        fontSize=11, textColor=ACCENT, spaceBefore=10,
                        spaceAfter=4)
    CE = ParagraphStyle("CE", parent=ss["Normal"], fontSize=8, leading=10,
                        textColor=INK)
    CEB = ParagraphStyle("CEB", parent=CE, fontName="Helvetica-Bold")

    page = landscape(A4) if len(report["salesmen"]) > 6 else A4
    doc = SimpleDocTemplate(out_path, pagesize=page,
                            leftMargin=14*mm, rightMargin=14*mm,
                            topMargin=12*mm, bottomMargin=12*mm,
                            title=f"Sales Report - {report['group']}")
    story = []
    story.append(Paragraph(f"Sales Report — {report['group']}", H))
    story.append(Paragraph(
        f"{report['date_from']} to {report['date_to']} · generated "
        f"{report['generated_at']} · quantities in CTN (pieces below)", S))

    # Matrix table: rows = items, cols = salesmen + total + target + ach
    smen = report["salesmen"]
    has_targets = any(i["target"] for i in report["items"])
    header = ["Item"] + smen + ["TOTAL"]
    if has_targets:
        header += ["Target", "Ach%"]
    data = [[Paragraph(f"<b>{h}</b>",
             ParagraphStyle("hh", parent=CE, textColor=colors.white,
                            fontName="Helvetica-Bold")) for h in header]]

    def cellfmt(c):
        if c["ctn"]:
            s = f"{c['ctn']:g} ctn<br/><font size=6.5 color='#5a6379'>{c['pieces']:g} pcs</font>"
        elif c["pieces"]:
            s = f"{c['pieces']:g} pcs"
        else:
            s = "-"
        if c.get("foc_pieces"):
            s += f"<br/><font size=6.5 color='#b58a00'>FOC {c['foc_pieces']:g}</font>"
        return Paragraph(s, CE)

    for it in report["items"]:
        row = [Paragraph(f"<b>{it['item']}</b>", CE)]
        for s in smen:
            row.append(cellfmt(it["per_salesman"][s]))
        row.append(cellfmt(it["total"]))
        if has_targets:
            if it["target"]:
                t = it["target"]
                row.append(Paragraph(f"{t['qty']:g} {t['unit'].lower()}", CE))
                pct = t["achieved_pct"]
                col = GREEN if (pct or 0) >= 100 else (RED if (pct or 0) < 60 else INK)
                row.append(Paragraph(
                    f"<font color='{col.hexval() if hasattr(col,'hexval') else col}'>"
                    f"<b>{pct if pct is not None else '-'}%</b></font>", CE))
            else:
                row += [Paragraph("-", CE), Paragraph("-", CE)]
        data.append(row)

    n_cols = len(header)
    avail = page[0] - 28*mm
    item_w = min(62*mm, avail * 0.28)
    other_w = (avail - item_w) / (n_cols - 1)
    t = Table(data, colWidths=[item_w] + [other_w] * (n_cols - 1),
              repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), SOFT))
    t.setStyle(TableStyle(style))
    story.append(t)

    # Per-salesman targets
    rows_t = []
    for it in report["items"]:
        for st_ in it["salesman_targets"]:
            pct = st_["achieved_pct"]
            col = "#1e8449" if (pct or 0) >= 100 else ("#c0392b" if (pct or 0) < 60 else "#1a2340")
            rows_t.append([
                Paragraph(it["item"], CE),
                Paragraph(st_["salesman"], CE),
                Paragraph(f"{st_['actual']:g} / {st_['qty']:g} {st_['unit'].lower()}", CE),
                Paragraph(f"<font color='{col}'><b>{pct if pct is not None else '-'}%</b></font>", CE),
            ])
    if rows_t:
        story.append(Paragraph("Personal targets", H2))
        tt = Table([[Paragraph(f"<b>{h}</b>", ParagraphStyle(
                    "hh2", parent=CE, textColor=colors.white))
                    for h in ["Item", "Salesman", "Actual / Target", "Ach%"]]]
                   + rows_t,
                   colWidths=[avail*0.4, avail*0.25, avail*0.22, avail*0.13],
                   repeatRows=1)
        tt.setStyle(TableStyle(style[:7] + [("GRID", (0, 0), (-1, -1), 0.5, LINE),
                    ("BACKGROUND", (0, 0), (-1, 0), ACCENT)]))
        story.append(tt)

    # Zero sellers summary
    zero_lines = []
    for it in report["items"]:
        if it["zero_sellers"]:
            zero_lines.append(f"<b>{it['item']}</b>: "
                              + ", ".join(it["zero_sellers"]))
    if zero_lines:
        story.append(Paragraph("Zero sellers (no sales in range)", H2))
        for z in zero_lines:
            story.append(Paragraph(z, CE))

    # Ranking
    story.append(Paragraph("Salesman ranking (group total)", H2))
    for i, r in enumerate(report["ranking"], 1):
        story.append(Paragraph(
            f"{i}. <b>{r['salesman']}</b> — {r['ctn']:g} ctn "
            f"({r['pieces']:g} pcs)", CE))

    doc.build(story)
    return out_path


# ────────────────────────────────────────────────────────────────────
# Product-list upload (create/extend a group from a CSV or Excel)
# ────────────────────────────────────────────────────────────────────

_HEADER_WORDS = {"product", "products", "product name", "item", "items",
                 "item name", "description", "name", "product list"}


def parse_product_list(data: bytes, filename: str = "") -> list[str]:
    """Parse a one-column product list (CSV or Excel). Tolerates:
    - header variations (PRODUCT / Item Name / etc.) or no header at all
    - BOM, blank lines, duplicate names, extra columns (first column wins)
    Returns the cleaned, de-duplicated product names in file order."""
    name = (filename or "").lower()
    values = []
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(data), sheet_name=0, header=None)
        if df.empty:
            return []
        values = [str(v) for v in df.iloc[:, 0].tolist()]
    else:
        text = data.decode("utf-8-sig", errors="replace")
        for line in text.splitlines():
            # take the first CSV cell only
            cell = line.split(",")[0]
            values.append(cell)

    out, seen = [], set()
    for i, v in enumerate(values):
        v = str(v).replace("\xa0", " ").strip().strip('"').strip()
        if not v or v.lower() == "nan":
            continue
        # Skip a header-looking first row
        if i == 0 and v.lower().strip() in _HEADER_WORDS:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out
