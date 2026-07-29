"""
focus_scraper.py  —  All Phases
Phase 1: Login + Navigate to Sales Order + Filter pending
Phase 2: Read table → Voucher numbers with empty LowCost (price issues)
Phase 3: Navigate to Sales Orders Register → Select Today → Download Excel
Phase 4: Process Excel → Match vouchers → Find Cost Restriction empty → Calculate margins
"""

import asyncio
from pathlib import Path
import os
import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
import config

FOCUS_BASE_URL = "https://ymt-9.focus9erp.com/focusx"

# Path to dashboard DB for price book lookup (works standalone too)
_DASHBOARD_DB = Path(__file__).parent.parent / "erp_dashboard" / "dashboard.db"


def _apply_category(pricebook_price: float, cat: dict) -> tuple:
    """Apply a landing category to a price, return (cost, label)."""
    ptype = (cat.get("purchase_type") or "standard_import").strip()
    pct   = cat.get("landing_pct")   or 0
    rep   = cat.get("repacking_cost")   or 0
    stk   = cat.get("stockpile_charge") or 0
    if ptype == "local_purchase":
        return round(pricebook_price, 2), "local purchase"
    elif ptype == "local_repacking":
        return round(pricebook_price + rep, 2), f"+${rep:.2f} repacking"
    elif ptype == "import_repacking":
        return round(pricebook_price * (1 + pct/100) + rep, 2), f"+{pct}% +${rep:.2f} repacking"
    elif ptype == "stockpile_rice":
        return round(pricebook_price + stk, 2), f"+${stk:.2f} stockpile"
    else:  # standard_import
        return round(pricebook_price * (1 + pct/100), 2), f"+{pct}%"


def get_landing_cost(item_name: str, pricebook_price: float,
                     default_pct: float = None) -> tuple:
    """
    Returns (landing_cost, label).
    Looks up item in pricebook → gets its category → applies formula.
    Falls back to the default category (is_default=1) or 5% if DB unavailable.
    """
    try:
        if _DASHBOARD_DB.exists():
            import sqlite3
            conn = sqlite3.connect(_DASHBOARD_DB)
            conn.row_factory = sqlite3.Row

            # Look up item → category
            row = conn.execute("""
                SELECT lc.* FROM pricebook pb
                JOIN purchase_types lc ON pb.purchase_type_id = lc.id
                WHERE LOWER(TRIM(pb.item_name)) = LOWER(TRIM(?))
            """, (item_name,)).fetchone()

            if row:
                conn.close()
                return _apply_category(pricebook_price, dict(row))

            # Fall back to default category
            default_cat = conn.execute(
                "SELECT * FROM purchase_types WHERE is_default=1 LIMIT 1"
            ).fetchone()
            conn.close()

            if default_cat:
                return _apply_category(pricebook_price, dict(default_cat))

    except Exception:
        pass

    # Last resort: use hardcoded default
    pct = default_pct or 5.0
    return round(pricebook_price * (1 + pct/100), 2), f"+{pct}%"

