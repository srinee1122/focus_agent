"""
runsheet_api.py — Runsheet agent, self-contained FastAPI router.

Builds one-page driver runsheets from the (synced) sales day book:
  GET  /api/runsheet/invoices?date=       invoices available for a date
  GET  /api/runsheet/columns              frequent round-item column config
  PUT  /api/runsheet/columns              save column config
  GET  /api/runsheet/run-items?vouchers=  items on the selected invoices
  POST /api/runsheet/build                compute + render printable HTML
  GET  /api/runsheet/history              saved runsheets
  GET  /api/runsheet/print/{id}           re-render a saved runsheet

Isolated by design (guarded include in main.py): failures here only
remove /api/runsheet/* routes.
"""
from __future__ import annotations
import json
import math
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

_THIS_DIR = Path(__file__).resolve().parent

from database import get_db  # dashboard is the server CWD

router = APIRouter()
_schema_ready = False

DEFAULT_COLUMNS = [
    {"code": "OG 5K",  "pack": "5KG ×6",   "item_name": "", "qty": 6},
    {"code": "OG 10K", "pack": "10KG ×3",  "item_name": "", "qty": 3},
    {"code": "OG 25K", "pack": "25KG BAG", "item_name": "", "qty": 1},
    {"code": "ID 5K",  "pack": "5KG ×6",   "item_name": "", "qty": 6},
    {"code": "ID 25K", "pack": "25KG BAG", "item_name": "", "qty": 1},
    {"code": "P 1K",   "pack": "1KG ×20",  "item_name": "", "qty": 20},
    {"code": "P.G",    "pack": "5KG ×4",   "item_name": "", "qty": 4},
    {"code": "P.M",    "pack": "5KG ×4",   "item_name": "", "qty": 4},
]


def _ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runsheets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_no TEXT, run_date TEXT, area TEXT,
                del_date TEXT, del_man TEXT, veh_no TEXT,
                payload TEXT, created_at TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runsheet_settings (
                key TEXT PRIMARY KEY, value TEXT
            )""")
        row = conn.execute("SELECT value FROM runsheet_settings "
                           "WHERE key='columns'").fetchone()
        if not row:
            conn.execute(
                "INSERT INTO runsheet_settings (key, value) VALUES "
                "('columns', ?)", (json.dumps(DEFAULT_COLUMNS),))
        conn.commit()
    _schema_ready = True


def _columns(conn) -> list:
    row = conn.execute("SELECT value FROM runsheet_settings "
                       "WHERE key='columns'").fetchone()
    return json.loads(row[0]) if row else list(DEFAULT_COLUMNS)


@router.get("/api/runsheet/columns")
async def get_columns():
    _ensure_schema()
    with get_db() as conn:
        return _columns(conn)


@router.put("/api/runsheet/columns")
async def put_columns(body: dict):
    _ensure_schema()
    cols = body.get("cols") or []
    clean = []
    for c in cols:
        code = str(c.get("code") or "").strip()
        if not code:
            continue
        try:
            qty = max(1, int(c.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1
        unit = str(c.get("unit") or "pcs").lower()
        clean.append({"code": code[:12],
                      "pack": str(c.get("pack") or "")[:14],
                      "item_name": str(c.get("item_name") or "").strip(),
                      "qty": qty,
                      "unit": unit if unit in ("pcs", "ctn") else "pcs"})
    if not clean:
        raise HTTPException(400, "At least one column is required")
    if len(clean) > 10:
        raise HTTPException(400, "Maximum 10 frequent columns fit on the "
                                 "sheet")
    with get_db() as conn:
        conn.execute("UPDATE runsheet_settings SET value=? "
                     "WHERE key='columns'", (json.dumps(clean),))
        conn.commit()
    return {"ok": True, "count": len(clean)}


@router.get("/api/runsheet/invoices")
async def invoices_for_date(date: str):
    """Distinct invoices in the day book for the date, with customer
    area (route) from the customers master when available."""
    _ensure_schema()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT s.voucher,
                   MAX(COALESCE(s.base_link_doc,'')) AS s_order,
                   s.customer,
                   MAX(COALESCE(s.salesman,''))      AS salesman,
                   COUNT(*)                          AS lines,
                   ROUND(SUM(COALESCE(s.qty_ctn,0)), 1) AS ctns,
                   MAX(COALESCE(c.area,''))          AS area
            FROM sales_data s
            LEFT JOIN customers_master c
              ON LOWER(TRIM(c.name)) = LOWER(TRIM(s.customer))
            WHERE s.date = ?
            GROUP BY s.voucher, s.customer
            ORDER BY s.voucher
        """, (date,)).fetchall()
    return [{"voucher": r[0], "s_order": r[1], "customer": r[2],
             "salesman": r[3], "lines": r[4], "ctns": r[5],
             "area": r[6]} for r in rows]


