"""
main.py — ERP Agent Dashboard backend (FastAPI)

Run:
    python -m uvicorn main:app --reload --port 8000

Then open: http://localhost:8000
"""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sys, io, threading, concurrent.futures
from database import get_db, init_db

# Thread pool — each agent runs in its own thread with its own event loop
# This is needed on Windows where uvicorn uses SelectorEventLoop which
# does not support subprocess creation (required by Playwright)
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# ── Stdout → Dashboard log bridge ─────────────────────────────────────────
_log_queues: dict[str, list] = {}

class LogBridge(io.TextIOBase):
    """Captures print() output and queues it for the dashboard log stream."""
    def __init__(self, agent: str, original):
        self.agent    = agent
        self.original = original
        self._buf     = ""
    def write(self, text):
        self.original.write(text)   # still show in terminal
        self.original.flush()
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                _log_queues.setdefault(self.agent, []).append(line.strip())
        return len(text)
    def flush(self):
        self.original.flush()

async def drain_log_queue(agent: str):
    """Flush any queued print() messages to the WebSocket log stream."""
    msgs = _log_queues.pop(agent, [])
    for msg in msgs:
        await log(agent, msg)

# Try standard subfolder first, fall back to sibling or same level
_base = Path(__file__).parent.parent
_candidates = [
    _base / "focus_agent",          # focus_agent_complete/focus_agent/
    Path(__file__).parent.parent,   # if erp_dashboard & focus_agent are siblings at same level
    Path(__file__).parent.parent / "focus_agent_complete" / "focus_agent",
]
AGENT_DIR = next((p for p in _candidates if (p / "main.py").exists()), _candidates[0])
print(f"[Dashboard] AGENT_DIR resolved to: {AGENT_DIR}")

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Reset any stuck "running" statuses from previous server session
    with get_db() as db:
        db.execute("UPDATE agents SET status='idle' WHERE status='running'")
    asyncio.create_task(scheduler_loop())
    yield