class FocusScraper:

    async def run(self, username: str, password: str) -> list:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=config.HEADLESS)
            context = await browser.new_context(accept_downloads=True, viewport={"width": 1920, "height": 1080})
            # Set default timeout to 60 seconds globally
            context.set_default_timeout(60_000)
            page = await context.new_page()
            try:
                alerts = await self._run_phases(page, username, password)
            except PlaywrightTimeout as e:
                print(f"❌ Timeout: {e}")
                try:
                    await page.screenshot(path="error_screenshot.png", timeout=10_000)
                    print("   Screenshot saved to error_screenshot.png")
                except Exception:
                    print("   (Could not save screenshot)")
                raise
            except Exception as e:
                print(f"❌ Scraping failed: {e}")
                try:
                    await page.screenshot(path="error_screenshot.png", timeout=10_000)
                except Exception:
                    pass
                raise
            finally:
                await asyncio.sleep(2)
                await browser.close()
        return alerts

    async def _run_phases(self, page, username: str, password: str) -> list:

        # ── PHASE 1: Login + Navigate to Sales Order ──────────────────────

        print("🌐 Opening Focus ERP...")
        await page.goto(FOCUS_BASE_URL, wait_until="networkidle", timeout=90_000)
        await asyncio.sleep(3)  # Wait for all JS to fully initialise
        print("   Page loaded.")

        print("🔐 Logging in...")
        await page.wait_for_selector("#txtUsername", state="visible", timeout=30_000)
        await asyncio.sleep(2)
        await page.bring_to_front()

        # Step 1: Initialise bRemFlag so getCompanySuccess callback doesn't crash
        await page.evaluate("window.bRemFlag = false;")

        # Step 2: Type username
        await page.click("#txtUsername")
        await asyncio.sleep(0.3)
        await page.keyboard.type(username, delay=50)
        await asyncio.sleep(0.3)

        # Step 3: Press Tab to trigger onblur → loads company list → strChkList populated
        await page.keyboard.press("Tab")
        await asyncio.sleep(2)  # Wait for company list AJAX to complete

        # Step 4: Click password field explicitly (don't rely on Tab focus)
        await page.click("#txtPassword")
        await asyncio.sleep(0.3)
        await page.keyboard.type(password, delay=50)
        await asyncio.sleep(0.5)

        # Step 5: Click Sign In
        await page.wait_for_selector("#btnSignin", state="visible", timeout=10_000)
        await page.click("#btnSignin")

        await page.wait_for_load_state("networkidle", timeout=30_000)
        await asyncio.sleep(2)
        print("   Logged in successfully.")

        print("📂 Navigating to Sales Order...")
        await page.get_by_role("link", name="Inventory").first.click()
        await asyncio.sleep(1.5)
        await page.get_by_role("link", name="Transactions").first.click()
        await asyncio.sleep(1.5)
        await page.get_by_role("link", name="Sales").first.click()
        await asyncio.sleep(1.5)
        await page.get_by_role("link", name="Sales Order").first.click()
        await asyncio.sleep(4)
        print("   Sales Order page opened.")

        print("🔽 Applying pending filter...")
        await page.wait_for_selector("i.icon-filter.hiconright2", timeout=30_000)
        await page.click("i.icon-filter.hiconright2")
        await asyncio.sleep(2)
        await page.wait_for_selector('[id="2106_2_DefaultFilter_3"]', timeout=15_000)
        await page.fill('[id="2106_2_DefaultFilter_3"]', "pending")
        await asyncio.sleep(0.5)
        await page.click("#btnSetFilterVal")
        await asyncio.sleep(4)
        print("✅ Phase 1 complete.\n")

        # ── PHASE 2: Read table, find price-issue vouchers ─────────────────

        print("📊 Phase 2: Reading Sales Order table...")
        await page.wait_for_selector("table", timeout=30_000)
        await asyncio.sleep(2)

        result = await page.evaluate("""() => {
            const tables = document.querySelectorAll('table');
            let biggest = null, maxRows = 0;
            for (const t of tables) {
                const rows = t.querySelectorAll('tr').length;
                if (rows > maxRows) { maxRows = rows; biggest = t; }
            }
            if (!biggest) return { headers: [], rows: [] };
            const headers = Array.from(biggest.querySelectorAll('thead th')).map(h => h.innerText.trim());
            const rows = Array.from(biggest.querySelectorAll('tbody tr')).map(row =>
                Array.from(row.querySelectorAll('td')).map(c => c.innerText.trim())
            );
            return { headers, rows };
        }""")

        headers = result["headers"]
        rows    = result["rows"]
        if not headers or not rows:
            raise ValueError("Sales Order table not found or empty.")

        df = pd.DataFrame(rows, columns=headers[:len(rows[0])])

        auth_col    = next((c for c in df.columns if "authorization" in str(c).lower()), None)
        lowcost_col = next((c for c in df.columns if str(c).strip().lower() == "lowcost"), None)
        voucher_col = next((c for c in df.columns if "voucher" in str(c).lower()), None)

        if not all([auth_col, lowcost_col, voucher_col]):
            raise ValueError(f"Required columns not found. Available: {list(df.columns)}")

        pending = df[df[auth_col].astype(str).str.strip().str.lower() == "pending"]
        price_issue = pending[
            pending[lowcost_col].isna() |
            (pending[lowcost_col].astype(str).str.strip().isin(["", "nan"]))
        ]

        price_issue_vouchers = set(price_issue[voucher_col].dropna().tolist())

        print(f"   Total rows          : {len(df)}")
        print(f"   Pending orders      : {len(pending)}")
        print(f"   Price issue vouchers: {len(price_issue_vouchers)}")
        for v in sorted(price_issue_vouchers):
            print(f"   → {v}")
        print("✅ Phase 2 complete.\n")

        if not price_issue_vouchers:
            print("ℹ️  No price-issue vouchers found. Nothing to report.")
            return []

        # ── PHASE 3 & 4: Click each flagged voucher → read detail table ──────

        print("🔍 Phase 3: Opening each price-issue voucher to read line items.\n")
        alerts = []

        for voucher_num in sorted(price_issue_vouchers):
            print(f"   📋 Opening voucher {voucher_num}...")
            try:
                # Scroll list to bring row into view
                await page.evaluate(f"""() => {{
                    for (const row of document.querySelectorAll('tr')) {{
                        if (row.innerText.includes('{voucher_num}')) {{
                            row.scrollIntoView({{ block: 'center' }});
                            break;
                        }}
                    }}
                }}""")
                await asyncio.sleep(0.5)

                # Double-click the row to open the voucher
                row_locator = page.locator(f"tr:has-text('{voucher_num}')").first
                await row_locator.wait_for(state="visible", timeout=10_000)
                await row_locator.dblclick()
                await asyncio.sleep(3)

                # Wait for the detail grid to attach (retry once if slow)
                try:
                    await page.wait_for_selector("#id_transaction_entry_detail_table",
                                                 state="attached", timeout=30_000)
                except PlaywrightTimeout:
                    print(f"      ⏳ Table slow to load, waiting extra 5s and retrying...")
                    await asyncio.sleep(5)
                    await page.wait_for_selector("#id_transaction_entry_detail_table",
                                                 state="attached", timeout=20_000)

                # Scroll table into view and trigger full render of all columns
                await page.evaluate("""() => {
                    const tbl = document.querySelector('#id_transaction_entry_detail_table');
                    if (!tbl) return;
                    tbl.scrollIntoView({ behavior: 'instant', block: 'center' });

                    // Find the horizontal scroll container and scroll right then left
                    // This forces Focus's grid to render all column cells
                    let el = tbl.parentElement;
                    while (el && el !== document.body) {
                        if (el.scrollWidth > el.clientWidth) {
                            el.scrollLeft = el.scrollWidth;  // scroll fully right
                            el.scrollLeft = 0;               // scroll back to start
                            break;
                        }
                        el = el.parentElement;
                    }
                }""")
                await asyncio.sleep(4)  # Wait for all cells to render

                # Read headers and rows
                result = await page.evaluate("""() => {
                    const tbl = document.querySelector('#id_transaction_entry_detail_table');
                    if (!tbl) return { headers: [], rows: [] };
                    const headers = Array.from(tbl.querySelectorAll('th[data-heading]'))
                        .map(h => h.getAttribute('data-heading').trim())
                        .filter(h => h !== '');
                    // Skip first cell (# row-number column — no header)
                    // Use innerText — data-value returns internal IDs for text columns
                    const rows = Array.from(tbl.querySelectorAll('tbody tr')).map(row => {
                        const cells = Array.from(row.querySelectorAll('td')).slice(1);
                        return cells.map(c => c.innerText.trim());
                    }).filter(r => r.some(c => c !== ''));
                    return { headers, rows };
                }""")

                headers = result["headers"]
                rows    = result["rows"]

                if not headers or not rows:
                    print(f"   ⚠️  No detail table found for {voucher_num} — skipping.")
                    raise Exception("No table data")

                # Build DataFrame
                detail_df = pd.DataFrame(rows)
                if len(headers) == detail_df.shape[1]:
                    detail_df.columns = headers
                else:
                    col_names = list(headers) + [f"Col_{i}" for i in range(len(headers), detail_df.shape[1])]
                    detail_df.columns = col_names[:detail_df.shape[1]]

                print(f"      Columns : {list(detail_df.columns)}")

                # Map columns
                col_map = {
                    "item"        : next((c for c in detail_df.columns if str(c).lower() == "description"), None)
                                    or next((c for c in detail_df.columns if "item" in str(c).lower()), None),
                    "last_price"  : next((c for c in detail_df.columns if "last" in str(c).lower() and "price" in str(c).lower()), None),
                    "rate"        : next((c for c in detail_df.columns if str(c).strip().lower() == "rate"), None),
                    "cost_restr"  : next((c for c in detail_df.columns if "cost" in str(c).lower() and "rest" in str(c).lower()), None),
                    "buy_price"   : next((c for c in detail_df.columns if "pricebook" in str(c).lower()), None)
                                    or next((c for c in detail_df.columns if "buying" in str(c).lower()), None),
                    "qty_per_ctn" : next((c for c in detail_df.columns if "qty" in str(c).lower() and "ctn" in str(c).lower()), None),
                    "quantity"    : next((c for c in detail_df.columns if str(c).strip().lower() == "quantity"), None),
                }

                # Get customer name and salesman from header input fields (data-focustext)
                unit_col = next((c for c in detail_df.columns if str(c).strip().lower() == "unit"), None)
                party = await page.evaluate("""() => {
                    const el = document.querySelector('input[data-fieldname="CustomerAC"]');
                    return el ? el.getAttribute('data-focustext') : '';
                }""")
                salesman = await page.evaluate("""() => {
                    const el = document.querySelector('input[data-fieldname="Salesman"]');
                    return el ? el.getAttribute('data-focustext') : '';
                }""")
                party    = str(party).strip()    or str(voucher_num)
                salesman = str(salesman).strip()  or "Unknown"

                # Filter rows where Cost Restriction is empty or 0 = price issue
                # (Cost Restriction = 1.00 means payment block; 0.00 or empty = price issue)
                if col_map["cost_restr"]:
                    low_cost = detail_df[
                        detail_df[col_map["cost_restr"]].isna() |
                        (detail_df[col_map["cost_restr"]].astype(str).str.strip().isin(["", "nan", "0", "0.0", "0.00"]))
                    ]
                else:
                    low_cost = detail_df

                import re as _re
                items = []
                for _, row in low_cost.iterrows():
                    try:
                        description = str(row.get(col_map["item"], ""))
                        unit        = str(row.get(unit_col, "pcs")).strip() if unit_col else "pcs"

                        # Skip FOC (Free of Charge) items — these are intentional promotions
                        if "foc" in unit.lower():
                            continue

                        unit = unit.lower()
                        pricebook   = float(str(row.get(col_map["buy_price"], 0) or 0).replace(",", ""))
                        rate_total  = float(str(row.get(col_map["rate"], 0) or 0).replace(",", ""))
                        prev_price  = float(str(row.get(col_map["last_price"], 0) or 0).replace(",", ""))

                        # Convert rate to per-piece if NOT sold as individual pieces
                        # Unit = pcs/pieces/pc → no conversion needed (rate is already per piece)
                        # Unit = ctn/bag/carton/etc → divide rate by qty per ctn
                        pieces_per_unit = 1
                        is_pieces = any(p in unit.lstrip(".") for p in ("pcs", "pc", "piece"))
                        if not is_pieces:
                            if col_map["qty_per_ctn"]:
                                try:
                                    pieces_per_unit = int(float(str(row.get(col_map["qty_per_ctn"], 1) or 1)))
                                except (ValueError, TypeError):
                                    pieces_per_unit = 1
                            if pieces_per_unit <= 1:
                                # Fallback: parse from description e.g. "GHEE 1LTR X 16"
                                match = _re.search(r'[Xx]\s*(\d+)\s*$', description.strip())
                                if match:
                                    pieces_per_unit = int(match.group(1))

                        rate_per_piece = rate_total / pieces_per_unit if pieces_per_unit > 0 else rate_total

                        # Prev price per piece (convert from CTN price if needed)
                        prev_price_per_piece = prev_price / pieces_per_unit if pieces_per_unit > 1 and prev_price > 0 else prev_price

                        # Quantity ordered
                        quantity = 0
                        if col_map["quantity"]:
                            try:
                                quantity = float(str(row.get(col_map["quantity"], 0) or 0).replace(",", ""))
                            except (ValueError, TypeError):
                                quantity = 0

                        # Landing cost — looks up price book, falls back to default %
                        landing, landing_label = get_landing_cost(description, pricebook)

                        # Margin based on rate per piece vs landing cost
                        margin = round(((rate_per_piece - landing) / rate_per_piece * 100), 1) if rate_per_piece > 0 else 0

                        diff = round(rate_per_piece - landing, 2)

                        items.append({
                            "item"            : description,
                            "unit"            : unit.upper().lstrip("."),
                            "pieces_per_unit" : pieces_per_unit,
                            "quantity"        : quantity,
                            "pricebook"       : pricebook,
                            "landing"         : landing,
                            "landing_label"   : landing_label,
                            "rate_per_piece"  : round(rate_per_piece, 2),
                            "prev_price"      : round(prev_price_per_piece, 2),
                            "diff"            : diff,
                            "margin"          : margin,
                        })
                    except Exception as item_err:
                        print(f"      ⚠️  Skipping row: {item_err}")
                        continue

                if items:
                    alerts.append({"voucher": f"SO : {voucher_num}", "party": party, "salesman": salesman, "items": items})
                    print(f"      ✅ {len(items)} low-price item(s) found.")
                else:
                    print(f"      ℹ️  No low-price items in this voucher.")

            except Exception as voucher_err:
                print(f"   ⚠️  Skipping {voucher_num}: {voucher_err}")

            finally:
                # Always try to close and return to list
                try:
                    close_btn = page.locator("span.icon-close.d-none.d-md-block.hiconright2").first
                    await close_btn.wait_for(state="visible", timeout=4_000)
                    await close_btn.click()
                    await asyncio.sleep(2)
                except Exception:
                    pass

                # Verify back on list, re-navigate if needed
                try:
                    await page.wait_for_selector("i.icon-filter.hiconright2",
                                                 state="visible", timeout=6_000)
                except Exception:
                    print(f"   🔄 Re-navigating to Sales Order list...")
                    try:
                        await page.get_by_role("link", name="Inventory").first.click()
                        await asyncio.sleep(1)
                        await page.get_by_role("link", name="Transactions").first.click()
                        await asyncio.sleep(1)
                        await page.get_by_role("link", name="Sales").first.click()
                        await asyncio.sleep(1)
                        await page.get_by_role("link", name="Sales Order").first.click()
                        await asyncio.sleep(3)
                        await page.click("i.icon-filter.hiconright2")
                        await asyncio.sleep(1.5)
                        await page.fill('[id="2106_2_DefaultFilter_3"]', "pending")
                        await asyncio.sleep(0.5)
                        await page.click("#btnSetFilterVal")
                        await asyncio.sleep(3)
                    except Exception as nav_err:
                        print(f"   ❌ Could not re-navigate: {nav_err}")

        print(f"\n✅ Phase 3 complete. {len(alerts)} voucher(s) flagged.\n")
        return alerts

