"""
sales_api.py — Sales Report agent, self-contained FastAPI router.

The dashboard (erp_dashboard/main.py) mounts this with a single guarded
include_router(). Everything the agent needs lives in this folder:
  - its own tables (created here, idempotently, on first import)
  - its own agent-card row + settings seed
  - all /api/sales/* endpoints
  - sales_module.py  (parsing / report / PDF)
  - sales_send.py    (WhatsApp delivery, shares focus_agent's sender)

Design rule: if anything here breaks, only /api/sales/* disappears —
the dashboard and other agents are unaffected.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

_THIS_DIR = Path(__file__).resolve().parent          # sales_agent/
_ROOT     = _THIS_DIR.parent                          # project root
sys.path.insert(0, str(_THIS_DIR))

import sales_module  # noqa: E402

# Dashboard helpers (late-bound; main is already imported when this loads)
from database import get_db  # erp_dashboard is the CWD of the server


def _dash():
    """Late import of the dashboard module for log() / settings access."""
    import main
    return main


router = APIRouter()


# ────────────────────────────────────────────────────────────────────
# Own schema + agent card (idempotent, runs at import)
# ────────────────────────────────────────────────────────────────────

_schema_ready = False

def _ensure_schema():
    """Idempotent; safe to call on every request. Own tables always created;
    the agent-card seed is attempted but tolerated to fail until the
    dashboard's init_db() has created the agents tables (fresh installs)."""
    global _schema_ready
    if _schema_ready:
        return
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sales_data (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                date          TEXT,
                voucher       TEXT,
                salesman      TEXT,
                customer      TEXT,
                item_name     TEXT,
                qty           REAL,
                unit          TEXT,
                qty_pieces    REAL,
                qty_ctn       REAL,
                rate          REAL,
                rate_pcs      REAL,
                gross         REAL,
                qty_per_ctn   REAL,
                base_link_doc TEXT,
                segment       TEXT,
                is_foc        INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sales_item_date
                ON sales_data(item_name, date);
            CREATE INDEX IF NOT EXISTS idx_sales_voucher_item
                ON sales_data(voucher, item_name);
            CREATE TABLE IF NOT EXISTS product_groups (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_items (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id  INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                UNIQUE(group_id, item_name)
            );
            CREATE TABLE IF NOT EXISTS salesmen_config (
                name        TEXT PRIMARY KEY,
                is_salesman INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS group_targets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    INTEGER NOT NULL,
                item_name   TEXT NOT NULL,
                salesman    TEXT,
                target_qty  REAL NOT NULL,
                target_unit TEXT DEFAULT 'CTN',
                UNIQUE(group_id, item_name, salesman)
            );
        """)
        conn.commit()
        # Agent card + settings so WhatsApp groups are editable in the UI.
        # Needs the dashboard's agents tables — on a brand-new install those
        # appear at server startup, so tolerate failure and retry next call.
        try:
            conn.execute("""
                INSERT OR IGNORE INTO agents
                    (name, display_name, description, interval_min, enabled)
                VALUES ('sales_report', 'Sales Report Agent',
                        'Salesman-wise product sales reports with targets, from uploaded sales day books.',
                        0, 0)
            """)
            conn.execute("""
                INSERT OR IGNORE INTO agent_settings
                    (agent, key, value, description, category)
                VALUES ('sales_report', 'whatsapp_groups', '[]',
                        'WhatsApp groups (JSON list)', 'whatsapp')
            """)
            conn.commit()
            _schema_ready = True
        except Exception:
            pass  # retried on the next request


try:
    _ensure_schema()
except Exception:
    pass  # DB not ready yet — endpoints ensure it lazily


async def _log(msg: str):
    try:
        await _dash().log("sales_report", msg)
    except Exception:
        print(f"[sales_report] {msg}")


# ────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────

@router.post("/api/sales/upload")
async def sales_upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Please upload an Excel file (.xlsx or .xls)")
    raw = await file.read()
    try:
        rows, stats = sales_module.parse_sales_excel(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with get_db() as conn:
        result = sales_module.upsert_sales(conn, rows)
    _invalidate_rows_cache()
    await _log(f"📥 Sales data uploaded: {stats['rows']} rows "
               f"({stats['date_from']} → {stats['date_to']}), "
               f"{result['replaced']} replaced")
    return {**stats, **result}


@router.get("/api/sales/summary")
async def sales_summary():
    _ensure_schema()
    with get_db() as conn:
        row = conn.execute("""
            SELECT COUNT(*), MIN(date), MAX(date),
                   COUNT(DISTINCT item_name), COUNT(DISTINCT salesman)
            FROM sales_data
        """).fetchone()
    return {"rows": row[0], "date_from": row[1], "date_to": row[2],
            "items": row[3], "salesmen": row[4]}


_BROWSE_COLS = ["date", "voucher", "salesman", "customer", "item_name",
                "qty", "unit", "qty_pieces", "qty_ctn", "rate", "rate_pcs",
                "gross", "segment", "is_foc"]
_FILTER_COLS = {"date", "voucher", "salesman", "customer", "item_name",
                "unit", "segment"}
# One concatenated blob so all-column search is a single test per row
_BLOB = " || ' ' || ".join(f"IFNULL({f},'')" for f in sorted(_FILTER_COLS))
_COUNT_CAP = 5000
_total_cache = {"n": None}          # unfiltered COUNT(*), reset on upload


def _invalidate_rows_cache():
    _total_cache["n"] = None


@router.get("/api/sales/rows")
async def sales_rows(q: str = "", col: str = "all", regex: int = 0,
                     limit: int = 100, offset: int = 0):
    """Browse raw sales rows. Plain search = LIKE (single concatenated
    expression for all-columns mode); regex mode = REGEXP via a registered
    Python function. Counts are capped at 5000 to keep worst cases fast."""
    _ensure_schema()
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    col = col if col in _FILTER_COLS else "all"
    q = (q or "").strip()

    cols_sql = ", ".join(_BROWSE_COLS)
    target = _BLOB if col == "all" else col
    where, params = "", []

    with get_db() as conn:
        if q:
            if regex:
                import re as _re
                try:
                    pat = _re.compile(q, _re.IGNORECASE)
                except _re.error as e:
                    raise HTTPException(400, f"Invalid regex: {e}")
                conn.create_function(
                    "REGEXP", 2,
                    lambda expr, item: 1 if item is not None
                    and pat.search(str(item)) else 0)
                where, params = f"WHERE ({target}) REGEXP ?", [q]
            else:
                where, params = f"WHERE ({target}) LIKE ?", [f"%{q}%"]

            capped = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM sales_data {where} "
                f"LIMIT {_COUNT_CAP + 1})", params).fetchone()[0]
            total, total_capped = capped, capped > _COUNT_CAP
        else:
            if _total_cache["n"] is None:
                _total_cache["n"] = conn.execute(
                    "SELECT COUNT(*) FROM sales_data").fetchone()[0]
            total, total_capped = _total_cache["n"], False

        rows = conn.execute(
            f"SELECT {cols_sql} FROM sales_data {where} "
            f"ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()

    return {"total": total, "total_capped": total_capped,
            "offset": offset, "limit": limit,
            "columns": _BROWSE_COLS,
            "rows": [list(r) for r in rows]}


@router.get("/api/sales/items")
async def sales_items(q: str = ""):
    _ensure_schema()
    """Item picker: pricebook items UNION items seen in sales data."""
    like = f"%{q}%"
    with get_db() as conn:
        rows = conn.execute("""
            SELECT item_name FROM pricebook WHERE item_name LIKE ?
            UNION
            SELECT DISTINCT item_name FROM sales_data WHERE item_name LIKE ?
            ORDER BY item_name LIMIT 40
        """, (like, like)).fetchall()
    return [r[0] for r in rows]


@router.get("/api/sales/salesmen")
async def sales_salesmen():
    _ensure_schema()
    with get_db() as conn:
        data_names = [r[0] for r in conn.execute(
            "SELECT DISTINCT salesman FROM sales_data ORDER BY salesman"
        ).fetchall()]
        cfg = {r[0]: r[1] for r in conn.execute(
            "SELECT name, is_salesman FROM salesmen_config").fetchall()}
    configured = len(cfg) > 0
    out = []
    for n in sorted(set(data_names) | set(cfg.keys())):
        if configured:
            is_s = bool(cfg.get(n, 0))       # unknown names default to Direct
            known = n in cfg
        else:
            is_s = True                       # no config yet: everyone counts
            known = False
        out.append({"name": n, "is_salesman": is_s, "configured": known})
    return {"configured": configured, "salesmen": out}


@router.post("/api/sales/salesmen")
async def sales_salesmen_save(body: dict):
    _ensure_schema()
    names = body.get("salesmen") or []      # the names ticked as real salesmen
    if not isinstance(names, list):
        raise HTTPException(400, "salesmen must be a list of names")
    picked = {str(n).strip() for n in names if str(n).strip()}
    with get_db() as conn:
        data_names = {r[0] for r in conn.execute(
            "SELECT DISTINCT salesman FROM sales_data").fetchall()}
        conn.execute("DELETE FROM salesmen_config")
        for n in sorted(data_names | picked):
            conn.execute(
                "INSERT INTO salesmen_config(name, is_salesman) VALUES (?,?)",
                (n, 1 if n in picked else 0))
        conn.commit()
    await _log(f"👥 Salesmen configured: {len(picked)} salesmen, "
               f"{len(data_names - picked)} direct-sales names")
    return {"ok": True, "salesmen": len(picked)}


@router.get("/api/sales/groups")
async def sales_groups():
    _ensure_schema()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT g.id, g.name,
                   (SELECT COUNT(*) FROM group_items i WHERE i.group_id=g.id),
                   (SELECT COUNT(*) FROM group_targets t WHERE t.group_id=g.id)
            FROM product_groups g ORDER BY g.name
        """).fetchall()
    return [{"id": r[0], "name": r[1], "items": r[2], "targets": r[3]}
            for r in rows]


@router.post("/api/sales/groups")
async def sales_group_create(body: dict):
    _ensure_schema()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Group name required")
    with get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO product_groups(name) VALUES (?)", (name,))
            conn.commit()
        except Exception:
            raise HTTPException(400, "A group with that name already exists")
        return {"id": cur.lastrowid, "name": name}


@router.delete("/api/sales/groups/{gid}")
async def sales_group_delete(gid: int):
    _ensure_schema()
    with get_db() as conn:
        conn.execute("DELETE FROM group_targets WHERE group_id=?", (gid,))
        conn.execute("DELETE FROM group_items WHERE group_id=?", (gid,))
        conn.execute("DELETE FROM product_groups WHERE id=?", (gid,))
        conn.commit()
    return {"ok": True}


@router.get("/api/sales/groups/{gid}")
async def sales_group_detail(gid: int):
    _ensure_schema()
    with get_db() as conn:
        g = conn.execute(
            "SELECT id, name FROM product_groups WHERE id=?", (gid,)).fetchone()
        if not g:
            raise HTTPException(404, "Group not found")
        items = [r[0] for r in conn.execute(
            "SELECT item_name FROM group_items WHERE group_id=? ORDER BY item_name",
            (gid,)).fetchall()]
        targets = [{"id": r[0], "item_name": r[1], "salesman": r[2],
                    "target_qty": r[3], "target_unit": r[4]}
                   for r in conn.execute(
            "SELECT id, item_name, salesman, target_qty, target_unit "
            "FROM group_targets WHERE group_id=? ORDER BY item_name, salesman",
            (gid,)).fetchall()]
    return {"id": g[0], "name": g[1], "items": items, "targets": targets}


@router.post("/api/sales/groups/{gid}/items")
async def sales_group_add_items(gid: int, body: dict):
    _ensure_schema()
    items = body.get("items") or []
    if isinstance(items, str):
        items = [items]
    added = 0
    with get_db() as conn:
        for it in items:
            it = (it or "").strip()
            if not it:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO group_items(group_id, item_name) "
                "VALUES (?,?)", (gid, it))
            added += 1
        conn.commit()
    return {"added": added}