app = FastAPI(title="ERP Agent Dashboard", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── WebSocket manager ──────────────────────────────────────────────────────
class ConnMgr:
    def __init__(self):
        self._sockets: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._sockets.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._sockets:
            self._sockets.remove(ws)

    async def broadcast(self, payload: dict):
        dead = []
        for ws in self._sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._sockets.remove(ws)

mgr = ConnMgr()
running_tasks: dict[str, asyncio.Task] = {}


# ── Helpers ────────────────────────────────────────────────────────────────
async def log(agent: str, message: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    with get_db() as db:
        db.execute("INSERT INTO logs (agent, level, message) VALUES (?, ?, ?)",
                   (agent, level, message))
    await mgr.broadcast({"type": "log", "agent": agent,
                          "level": level, "message": message, "ts": ts})


def set_status(name: str, status: str, last_status: str = None, loop=None):
    with get_db() as db:
        if last_status:
            db.execute(
                "UPDATE agents SET status=?, last_status=?, last_run=? WHERE name=?",
                (status, last_status, datetime.now().isoformat(), name))
        else:
            db.execute("UPDATE agents SET status=? WHERE name=?", (status, name))
    payload = {"type": "status", "agent": name, "status": status}
    try:
        # Try to get running loop (works when called from async context)
        running = asyncio.get_running_loop()
        running.create_task(mgr.broadcast(payload))
    except RuntimeError:
        # Called from a thread — use run_coroutine_threadsafe if loop provided
        if loop:
            asyncio.run_coroutine_threadsafe(mgr.broadcast(payload), loop)


def get_agent_setting(agent: str, key: str, default: str = "") -> str:
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM agent_settings WHERE agent=? AND key=?",
            (agent, key)).fetchone()
    return row["value"] if row else default


# ── Agent runners ──────────────────────────────────────────────────────────
async def run_low_price_agent():
    """Run the Low Price Agent as a subprocess using threading (Windows-safe)."""
    set_status("low_price", "running")
    run_start = datetime.now().strftime("%d %b %Y  %I:%M:%S %p")
    await log("low_price", f"{'━'*50}")
    await log("low_price", f"▶  RUN STARTED  —  {run_start}")
    await log("low_price", f"{'━'*50}")

    main_loop = asyncio.get_running_loop()

    def send(msg, level="info"):
        asyncio.run_coroutine_threadsafe(log("low_price", msg, level), main_loop)

    def agent_thread():
        import subprocess, os
        # Read toggle settings and pass to agent via env vars
        send_text      = get_agent_setting("low_price", "send_text",      "true")
        send_image     = get_agent_setting("low_price", "send_image",     "true")
        whatsapp_groups= get_agent_setting("low_price", "whatsapp_groups", "")
        # Enforce at least one on
        if send_text.lower() != "true" and send_image.lower() != "true":
            send_text = "true"

        env = {
            **os.environ,
            "PYTHONIOENCODING":         "utf-8",
            "PYTHONLEGACYWINDOWSSTDIO": "0",
            "AGENT_SEND_TEXT":          send_text,
            "AGENT_SEND_IMAGE":         send_image,
            "AGENT_WA_GROUPS":          whatsapp_groups,
        }
        try:
            proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", "main.py", "--now"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(AGENT_DIR),
                env=env
            )
            for raw in iter(proc.stdout.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    send(text)
            proc.wait()
            if proc.returncode == 0:
                send("✅ Run complete.")
                set_status("low_price", "idle", "success", loop=main_loop)
            else:
                send(f"❌ Agent exited with code {proc.returncode}", "error")
                set_status("low_price", "error", "error", loop=main_loop)
        except Exception as e:
            import traceback
            send(f"❌ {e}", "error")
            send(traceback.format_exc(), "error")
            set_status("low_price", "error", "error", loop=main_loop)

    await main_loop.run_in_executor(_executor, agent_thread)


async def run_data_sync_agent():
    set_status("data_sync", "running")
    await log("data_sync", "▶ Data Sync Agent — not yet implemented.")
    await asyncio.sleep(2)
    set_status("data_sync", "idle", "success")
    await log("data_sync", "✅ Placeholder complete.")


RUNNERS = {
    "low_price": run_low_price_agent,
    "data_sync":  run_data_sync_agent,
}


async def trigger(name: str) -> bool:
    if name in running_tasks and not running_tasks[name].done():
        return False
    runner = RUNNERS.get(name)
    if not runner:
        return False
    running_tasks[name] = asyncio.create_task(runner())
    return True


# ── Scheduler ──────────────────────────────────────────────────────────────
async def scheduler_loop():
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        with get_db() as db:
            rows = db.execute("SELECT * FROM agents WHERE enabled=1").fetchall()
        for row in rows:
            name = row["name"]
            if name in running_tasks and not running_tasks[name].done():
                continue
            interval = row["interval_min"] or 60
            last = row["last_run"]
            if last:
                due = datetime.fromisoformat(last) + timedelta(minutes=interval)
                if now >= due:
                    asyncio.create_task(trigger(name))
            else:
                asyncio.create_task(trigger(name))


# ── REST: Agents ───────────────────────────────────────────────────────────
@app.get("/api/agents")
def get_agents():
    with get_db() as db:
        rows = db.execute("SELECT * FROM agents").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/agents/{name}/run")
async def run_agent(name: str):
    if not await trigger(name):
        raise HTTPException(400, "Agent already running or not found")
    return {"ok": True}


@app.post("/api/agents/{name}/stop")
async def stop_agent(name: str):
    task = running_tasks.get(name)
    if task and not task.done():
        task.cancel()
        set_status(name, "idle")
        await log(name, "⏹ Stopped by user.", "warn")
    return {"ok": True}


class AgentUpdate(BaseModel):
    enabled: Optional[bool] = None
    interval_min: Optional[int] = None


@app.patch("/api/agents/{name}")
def update_agent(name: str, body: AgentUpdate):
    with get_db() as db:
        if body.enabled is not None:
            db.execute("UPDATE agents SET enabled=? WHERE name=?",
                       (1 if body.enabled else 0, name))
        if body.interval_min is not None:
            db.execute("UPDATE agents SET interval_min=? WHERE name=?",
                       (body.interval_min, name))
    return {"ok": True}


# ── REST: Per-agent settings ───────────────────────────────────────────────
@app.get("/api/agents/{name}/settings")
def get_agent_settings(name: str):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM agent_settings WHERE agent=? ORDER BY category, key",
            (name,)).fetchall()
    return [dict(r) for r in rows]