@router.get("/api/runsheet/lookup")
async def lookup_invoice(invoice: str):
    """Find one invoice by its number anywhere in the day book (any
    date). Returns everything the builder needs to auto-fill a row:
    per-column pieces (using the current column mapping), other cartons,
    and the invoice's item lines (for all-round auto-fill). found=False
    when the day book doesn't have it — the builder then allows manual
    entry, so runsheets never depend on a sync having run."""
    _ensure_schema()
    q = str(invoice or "").strip()
    if not q:
        raise HTTPException(400, "invoice number required")
    with get_db() as conn:
        cols = _columns(conn)
        for cc in cols:
            if cc.get("item_name"):
                cc["qty"] = _qty_per_ctn(conn, cc["item_name"],
                                         cc.get("qty") or 1)
        rows = conn.execute("""
            SELECT voucher, MAX(COALESCE(base_link_doc,'')),
                   customer, MAX(COALESCE(salesman,'')), MAX(date)
            FROM sales_data
            WHERE voucher LIKE ?
            GROUP BY voucher
            ORDER BY MAX(date) DESC LIMIT 3
        """, (f"%{q}",)).fetchall()
        if not rows:
            rows = conn.execute("""
                SELECT voucher, MAX(COALESCE(base_link_doc,'')),
                       customer, MAX(COALESCE(salesman,'')), MAX(date)
                FROM sales_data
                WHERE voucher LIKE ?
                GROUP BY voucher
                ORDER BY MAX(date) DESC LIMIT 3
            """, (f"%{q}%",)).fetchall()
        if not rows:
            return {"found": False, "invoice": q, "cols": cols}
        voucher, so, cust, man, vdate = rows[0]
        lines = conn.execute("""
            SELECT item_name, SUM(COALESCE(qty_pieces,0)),
                   SUM(COALESCE(qty_ctn,0)), MAX(COALESCE(qty_per_ctn,0))
            FROM sales_data WHERE voucher = ?
            GROUP BY item_name
        """, (voucher,)).fetchall()
        col_names = {cc["item_name"].lower().strip(): j
                     for j, cc in enumerate(cols) if cc.get("item_name")}
        pcs = [0] * len(cols)
        items = []
        for (item, p, ctn, per) in lines:
            p = int(p or 0)
            key = (item or "").lower().strip()
            items.append({"item": item, "pcs": p,
                          "per": int(per or 0)})
            if key in col_names:
                pcs[col_names[key]] += p
        # NOTE: "Other ctn" is deliberately NOT computed — the warehouse
        # packs multiple products into shared cartons, so the true carton
        # count exists only at packing time and is entered manually.
        return {"found": True, "invoice": q,
                "voucher_raw": voucher,
                "inv": _clean_no(voucher), "so": _clean_no(so),
                "cust": cust, "by": man, "date": vdate,
                "pcs": pcs, "other": 0,
                "items": items, "cols": cols}


