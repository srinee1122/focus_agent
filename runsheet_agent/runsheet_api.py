"""
runsheet_api.py — Runsheet agent, self-contained FastAPI router.

Builds one-page driver runsheets from the (synced) sales day book:
  GET  /api/runsheet/invoices?date=       invoices available for a date
  GET  /api/runsheet/columns              frequent round-item column config
  PUT  /api/runsheet/columns              save column config
  GET  /api/runsheet/run-items?vouchers=  items on the selected invoices
  POST /api/runsheet/from-photo           extract annotated SO photos (AI)
  POST /api/runsheet/build                compute + render printable HTML
  GET  /api/runsheet/history              saved runsheets
  GET  /api/runsheet/print/{id}           re-render a saved runsheet

Isolated by design (guarded include in main.py): failures here only
remove /api/runsheet/* routes.
"""
from __future__ import annotations
import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
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
    m = re.search(r"(\d+)\s*$", str(v or ""))
    return m.group(1) if m else str(v or "").strip()


# ── Photo extraction (annotated Sales Order → structured row) ────────
# One Anthropic API call per photo; multi-page orders merged by so_no.
# The prompt lives in photo_extraction_prompt.md and is sent verbatim.
# Nothing here auto-commits — the frontend review screen is mandatory.

FIREBASE_PROJECT_ID = "sriambikasagents"
_API_KEYS_URL = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
    f"/databases/(default)/documents/app_config/api_keys"
)
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_PHOTO_MODEL = "claude-sonnet-4-6"
_PHOTO_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_photo_prompt: str | None = None


def _photo_system_prompt() -> str:
    """The extraction prompt, read verbatim from the spec file (the text
    between the '## System prompt' heading and the next '---' rule)."""
    global _photo_prompt
    if _photo_prompt:
        return _photo_prompt
    text = (_THIS_DIR / "photo_extraction_prompt.md").read_text(
        encoding="utf-8")
    m = re.search(r"^## System prompt[^\n]*\n(.*?)\n---", text,
                  re.S | re.M)
    if not m:
        raise HTTPException(500, "photo_extraction_prompt.md is missing "
                                 "its '## System prompt' section")
    _photo_prompt = m.group(1).strip()
    return _photo_prompt