class SettingUpdate(BaseModel):
    value: str


@app.put("/api/agents/{name}/settings/{key}")
def update_agent_setting(name: str, key: str, body: SettingUpdate):
    with get_db() as db:
        db.execute(
            "UPDATE agent_settings SET value=? WHERE agent=? AND key=?",
            (body.value, name, key))
    return {"ok": True}


# ── REST: Logs ─────────────────────────────────────────────────────────────
@app.get("/api/logs")
def get_logs(agent: str = None, limit: int = 200):
    with get_db() as db:
        if agent:
            rows = db.execute(
                "SELECT * FROM logs WHERE agent=? ORDER BY id DESC LIMIT ?",
                (agent, limit)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


@app.delete("/api/logs")
def clear_logs(agent: str = None):
    with get_db() as db:
        if agent:
            db.execute("DELETE FROM logs WHERE agent=?", (agent,))
        else:
            db.execute("DELETE FROM logs")
    return {"ok": True}


# ── REST: Purchase types ──────────────────────────────────────────────────
@app.get("/api/purchase-types")
def get_purchase_types():
    with get_db() as db:
        rows = db.execute("SELECT * FROM purchase_types ORDER BY is_default DESC, name").fetchall()
    return [dict(r) for r in rows]


class PurchaseTypeEntry(BaseModel):
    name:             str
    purchase_type:    str   = "standard_import"
    landing_pct:      float = 5.0
    repacking_cost:   float = 0.0
    stockpile_charge: float = 0.0
    is_default:       bool  = False
    notes:            Optional[str] = ""


@app.post("/api/purchase-types")
def add_purchase_type(entry: PurchaseTypeEntry):
    with get_db() as db:
        if entry.is_default:
            db.execute("UPDATE purchase_types SET is_default=0")
        db.execute("""
            INSERT INTO purchase_types
                (name, purchase_type, landing_pct, repacking_cost, stockpile_charge, is_default, notes)
            VALUES (?,?,?,?,?,?,?)""",
            (entry.name, entry.purchase_type, entry.landing_pct,
             entry.repacking_cost, entry.stockpile_charge,
             1 if entry.is_default else 0, entry.notes))
    return {"ok": True}


@app.put("/api/purchase-types/{id}")
def update_purchase_type(id: int, entry: PurchaseTypeEntry):
    with get_db() as db:
        if entry.is_default:
            db.execute("UPDATE purchase_types SET is_default=0")
        db.execute("""
            UPDATE purchase_types SET
                name=?, purchase_type=?, landing_pct=?, repacking_cost=?,
                stockpile_charge=?, is_default=?, notes=?
            WHERE id=?""",
            (entry.name, entry.purchase_type, entry.landing_pct,
             entry.repacking_cost, entry.stockpile_charge,
             1 if entry.is_default else 0, entry.notes, id))
    return {"ok": True}


@app.delete("/api/purchase-types/{id}")
def delete_purchase_type(id: int):
    with get_db() as db:
        pt = db.execute("SELECT * FROM purchase_types WHERE id=?", (id,)).fetchone()
        if not pt:
            raise HTTPException(404, "Purchase type not found")
        if pt["is_default"]:
            raise HTTPException(400, "Cannot delete the default purchase type")
        count = db.execute(
            "SELECT COUNT(*) FROM pricebook WHERE purchase_type_id=?", (id,)).fetchone()[0]
        if count > 0:
            raise HTTPException(400, f"Cannot delete — {count} product(s) using this purchase type")
        db.execute("DELETE FROM purchase_types WHERE id=?", (id,))
    return {"ok": True}


# ── REST: Price book ───────────────────────────────────────────────────────
@app.get("/api/pricebook")
def get_pricebook():
    with get_db() as db:
        rows = db.execute("SELECT * FROM pricebook ORDER BY item_name").fetchall()
    return [dict(r) for r in rows]


class PricebookEntry(BaseModel):
    item_name:        str
    item_code:        Optional[str]   = ""
    currency:         Optional[str]   = "SGD"
    unit_name:        Optional[str]   = ""
    rate:             Optional[float] = None
    min_sale_price:   Optional[float] = None
    buying_price:     Optional[float] = None
    purchase_type_id: Optional[int]   = None
    notes:            Optional[str]   = ""


@app.post("/api/pricebook")
def add_pricebook(entry: PricebookEntry):
    with get_db() as db:
        db.execute("""
            INSERT INTO pricebook
                (item_name, item_code, currency, unit_name, rate,
                 min_sale_price, buying_price, purchase_type_id, notes, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (entry.item_name, entry.item_code, entry.currency, entry.unit_name,
             entry.rate, entry.min_sale_price, entry.buying_price,
             entry.purchase_type_id, entry.notes, datetime.now().isoformat()))
    return {"ok": True}


@app.put("/api/pricebook/{id}")
def update_pricebook(id: int, entry: PricebookEntry):
    with get_db() as db:
        db.execute("""
            UPDATE pricebook SET
                item_name=?, item_code=?, currency=?, unit_name=?, rate=?,
                min_sale_price=?, buying_price=?, purchase_type_id=?, notes=?, updated_at=?
            WHERE id=?""",
            (entry.item_name, entry.item_code, entry.currency, entry.unit_name,
             entry.rate, entry.min_sale_price, entry.buying_price,
             entry.purchase_type_id, entry.notes, datetime.now().isoformat(), id))
    return {"ok": True}


@app.patch("/api/pricebook/bulk")
def bulk_update_pricebook(body: dict):
    """Bulk assign purchase type to multiple pricebook items."""
    ids              = body.get("ids", [])
    purchase_type_id = body.get("purchase_type_id")   # None = clear it
    if not ids:
        raise HTTPException(400, "No IDs provided")
    with get_db() as db:
        for pid in ids:
            db.execute(
                "UPDATE pricebook SET purchase_type_id=?, updated_at=? WHERE id=?",
                (purchase_type_id, datetime.now().isoformat(), pid)
            )
    return {"ok": True, "updated": len(ids)}


@app.delete("/api/pricebook/{id}")
def delete_pricebook(id: int):
    with get_db() as db:
        db.execute("DELETE FROM pricebook WHERE id=?", (id,))
    return {"ok": True}


@app.post("/api/pricebook/import")
async def import_pricebook(file: UploadFile = File(...)):
    """
    Import pricebook from Focus ERP Excel export.
    - Items already in DB: prices updated, purchase_type_id preserved
    - New items: inserted with no purchase_type_id
    - DB items not found in file: returned as unmatched list
    """
    import pandas as pd, io

    if not file.filename.lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Please upload an Excel file (.xlsx or .xls)")

    raw = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(400, f"Could not read Excel file: {e}")

    # Normalise columns: remove spaces/underscores, lowercase for matching
    df.columns = df.columns.str.strip()
    norm = {c.lower().replace(" ", "").replace("_", ""): c for c in df.columns}

    col_map = {
        "item_name":      norm.get("itemname"),
        "item_code":      norm.get("itemcode"),
        "currency":       norm.get("currency"),
        "unit_name":      norm.get("unitname"),
        "rate":           norm.get("rate"),
        "min_sale_price": norm.get("minsaleprice"),
        "buying_price":   norm.get("buyingprice"),
    }

    if not col_map["item_name"]:
        raise HTTPException(400,
            f"Could not find 'Item Name' column. Found columns: {list(df.columns)}")

    def get_float(row, col):
        if not col or col not in row.index:
            return None
        try:
            v = float(row[col])
            return None if pd.isna(v) else round(v, 4)
        except Exception:
            return None

    def get_str(row, col):
        if not col or col not in row.index:
            return None
        v = str(row[col]).strip()
        return None if v.lower() in ("nan", "none", "") else v

    now = datetime.now().isoformat()
    updated, added = [], []

    try:
        with get_db() as db:
            existing = db.execute("SELECT * FROM pricebook").fetchall()
            # Composite key: item_name + unit_name (same product can have PCS and CTN rows)
            existing_map = {
                (str(r["item_name"]).strip().lower(),
                 str(r["unit_name"] or "").strip().lower()): dict(r)
                for r in existing
            }
            file_keys = set()

            for _, row in df.iterrows():
                name = get_str(row, col_map["item_name"])
                if not name:
                    continue

                item_code      = get_str(row, col_map["item_code"]) or name
                currency       = get_str(row, col_map["currency"]) or "SGD"
                unit_name      = get_str(row, col_map["unit_name"]) or ""
                rate           = get_float(row, col_map["rate"])
                min_sale_price = get_float(row, col_map["min_sale_price"])
                buying_price   = get_float(row, col_map["buying_price"])

                key = (name.strip().lower(), unit_name.strip().lower())
                file_keys.add(key)

                if key in existing_map:
                    ex = existing_map[key]
                    db.execute("""
                        UPDATE pricebook SET
                            item_code=?, currency=?, unit_name=?, rate=?,
                            min_sale_price=?, buying_price=?, updated_at=?
                        WHERE id=?""",
                        (item_code, currency, unit_name, rate,
                         min_sale_price, buying_price, now, ex["id"]))
                    updated.append(name)
                else:
                    db.execute("""
                        INSERT INTO pricebook
                            (item_name, item_code, currency, unit_name,
                             rate, min_sale_price, buying_price, updated_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (name, item_code, currency, unit_name,
                         rate, min_sale_price, buying_price, now))
                    added.append(name)

            # Unmatched = DB rows whose (name, unit) combo wasn't in the file
            unmatched = [
                {"id": r["id"], "item_name": r["item_name"],
                 "unit_name": r["unit_name"],
                 "purchase_type_id": dict(r).get("purchase_type_id")}
                for r in existing
                if (str(r["item_name"]).strip().lower(),
                    str(r["unit_name"] or "").strip().lower()) not in file_keys
            ]

    except Exception as e:
        import traceback
        raise HTTPException(500, f"Import error: {e}\n{traceback.format_exc()}")

    return {
        "ok":              True,
        "updated":         len(updated),
        "added":           len(added),
        "unmatched":       unmatched,
        "unmatched_count": len(unmatched),
    }


# ── REST: Global config ────────────────────────────────────────────────────
@app.get("/api/config")
def get_config():
    with get_db() as db:
        rows = db.execute("SELECT * FROM config").fetchall()
    return [dict(r) for r in rows]


class ConfigUpdate(BaseModel):
    value: str


@app.put("/api/config/{key}")
def update_config(key: str, body: ConfigUpdate):
    with get_db() as db:
        db.execute("UPDATE config SET value=? WHERE key=?", (body.value, key))
    return {"ok": True}


# ── WebSocket ──────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await mgr.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        mgr.disconnect(ws)


# ── Serve frontend ─────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/")
def serve():
    return FileResponse("frontend/index.html")
