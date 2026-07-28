"""
database.py — SQLite setup for the ERP Agent Dashboard
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# Always use absolute path so it works regardless of working directory
DB_PATH = Path(__file__).parent / "dashboard.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:

        # ── Agents ────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                name         TEXT PRIMARY KEY,
                display_name TEXT,
                description  TEXT,
                status       TEXT DEFAULT 'idle',
                interval_min INTEGER DEFAULT 15,
                enabled      INTEGER DEFAULT 0,
                last_run     TEXT,
                last_status  TEXT,
                next_run     TEXT
            )
        """)

        # ── Per-agent settings ────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_settings (
                agent    TEXT,
                key      TEXT,
                value    TEXT,
                label    TEXT,
                category TEXT,
                PRIMARY KEY (agent, key)
            )
        """)

        # ── Price book ────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pricebook (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name        TEXT NOT NULL,
                purchase_type    TEXT DEFAULT 'standard_import',
                landing_pct      REAL DEFAULT 5.0,
                repacking_cost   REAL DEFAULT 0.0,
                stockpile_charge REAL DEFAULT 0.0,
                source_item      TEXT,
                notes            TEXT,
                updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate existing price book tables (add new columns if missing)
        for col, defval in [
            ("purchase_type",    "'standard_import'"),
            ("repacking_cost",   "0.0"),
            ("stockpile_charge", "0.0"),
            ("source_item",      "NULL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE pricebook ADD COLUMN {col} REAL DEFAULT {defval}")
            except Exception:
                pass  # column already exists

        # ── Global config ─────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key      TEXT PRIMARY KEY,
                value    TEXT,
                label    TEXT,
                category TEXT
            )
        """)

        # ── Logs ──────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                agent   TEXT,
                level   TEXT DEFAULT 'info',
                message TEXT,
                ts      TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Seed: agents ──────────────────────────────────────────────────
        agents = [
            ("low_price", "Low Price Agent",
             "Monitors live Sales Orders for below-cost pricing in real-time.",
             15, 0),
            ("data_sync", "Data Sync Agent",
             "Fetches Sales, Purchase, Stock and Customer reports from Focus ERP daily.",
             720, 0),
        ]
        for name, display, desc, interval, enabled in agents:
            conn.execute("""
                INSERT OR IGNORE INTO agents
                    (name, display_name, description, interval_min, enabled)
                VALUES (?, ?, ?, ?, ?)
            """, (name, display, desc, interval, enabled))

        # ── Seed: per-agent settings ──────────────────────────────────────
        agent_cfg = [
            # Low Price Agent
            ("low_price", "whatsapp_groups",  '["Low price invoice"]',
             "WhatsApp groups (JSON list)", "whatsapp"),
            ("low_price", "focus_url",        "https://ymt-9.focus9erp.com/focusx",
             "Focus ERP URL", "focus"),
            ("low_price", "credentials_file", "credentials.xlsx",
             "Credentials Excel file path", "focus"),
            ("low_price", "default_landing",  "5.0",
             "Default landing cost % (if not in price book)", "pricing"),

            # Data Sync Agent
            ("data_sync", "whatsapp_groups",  '["Management"]',
             "WhatsApp groups (JSON list)", "whatsapp"),
            ("data_sync", "focus_url",        "https://ymt-9.focus9erp.com/focusx",
             "Focus ERP URL", "focus"),
            ("data_sync", "credentials_file", "credentials.xlsx",
             "Credentials Excel file path", "focus"),
            ("data_sync", "run_times",        "07:00,15:00",
             "Scheduled run times (comma-separated)", "schedule"),
            ("data_sync", "reports",          "sales,purchases,stock,customers",
             "Reports to fetch (comma-separated)", "reports"),
        ]
        for agent, key, value, label, cat in agent_cfg:
            conn.execute("""
                INSERT OR IGNORE INTO agent_settings
                    (agent, key, value, label, category)
                VALUES (?, ?, ?, ?, ?)
            """, (agent, key, value, label, cat))

        # ── Seed: global config ───────────────────────────────────────────
        global_cfg = [
            ("default_landing_pct", "5.0",
             "Default landing cost % (applies to all agents)", "pricing"),
        ]
        for key, value, label, cat in global_cfg:
            conn.execute("""
                INSERT OR IGNORE INTO config (key, value, label, category)
                VALUES (?, ?, ?, ?)
            """, (key, value, label, cat))

    print("✅ Database initialised.")
