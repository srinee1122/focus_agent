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

                # Wait for the detail grid to attach
                await page.wait_for_selector("#id_transaction_entry_detail_table",
                                             state="attached", timeout=20_000)
                await asyncio.sleep(1)

                # Scroll the grid container into view
                await page.evaluate("""() => {
                    const tbl = document.querySelector('#id_transaction_entry_detail_table');
                    if (tbl) {
                        tbl.scrollIntoView({ behavior: 'instant', block: 'center' });
                        let el = tbl.parentElement;
                        while (el && el !== document.body) {
                            if (el.scrollHeight > el.clientHeight) {
                                el.scrollTop = el.scrollHeight;
                                break;
                            }
                            el = el.parentElement;
                        }
                    }
                }""")
                await asyncio.sleep(2)

                # Read headers and rows
                result = await page.evaluate("""() => {
                    const tbl = document.querySelector('#id_transaction_entry_detail_table');
                    if (!tbl) return { headers: [], rows: [] };
                    const headers = Array.from(tbl.querySelectorAll('th[data-heading]'))
                        .map(h => h.getAttribute('data-heading').trim())
                        .filter(h => h !== '');
                    const rows = Array.from(tbl.querySelectorAll('tbody tr')).map(row => {
                        const cells = row.querySelectorAll('td');
                        return Array.from(cells).map(c =>
                            (c.getAttribute('data-value') !== null
                                ? c.getAttribute('data-value')
                                : c.innerText).trim()
                        );
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
                    "item"       : next((c for c in detail_df.columns if str(c).lower() == "description"), None)
                                   or next((c for c in detail_df.columns if "item" in str(c).lower()), None),
                    "last_price" : next((c for c in detail_df.columns if "last" in str(c).lower() and "price" in str(c).lower()), None),
                    "rate"       : next((c for c in detail_df.columns if str(c).strip().lower() == "rate"), None),
                    "cost_restr" : next((c for c in detail_df.columns if "cost" in str(c).lower() and "rest" in str(c).lower()), None),
                    "buy_price"  : next((c for c in detail_df.columns if "pricebook" in str(c).lower()), None)
                                   or next((c for c in detail_df.columns if "buying" in str(c).lower()), None),
                }

                # Get customer name
                party = str(voucher_num)
                for sel in ["[data-fieldname='PartyID']", ".party-name", "h4", "h3"]:
                    try:
                        txt = await page.locator(sel).first.inner_text(timeout=2_000)
                        if txt.strip():
                            party = txt.strip()
                            break
                    except Exception:
                        continue

                # Filter rows where Cost Restriction is empty
                if col_map["cost_restr"]:
                    low_cost = detail_df[
                        detail_df[col_map["cost_restr"]].isna() |
                        (detail_df[col_map["cost_restr"]].astype(str).str.strip().isin(["", "nan"]))
                    ]
                else:
                    low_cost = detail_df

                items = []
                for _, row in low_cost.iterrows():
                    try:
                        cost_price = float(str(row.get(col_map["buy_price"], 0) or 0).replace(",", ""))
                        landing    = round(cost_price * 1.05, 2)
                        sold_at    = float(str(row.get(col_map["rate"], 0) or 0).replace(",", ""))
                        prev_price = float(str(row.get(col_map["last_price"], 0) or 0).replace(",", ""))
                        margin     = round(((sold_at - landing) / sold_at * 100), 1) if sold_at > 0 else 0
                        items.append({
                            "item"       : str(row.get(col_map["item"], "")),
                            "cost_price" : cost_price,
                            "landing"    : landing,
                            "sold_at"    : sold_at,
                            "prev_price" : prev_price,
                            "margin"     : margin,
                        })
                    except Exception:
                        continue

                if items:
                    alerts.append({"voucher": f"SO : {voucher_num}", "party": party, "items": items})
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