@router.post("/api/sales/groups/{gid}/items/upload")
async def sales_group_upload_items(gid: int, file: UploadFile = File(...)):
    _ensure_schema()
    if not file.filename.lower().endswith(('.csv', '.xlsx', '.xls')):
        raise HTTPException(400, "Upload a .csv or Excel file")
    raw = await file.read()
    try:
        names = sales_module.parse_product_list(raw, file.filename)
    except Exception as e:
        raise HTTPException(400, f"Could not read the file: {e}")
    if not names:
        raise HTTPException(400, "No product names found in the file")

    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM product_groups WHERE id=?",
                            (gid,)).fetchone():
            raise HTTPException(404, "Group not found")
        existing = {r[0].lower() for r in conn.execute(
            "SELECT item_name FROM group_items WHERE group_id=?",
            (gid,)).fetchall()}
        added = 0
        for n in names:
            if n.lower() in existing:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO group_items(group_id, item_name) "
                "VALUES (?,?)", (gid, n))
            added += 1
        conn.commit()

        # Advisory: names not present in pricebook or sales data
        known = {r[0].strip().lower() for r in conn.execute(
            "SELECT item_name FROM pricebook").fetchall()}
        known |= {r[0].strip().lower() for r in conn.execute(
            "SELECT DISTINCT item_name FROM sales_data").fetchall()}
        unknown = [n for n in names if n.strip().lower() not in known]

    await _log(f"📥 Product list uploaded to group {gid}: "
               f"{added} added, {len(names)-added} already present, "
               f"{len(unknown)} unknown")
    return {"total_in_file": len(names), "added": added,
            "already_present": len(names) - added,
            "unknown": unknown[:10], "unknown_count": len(unknown)}