@router.get("/api/runsheet/run-items")
async def items_on_run(vouchers: str, date: str):
    """Items across the selected invoices (for choosing all-round
    products), with total pieces."""
    _ensure_schema()
    vlist = [v.strip() for v in vouchers.split(",") if v.strip()]
    if not vlist:
        return []
    ph = ",".join("?" * len(vlist))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT item_name, SUM(COALESCE(qty_pieces,0)) AS pcs,
                   MAX(COALESCE(qty_per_ctn,0)) AS per
            FROM sales_data
            WHERE date = ? AND voucher IN ({ph})
            GROUP BY item_name
            HAVING pcs > 0
            ORDER BY pcs DESC
        """, [date] + vlist).fetchall()
    return [{"item": r[0], "pcs": int(r[1]), "per": int(r[2] or 0)}
            for r in rows]


def _qty_per_ctn(conn, item_name: str, fallback: int = 1) -> int:
    row = conn.execute(
        "SELECT qty_per_ctn FROM products_master WHERE "
        "LOWER(TRIM(name)) = LOWER(TRIM(?))", (item_name,)).fetchone()
    try:
        v = int(row[0]) if row and row[0] else 0
    except (TypeError, ValueError):
        v = 0
    return v or fallback


def _clean_no(v: str) -> str:
    """Display form of voucher/order refs: 'SalInv : 486845' → '486845'."""
    import re
    m = re.search(r"(\d+)\s*$", str(v or ""))
    return m.group(1) if m else str(v or "").strip()


def _build_data(body: dict) -> dict:
    """Validate + normalise the builder's explicit state. Rows may come
    from day-book lookups OR be typed manually — the sheet builds either
    way (sync independence by design)."""
    rows_in = body.get("rows") or []
    ar_in = body.get("all_round") or []
    meta_in = body.get("meta") or {}
    if not rows_in:
        raise HTTPException(400, "Add at least one invoice row")
    if len(rows_in) > 20:
        raise HTTPException(400, "One page holds at most 20 invoices — "
                                 "split the run")

    with get_db() as conn:
        cols = _columns(conn)
        for cc in cols:
            if cc.get("item_name"):
                cc["qty"] = _qty_per_ctn(conn, cc["item_name"],
                                         cc.get("qty") or 1)

    def _i(v):
        try:
            return max(0, int(float(v)))
        except (TypeError, ValueError):
            return 0

    data_rows = []
    for r in rows_in:
        pcs = [_i(x) for x in (r.get("pcs") or [])][:len(cols)]
        pcs += [0] * (len(cols) - len(pcs))
        data_rows.append({
            "inv": _clean_no(r.get("inv")) or "—",
            "so": _clean_no(r.get("so")),
            "cust": str(r.get("cust") or "").strip()[:60] or "—",
            "by": str(r.get("by") or "").strip()[:14],
            "cash": 0, "chq": 0,
            "pcs": pcs,
            "ctn": _i(r.get("ctn") if r.get("ctn") is not None
                      else r.get("other")),
        })

    all_round = []
    for p in ar_in:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        by_inv = {}
        for k, v in (p.get("byInv") or {}).items():
            v = _i(v)
            if v and 0 <= int(k) < len(data_rows):
                by_inv[str(int(k))] = v
        if by_inv:
            unit = str(p.get("unit") or "pcs").lower()
            all_round.append({"name": name[:44],
                              "qty": max(1, _i(p.get("qty")) or 1),
                              "unit": unit if unit in ("pcs", "ctn")
                                      else "pcs",
                              "byInv": by_inv})

    meta = {
        "sheet_no": str(meta_in.get("sheet_no") or "").strip() or "—",
        "run_date": str(meta_in.get("run_date") or ""),
        "del_date": str(meta_in.get("del_date") or ""),
        "area": str(meta_in.get("area") or "").strip(),
        "del_man": str(meta_in.get("del_man") or "").strip(),
        "veh_no": str(meta_in.get("veh_no") or "").strip(),
    }
    return {"meta": meta, "cols": cols, "rows": data_rows,
            "all_round": all_round}


def _render(data: dict) -> str:
    template = (_THIS_DIR / "template.html").read_text(encoding="utf-8")
    return template.replace("/*__DATA__*/null",
                            json.dumps(data, ensure_ascii=False))


@router.get("/api/runsheet/template")
async def get_template():
    """The last-used setup (all-round products with units, run defaults)
    — auto-saved on every build so the clerk's sweet spot persists."""
    _ensure_schema()
    with get_db() as conn:
        row = conn.execute("SELECT value FROM runsheet_settings "
                           "WHERE key='last_template'").fetchone()
    return json.loads(row[0]) if row else {}


def _save_template(conn, data: dict):
    tpl = {
        "all_round": [{"name": p["name"], "qty": p["qty"],
                       "unit": p.get("unit", "pcs")}
                      for p in data.get("all_round", [])],
        "meta": {k: data["meta"].get(k, "")
                 for k in ("area", "del_man", "veh_no")},
        "saved_at": datetime.now().isoformat(),
    }
    conn.execute(
        "INSERT INTO runsheet_settings (key, value) VALUES "
        "('last_template', ?) ON CONFLICT(key) DO UPDATE SET "
        "value = excluded.value", (json.dumps(tpl, ensure_ascii=False),))


@router.post("/api/runsheet/build")
async def build_runsheet(body: dict):
    _ensure_schema()
    data = _build_data(body)
    html = _render(data)
    with get_db() as conn:
        _save_template(conn, data)
        cur = conn.execute(
            "INSERT INTO runsheets (sheet_no, run_date, area, del_date, "
            "del_man, veh_no, payload, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (data["meta"]["sheet_no"], data["meta"]["run_date"],
             data["meta"]["area"], data["meta"]["del_date"],
             data["meta"]["del_man"], data["meta"]["veh_no"],
             json.dumps(data, ensure_ascii=False),
             datetime.now().isoformat()))
        rid = cur.lastrowid
        conn.commit()
    return {"ok": True, "id": rid, "html": html,
            "invoices": len(data["rows"]),
            "all_round_products": len(data["all_round"])}


@router.get("/api/runsheet/history")
async def runsheet_history(limit: int = 30):
    _ensure_schema()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, sheet_no, run_date, area, del_man, veh_no, "
            "created_at FROM runsheets ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),)).fetchall()
    return [{"id": r[0], "sheet_no": r[1], "run_date": r[2], "area": r[3],
             "del_man": r[4], "veh_no": r[5], "created_at": r[6]}
            for r in rows]


@router.get("/api/runsheet/print/{rid}")
async def reprint(rid: int):
    _ensure_schema()
    with get_db() as conn:
        row = conn.execute("SELECT payload FROM runsheets WHERE id=?",
                           (rid,)).fetchone()
    if not row:
        raise HTTPException(404, "Runsheet not found")
    return HTMLResponse(_render(json.loads(row[0])))