def _anthropic_key(request: Request) -> str:
    """Anthropic API key from Firestore app_config/api_keys, read with
    the caller's Firebase ID token (same access model as the ERP
    credentials in focus_agent/credentials.py)."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = os.environ.get("AGENT_AUTH_TOKEN", "").strip()
    if not token:
        raise HTTPException(401, "Not authenticated")
    req = urllib.request.Request(
        _API_KEYS_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise HTTPException(403, "Access denied reading the API key "
                                     "from Firestore")
        if e.code == 404:
            raise HTTPException(500, "Firestore document app_config/"
                                     "api_keys not found — create it "
                                     "with an anthropic_api_key field")
        raise HTTPException(502, f"Firestore error {e.code}")
    except Exception as e:
        raise HTTPException(502, f"Could not reach Firestore: {e}")
    key = (data.get("fields", {}).get("anthropic_api_key", {})
               .get("stringValue", "").strip())
    if not key:
        raise HTTPException(500, "app_config/api_keys has no "
                                 "anthropic_api_key field")
    return key


def _extract_one_photo(api_key: str, media_type: str, b64: str) -> dict:
    """One photo → the strict-JSON extraction dict."""
    payload = {
        "model": _PHOTO_MODEL,
        "max_tokens": 3000,
        "system": _photo_system_prompt(),
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image",
                "source": {"type": "base64",
                           "media_type": media_type,
                           "data": b64},
            }],
        }],
    }
    req = urllib.request.Request(
        _ANTHROPIC_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode())["error"]["message"]
        except Exception:
            msg = e.reason
        raise ValueError(f"Anthropic API error {e.code}: {msg}")
    except Exception as e:
        raise ValueError(f"Could not reach the Anthropic API: {e}")
    if out.get("stop_reason") == "refusal":
        raise ValueError("The model declined to read this photo")
    text = "".join(b.get("text", "") for b in out.get("content", [])
                   if b.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Model reply was not JSON")
    try:
        page = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"Model reply was not valid JSON: {e}")
    if not isinstance(page, dict):
        raise ValueError("Model reply was not a JSON object")
    return page


def _merge_photo_pages(pages: list) -> list:
    """Merge pages of the same SO: union round_items, box from whichever
    page has it, concatenate notes/uncertain (spec: post-extraction
    mapping)."""
    by_so, order = {}, []
    for idx, p in enumerate(pages):
        so = _clean_no(p.get("so_no"))
        key = so or f"__nopage{idx}"
        if key not in by_so:
            o = {
                "so_no": so,
                "so_date": str(p.get("so_date") or ""),
                "customer": str(p.get("customer") or ""),
                "area": str(p.get("area") or ""),
                "salesman": str(p.get("salesman") or ""),
                "pages": [p.get("page")],
                "box": p.get("box") if isinstance(p.get("box"), dict)
                       else None,
                "round_items": list(p.get("round_items") or []),
                "pallet_no": str(p.get("pallet_no") or ""),
                "notes": list(p.get("notes") or []),
                "uncertain": list(p.get("uncertain") or []),
            }
            by_so[key] = o
            order.append(key)
            continue
        o = by_so[key]
        o["pages"].append(p.get("page"))
        seen = {(ri.get("serial"), _norm_name(ri.get("item")))
                for ri in o["round_items"]}
        for ri in p.get("round_items") or []:
            k = (ri.get("serial"), _norm_name(ri.get("item")))
            if k not in seen:
                o["round_items"].append(ri)
                seen.add(k)
        if not o["box"] and isinstance(p.get("box"), dict):
            o["box"] = p["box"]
        o["notes"] += list(p.get("notes") or [])
        o["uncertain"] += list(p.get("uncertain") or [])
        for f in ("so_date", "customer", "area", "salesman", "pallet_no"):
            if not o[f]:
                o[f] = str(p.get(f) or "")
    return [by_so[k] for k in order]


def _norm_name(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _pack_per_from_name(name: str) -> int:
    """'OOTY GOLD ... - 5KG X 6' → 6 (spec fallback when the items
    master has no qty_per_ctn)."""
    m = re.search(r"[x×]\s*(\d+)\s*$", str(name or "").strip(),
                  re.IGNORECASE)
    try:
        return int(m.group(1)) if m else 0
    except ValueError:
        return 0


def _match_so(conn, so: str) -> dict:
    """Day-book cross-check: SO number → invoice via base_link_doc.
    Not found → the row stays manual (amber); photo data stands alone."""
    so = _clean_no(so)
    if not so:
        return {"found": False}
    rows = conn.execute("""
        SELECT voucher, MAX(COALESCE(base_link_doc,'')),
               customer, MAX(COALESCE(salesman,'')), MAX(date)
        FROM sales_data
        WHERE base_link_doc LIKE ?
        GROUP BY voucher
        ORDER BY MAX(date) DESC LIMIT 5
    """, (f"%{so}",)).fetchall()
    rows = [r for r in rows if _clean_no(r[1]) == so]
    if not rows:
        return {"found": False, "so": so}
    voucher, blink, cust, man, vdate = rows[0]
    lines = conn.execute("""
        SELECT item_name, SUM(COALESCE(qty_pieces,0)),
               MAX(COALESCE(qty_per_ctn,0))
        FROM sales_data WHERE voucher = ?
        GROUP BY item_name
    """, (voucher,)).fetchall()
    return {"found": True, "inv": _clean_no(voucher),
            "voucher_raw": voucher, "so": so, "cust": cust,
            "by": man, "date": vdate,
            "items": [{"item": r[0], "pcs": int(r[1] or 0),
                       "per": int(r[2] or 0)} for r in lines]}


@router.post("/api/runsheet/from-photo")
def runsheet_from_photo(body: dict, request: Request):
    """Extract one or more annotated-SO photos into review-ready order
    data. Body: {images: [{data: <base64>, media_type: "image/jpeg"}]}.
    Returns orders (merged by so_no) with day-book match + round items
    mapped to the frequent columns. The caller MUST show a review screen
    — this endpoint never writes anything."""
    _ensure_schema()
    images = body.get("images") or []
    if not images:
        raise HTTPException(400, "No images supplied")
    if len(images) > 6:
        raise HTTPException(400, "Max 6 photos per upload")
    api_key = _anthropic_key(request)

    pages, errors = [], []
    for i, im in enumerate(images):
        mt = str((im or {}).get("media_type") or "").lower()
        b64 = str((im or {}).get("data") or "")
        if mt not in _PHOTO_MEDIA_TYPES or not b64:
            errors.append(f"Photo {i + 1}: unsupported image type")
            continue
        try:
            pages.append(_extract_one_photo(api_key, mt, b64))
        except ValueError as e:
            errors.append(f"Photo {i + 1}: {e}")
    if not pages:
        raise HTTPException(502, "; ".join(errors) or "Extraction failed")

    orders = _merge_photo_pages(pages)
    with get_db() as conn:
        cols = _columns(conn)
        for cc in cols:
            if cc.get("item_name"):
                cc["qty"] = _qty_per_ctn(conn, cc["item_name"],
                                         cc.get("qty") or 1)
        col_by_name = {_norm_name(cc["item_name"]): j
                       for j, cc in enumerate(cols)
                       if cc.get("item_name")}
        for o in orders:
            o["match"] = _match_so(conn, o["so_no"])
            for ri in o["round_items"]:
                item = str(ri.get("item") or "")
                try:
                    qty = max(0, int(float(ri.get("qty") or 0)))
                except (TypeError, ValueError):
                    qty = 0
                per = (_qty_per_ctn(conn, item, 0)
                       or _pack_per_from_name(item) or 1)
                uom = str(ri.get("uom") or "").strip().upper()
                ri["per"] = per
                ri["pcs"] = qty * per if uom.startswith("CTN") else qty
                j = col_by_name.get(_norm_name(item))
                ri["col"] = j if j is not None else None
                ri["struck"] = bool(ri.get("struck"))
    return {"ok": True, "orders": orders, "cols": cols, "errors": errors}


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
        "notes": str(meta_in.get("notes") or "").strip()[:400],
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
