"""
sync_api.py — Data Sync agent, self-contained FastAPI router.

Mounted by erp_dashboard/main.py with a single guarded include_router()
(same isolation pattern as the sales agent). Provides:

  POST /api/sync/daybook   — run sync_runner.py as a subprocess
  GET  /api/sync/status    — last sync results

The runner does the Focus browsing (frozen-login copy) and imports via
sales_agent's parsers. If anything here breaks, only /api/sync/*
disappears; the dashboard and other agents are unaffected.
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

_THIS_DIR = Path(__file__).resolve().parent          # data_sync/

from database import get_db  # erp_dashboard is the server CWD


def _dash():
    import main
    return main


router = APIRouter()

_schema_ready = False
_sync_running = {"daybook": False, "pricebook": False, "items": False,
                 "accounts": False}


def _ensure_schema():
    """Seed the agent card + settings (idempotent, tolerant on fresh DBs)."""
    global _schema_ready
    if _schema_ready:
        return
    with get_db() as conn:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO agents
                    (name, display_name, description, status, interval_min,
                     enabled)
                VALUES ('data_sync', 'Data Sync Agent',
                        'Downloads sales day book and masters from Focus '
                        'and updates the local database.',
                        'idle', 0, 1)
            """)
            conn.execute("""
                INSERT OR IGNORE INTO agent_settings
                    (agent, key, value, label, category)
                VALUES ('data_sync', 'sync_days', '60',
                        'Day book window (days)', 'sync')
            """)
            conn.commit()
            _schema_ready = True
        except Exception:
            pass


async def _log(msg: str, level: str = "info"):
    try:
        await _dash().log("data_sync", msg, level)
    except Exception:
        print(f"[data_sync] {msg}")


@router.get("/api/sync/status")
async def sync_status():
    _ensure_schema()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM agent_settings "
            "WHERE agent='data_sync' AND key LIKE 'last_%'").fetchall()
    return {"running": dict(_sync_running),
            "last": {r[0]: r[1] for r in rows}}


@router.post("/api/sync/pricebook")
async def sync_pricebook(request: Request, body: dict = {}):
    _ensure_schema()
    if _sync_running["pricebook"]:
        raise HTTPException(409, "A price book sync is already running")
    auth_header = request.headers.get("authorization", "")
    user_token = (auth_header.replace("Bearer ", "")
                  if auth_header.startswith("Bearer ") else "")

    await _log("━" * 46)
    await _log("▶ SELLER PRICE BOOK SYNC")

    import subprocess
    log_path = _THIS_DIR / "sync_run.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "AGENT_AUTH_TOKEN": user_token,
           "DASHBOARD_URL": str(request.base_url)}
    try:
        m = _dash()
        env["AGENT_DIR"] = str(m.AGENT_DIR)
        from database import DB_PATH
        env["DASHBOARD_DB"] = str(DB_PATH.resolve())
    except Exception:
        pass

    def _run() -> int:
        with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
            return subprocess.call(
                [sys.executable, "sync_runner.py", "--what", "pricebook"],
                cwd=str(_THIS_DIR), stdout=lf,
                stderr=subprocess.STDOUT, env=env)

    _sync_running["pricebook"] = True
    try:
        rc = await asyncio.to_thread(_run)
    except Exception as e:
        await _log(f"❌ Could not start sync_runner.py: {e}")
        raise HTTPException(500, f"Could not start the sync runner: {e}")
    finally:
        _sync_running["pricebook"] = False

    try:
        for line in log_path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            if line.strip():
                await _log(line.rstrip())
    except Exception:
        pass

    ok = rc == 0
    await _log("✅ Price book sync finished" if ok
               else f"❌ Price book sync failed (exit {rc}) — see above")
    summary = ""
    if ok:
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT value FROM agent_settings WHERE "
                    "agent='data_sync' AND key='last_pricebook'").fetchone()
            summary = row[0] if row else ""
        except Exception:
            pass
    return {"ok": ok, "summary": summary}


@router.post("/api/sync/accounts")
async def sync_accounts(request: Request, body: dict = {}):
    _ensure_schema()
    if _sync_running["accounts"]:
        raise HTTPException(409, "A customers master sync is already "
                                 "running")
    auth_header = request.headers.get("authorization", "")
    user_token = (auth_header.replace("Bearer ", "")
                  if auth_header.startswith("Bearer ") else "")

    await _log("━" * 46)
    await _log("▶ CUSTOMERS MASTER SYNC")

    import subprocess
    log_path = _THIS_DIR / "sync_run.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "AGENT_AUTH_TOKEN": user_token,
           "DASHBOARD_URL": str(request.base_url)}
    try:
        m = _dash()
        env["AGENT_DIR"] = str(m.AGENT_DIR)
        from database import DB_PATH
        env["DASHBOARD_DB"] = str(DB_PATH.resolve())
    except Exception:
        pass

    def _run() -> int:
        with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
            return subprocess.call(
                [sys.executable, "sync_runner.py", "--what", "accounts"],
                cwd=str(_THIS_DIR), stdout=lf,
                stderr=subprocess.STDOUT, env=env)

    _sync_running["accounts"] = True
    try:
        rc = await asyncio.to_thread(_run)
    except Exception as e:
        await _log(f"❌ Could not start sync_runner.py: {e}")
        raise HTTPException(500, f"Could not start the sync runner: {e}")
    finally:
        _sync_running["accounts"] = False

    try:
        for line in log_path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            if line.strip():
                await _log(line.rstrip())
    except Exception:
        pass

    ok = rc == 0
    await _log("✅ Customers master sync finished" if ok
               else f"❌ Customers master sync failed (exit {rc}) — "
                    f"see above")
    summary = ""
    if ok:
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT value FROM agent_settings WHERE "
                    "agent='data_sync' AND key='last_accounts'").fetchone()
            summary = row[0] if row else ""
        except Exception:
            pass
    return {"ok": ok, "summary": summary}