@router.delete("/api/sales/groups/{gid}/items")
async def sales_group_del_item(gid: int, name: str):
    _ensure_schema()
    with get_db() as conn:
        conn.execute(
            "DELETE FROM group_items WHERE group_id=? AND item_name=?",
            (gid, name))
        conn.execute(
            "DELETE FROM group_targets WHERE group_id=? AND item_name=?",
            (gid, name))
        conn.commit()
    return {"ok": True}


@router.post("/api/sales/groups/{gid}/targets")
async def sales_group_add_target(gid: int, body: dict):
    _ensure_schema()
    item = (body.get("item_name") or "").strip()
    sman = (body.get("salesman") or "").strip() or None
    qty = float(body.get("target_qty") or 0)
    unit = (body.get("target_unit") or "CTN").upper()
    if not item or qty <= 0:
        raise HTTPException(400, "item_name and positive target_qty required")
    if unit not in ("CTN", "PCS"):
        raise HTTPException(400, "target_unit must be CTN or PCS")
    with get_db() as conn:
        conn.execute("""
            INSERT INTO group_targets(group_id, item_name, salesman,
                                      target_qty, target_unit)
            VALUES (?,?,?,?,?)
            ON CONFLICT(group_id, item_name, salesman)
            DO UPDATE SET target_qty=excluded.target_qty,
                          target_unit=excluded.target_unit
        """, (gid, item, sman, qty, unit))
        conn.commit()
    return {"ok": True}


