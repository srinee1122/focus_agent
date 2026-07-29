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

        # ── Purchase types ───────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS purchase_types (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL,
                purchase_type    TEXT DEFAULT 'standard_import',
                landing_pct      REAL DEFAULT 5.0,
                repacking_cost   REAL DEFAULT 0.0,
                stockpile_charge REAL DEFAULT 0.0,
                is_default       INTEGER DEFAULT 0,
                notes            TEXT
            )
        """)

        # Seed default purchase types
        categories = [
            ("Default (5%)",             "standard_import",  5.0,  0.0,  0.0, 1, "Default landing cost — 5% for all unassigned items"),
            ("Local Purchase",          "local_purchase",   0.0,  0.0,  0.0, 0, "Locally bought — no landing cost"),
            ("Local Repacking + $0.30", "local_repacking",  0.0,  0.30, 0.0, 0, "Local buy, repacked as 1kg units"),
            ("Local Repacking + $0.20", "local_repacking",  0.0,  0.20, 0.0, 0, "Local buy, repacked as 500gm units"),
            ("Import Stockpile Rice",   "stockpile_rice",   5.0,  0.0,  0.50, 0, "Imported rice with stockpile charge"),
        ]
        for name, ptype, pct, repack, stock, is_def, notes in categories:
            conn.execute("""
                INSERT OR IGNORE INTO purchase_types
                    (name, purchase_type, landing_pct, repacking_cost, stockpile_charge, is_default, notes)
                SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS
                    (SELECT 1 FROM purchase_types WHERE name=?)
            """, (name, ptype, pct, repack, stock, is_def, notes, name))

        # ── Price book ────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pricebook (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name        TEXT NOT NULL,
                item_code        TEXT,
                currency         TEXT DEFAULT 'SGD',
                unit_name        TEXT,
                rate             REAL,
                min_sale_price   REAL,
                buying_price     REAL,
                purchase_type_id INTEGER,
                notes            TEXT,
                updated_at       TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (purchase_type_id) REFERENCES purchase_types(id)
            )
        """)
        # Reset hardcoded "Low price invoice" group to empty so user sets correct group
        conn.execute("""
            UPDATE agent_settings
            SET value = '[]'
            WHERE agent = 'low_price'
              AND key   = 'whatsapp_groups'
              AND value LIKE '%Low price invoice%'
        """)

        # Rename "Standard Import (5%)" → "Default (5%)" if old name exists
        conn.execute("""
            UPDATE purchase_types SET
                name  = 'Default (5%)',
                notes = 'Default landing cost — 5% for all unassigned items'
            WHERE name = 'Standard Import (5%)'
        """)

        # Remove duplicate defaults — keep the one with the lowest id, set it as default
        conn.execute("""
            DELETE FROM purchase_types
            WHERE (name LIKE 'Default%' OR is_default = 1)
            AND id != (
                SELECT MIN(id) FROM purchase_types
                WHERE name LIKE 'Default%' OR is_default = 1
            )
        """)
        # Ensure the remaining default is correctly named and flagged
        conn.execute("""
            UPDATE purchase_types SET
                name     = 'Default (5%)',
                is_default = 1
            WHERE id = (
                SELECT MIN(id) FROM purchase_types
                WHERE name LIKE 'Default%'
            )
        """)
        # Clear is_default on all others
        conn.execute("""
            UPDATE purchase_types SET is_default = 0
            WHERE is_default = 1 AND name != 'Default (5%)'
        """)

        # Migrate: add new columns to existing databases
        for col, coltype in [
            ("item_code",        "TEXT"),
            ("currency",         "TEXT DEFAULT 'SGD'"),
            ("unit_name",        "TEXT"),
            ("rate",             "REAL"),
            ("min_sale_price",   "REAL"),
            ("buying_price",     "REAL"),
            ("purchase_type_id", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE pricebook ADD COLUMN {col} {coltype}")
            except Exception:
                pass

        # ── Sent alerts log ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                agent     TEXT NOT NULL,
                voucher   TEXT NOT NULL,
                item_name TEXT NOT NULL DEFAULT '',
                sent_at   TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sent_alerts_lookup
            ON sent_alerts (agent, voucher, item_name, sent_at)
        """)
        # Migrate: add item_name column if missing (for existing databases)
        try:
            conn.execute("ALTER TABLE sent_alerts ADD COLUMN item_name TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass

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
            ("low_price", "whatsapp_groups",  '[]',
             "WhatsApp groups (JSON list)", "whatsapp"),
            ("low_price", "skip_orders",      "",
             "Skip these SO numbers (comma-separated, e.g. 42123,42232)", "alerts"),
            ("low_price", "skip_sent_sos",   "true",
             "Skip SOs already sent today", "alerts"),
            ("low_price", "send_text",        "true",
             "Send text message", "alerts"),
            ("low_price", "send_image",       "true",
             "Send table as image", "alerts"),
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