@router.post("/api/sync/items")
async def sync_items(request: Request, body: dict = {}):
    _ensure_schema()
    if _sync_running["items"]:
        raise HTTPException(409, "An items master sync is already running")
    auth_header = request.headers.get("authorization", "")
    user_token = (auth_header.replace("Bearer ", "")
                  if auth_header.startswith("Bearer ") else "")

    await _log("━" * 46)
    await _log("▶ ITEMS MASTER SYNC")

    import subprocess
    log_path = _THIS_DIR / "sync_run.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "AGENT_AUTH_TOKEN": user_token,
           "DASHBOARD_URL": str(request.base_url)}
    try:
        m = _dash()
        env["AGENT_DIR"] = str(m.AGENT_DIR)
        from database import DB_PATH
        env["DASHBOARD_DB"] = str(DB_PATH.resolve())
    except Exception:
        pass

    def _run() -> int:
        with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
            return subprocess.call(
                [sys.executable, "sync_runner.py", "--what", "items"],
                cwd=str(_THIS_DIR), stdout=lf,
                stderr=subprocess.STDOUT, env=env)

    _sync_running["items"] = True
    try:
        rc = await asyncio.to_thread(_run)
    except Exception as e:
        await _log(f"❌ Could not start sync_runner.py: {e}")
        raise HTTPException(500, f"Could not start the sync runner: {e}")
    finally:
        _sync_running["items"] = False

    try:
        for line in log_path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            if line.strip():
                await _log(line.rstrip())
    except Exception:
        pass

    ok = rc == 0
    await _log("✅ Items master sync finished" if ok
               else f"❌ Items master sync failed (exit {rc}) — see above")
    summary = ""
    if ok:
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT value FROM agent_settings WHERE "
                    "agent='data_sync' AND key='last_items'").fetchone()
            summary = row[0] if row else ""
        except Exception:
            pass
    return {"ok": ok, "summary": summary}


@router.post("/api/sync/daybook")
async def sync_daybook(request: Request, body: dict = {}):
    _ensure_schema()
    # Same mechanism as the low-price agent: pass the signed-in user's
    # Firebase token so the runner can fetch ERP credentials from Firestore.
    auth_header = request.headers.get("authorization", "")
    user_token = (auth_header.replace("Bearer ", "")
                  if auth_header.startswith("Bearer ") else "")
    if _sync_running["daybook"]:
        raise HTTPException(409, "A day book sync is already running")

    try:
        days = int(body.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    if not days:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM agent_settings WHERE agent='data_sync' "
                "AND key='sync_days'").fetchone()
        days = int(row[0]) if row and str(row[0]).isdigit() else 60
    days = max(1, min(days, 366))

    await _log("━" * 46)
    await _log(f"▶ DAY BOOK SYNC — last {days} days")

    # Proven Windows-safe pattern: plain subprocess in a thread,
    # stdout to a file (never asyncio subprocess, never pipes).
    import subprocess
    log_path = _THIS_DIR / "sync_run.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "AGENT_AUTH_TOKEN": user_token,
           "DASHBOARD_URL": str(request.base_url)}
    # Hand the dashboard's own resolutions to the runner (works no matter
    # whether the low-price agent lives at project root or in focus_agent/)
    try:
        m = _dash()
        env["AGENT_DIR"] = str(m.AGENT_DIR)
        from database import DB_PATH
        env["DASHBOARD_DB"] = str(DB_PATH.resolve())
    except Exception:
        pass

    def _run() -> int:
        with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
            return subprocess.call(
                [sys.executable, "sync_runner.py",
                 "--what", "daybook", "--days", str(days)],
                cwd=str(_THIS_DIR), stdout=lf,
                stderr=subprocess.STDOUT, env=env)

    _sync_running["daybook"] = True
    try:
        rc = await asyncio.to_thread(_run)
    except Exception as e:
        await _log(f"❌ Could not start sync_runner.py: {e}")
        raise HTTPException(500, f"Could not start the sync runner: {e}")
    finally:
        _sync_running["daybook"] = False

    try:
        for line in log_path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            if line.strip():
                await _log(line.rstrip())
    except Exception:
        pass

    ok = rc == 0
    await _log("✅ Day book sync finished" if ok
               else f"❌ Day book sync failed (exit {rc}) — see lines above")

    # Invalidate the sales rows cache so the browser total refreshes
    if ok:
        try:
            import sales_agent.sales_api as sales_api
            sales_api._invalidate_rows_cache()
        except Exception:
            pass
    return {"ok": ok}