@router.delete("/api/sales/targets/{tid}")
async def sales_target_delete(tid: int):
    _ensure_schema()
    with get_db() as conn:
        conn.execute("DELETE FROM group_targets WHERE id=?", (tid,))
        conn.commit()
    return {"ok": True}


def _report_or_400(body: dict) -> dict:
    gid = int(body.get("group_id") or 0)
    dfrom = (body.get("date_from") or "").strip()
    dto = (body.get("date_to") or "").strip()
    if not gid or not dfrom or not dto:
        raise HTTPException(400, "group_id, date_from, date_to required")
    with get_db() as conn:
        try:
            return sales_module.build_report(conn, gid, dfrom, dto)
        except ValueError as e:
            raise HTTPException(400, str(e))


@router.post("/api/sales/report")
async def sales_report(body: dict):
    _ensure_schema()
    return _report_or_400(body)


@router.post("/api/sales/report/pdf")
async def sales_report_pdf(body: dict):
    _ensure_schema()
    rep = _report_or_400(body)
    out_dir = _THIS_DIR / "downloads"
    out_dir.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in rep["group"])[:30]
    out = out_dir / (f"SalesReport_{safe}_{rep['date_from']}"
                     f"_to_{rep['date_to']}.pdf")
    sales_module.report_pdf(rep, str(out))
    await _log(f"📄 PDF generated: {out.name}")
    return FileResponse(str(out), filename=out.name,
                        media_type="application/pdf")


@router.post("/api/sales/report/whatsapp")
async def sales_report_whatsapp(body: dict):
    _ensure_schema()
    rep = _report_or_400(body)
    try:
        wa_groups = _dash().get_agent_setting("sales_report",
                                              "whatsapp_groups") or "[]"
    except Exception:
        wa_groups = "[]"

    tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     dir=str(_THIS_DIR), encoding="utf-8")
    json.dump(rep, tf)
    tf.close()
    await _log(f"📲 Sending report to WhatsApp: {rep['group']} "
               f"{rep['date_from']}→{rep['date_to']}")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "sales_send.py",
        cwd=str(_THIS_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ,
             "SALES_REPORT_JSON": tf.name,
             "SALES_WA_GROUPS": wa_groups,
             "PYTHONIOENCODING": "utf-8"})
    out, _ = await proc.communicate()
    for line in (out or b"").decode("utf-8", "replace").splitlines():
        if line.strip():
            await _log(line.rstrip())
    try:
        os.unlink(tf.name)
    except Exception:
        pass
    ok = proc.returncode == 0
    await _log("✅ WhatsApp send finished" if ok
               else f"❌ WhatsApp send failed (exit {proc.returncode})")
    return {"ok": ok}
