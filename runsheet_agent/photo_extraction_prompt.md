# Runsheet Photo Extraction — Prompt + Schema (v1.1)

v1.1: validated against SO 42183 p1 — box, all 4 round items
(incl. circled+struck), pallet and notes extracted correctly.
Two fixes from that run: salesman must come from the subject
document's FOOTER (a Salesman line above the letterhead belongs
to a stacked document), and unclear page digits → null.

Designed and validated against 5 real annotated Sales Orders
(SO 42167 p2, SO 42167 p1, SO 42175, SO 42183 p2, SO 42183 p1).
The API call sends ONE photo per request with this system prompt;
multi-page orders are merged afterwards by so_no.

---

## System prompt (send verbatim, image attached as user content)

You are reading a photo of an annotated SALES ORDER from Sri Ambikas
Pte Ltd's warehouse. The paper carries handwritten marks made by the
picking team. Extract ONLY what is asked below and return STRICT JSON
(no markdown, no commentary).

CONTEXT — how the warehouse annotates:
- A TICK on a line means picked (you do NOT need to list ticked lines).
- A LINE STRUCK THROUGH horizontally means NOT supplied.
- A CIRCLE around the serial number (S/No) marks a ROUND ITEM
  (bulk bag) — these are the ONLY item lines you must extract.
- A circle around anything else (a rate, a quantity elsewhere) is NOT
  a round-item mark. Only circles at the serial number count.
- A stamped rectangular box ("BILL READER NAME") contains: the
  picker/reader's handwritten name, date, from/to times, and counts:
  CARTON, LOOSE ITEMS, TOTAL, PALLETED BY.
- Handwritten margin notes may include a pallet number like "PA-2"
  and delivery instructions like "Please call salesperson before
  delivery", or carton tallies like "4-CTN".
- The photo may show edges of OTHER documents (a sheet stacked above
  or behind). Extract ONLY from the document whose printed header
  ("SALES ORDER", SO NO., Customer) is the main subject. Ignore
  content that belongs to a different page's totals visible at the
  photo's edge. SPECIFICALLY: a "Salesman:" line or Sub Total/GST
  block appearing ABOVE the subject document's letterhead belongs to
  ANOTHER document — never take the salesman from there. The subject
  document's salesman is printed in its FOOTER, at the bottom left,
  below the item table.

Return JSON exactly in this shape:

{
  "so_no": "42183",                 // printed SO NO.
  "so_date": "28/07/2026",          // printed Date (DD/MM/YYYY)
  "customer": "JW/A.V.N STORE",     // printed Customer line
  "area": "JURONG WEST",            // printed Area, "" if not visible
  "salesman": "JEGAN",              // printed Salesman, "" if not visible
  "page": "1/2",                    // printed Page number as N/M;
                                    // if either digit is unclear use
                                    // null and flag in "uncertain"
  "box": {                          // the stamped box; null if absent
    "reader_name": "V.THESA",       // handwritten name
    "date": "28-7-2026",
    "time_from": "1:56", "time_to": "2:26",
    "carton": 5, "loose": 6, "total": 11,   // numbers; null if blank
    "palleted_by": "VIKI"
  },
  "round_items": [                  // ONLY circled-serial lines
    {
      "serial": 1,
      "item": "OOTY GOLD PONNI PARBOILED RICE - 5KG X 6",  // full printed name
      "qty": 12, "uom": "PCS",      // printed quantity + UOM
      "struck": false               // true if the line is ALSO struck out
    }
  ],
  "pallet_no": "PA-2",              // handwritten PA-x, "" if none
  "notes": ["Please call salesperson before delivery", "4-CTN"],
                                    // handwritten margin notes, [] if none
  "uncertain": ["box.loose unclear — could be 4 or 6"]
                                    // anything you could not read with
                                    // confidence; [] if fully confident
}

RULES:
- round_items: include circled lines EVEN IF also struck (struck:true).
  Do not include ticked or plain lines.
- Numbers: use the PRINTED quantity, not handwritten corrections,
  unless a handwritten number clearly replaces it — then use the
  handwritten one and add a note to "uncertain".
- If the stamped box is absent on this page, "box": null.
- If a field is unreadable, use null/"" and describe it in "uncertain".
- Output must be valid JSON. Nothing else.

---

## Post-extraction mapping (server side, not the model's job)

- Merge photos with the same so_no (union round_items; take box from
  whichever page has it; concatenate notes).
- Day-book lookup by so_no against base_link_doc → invoice no,
  customer verification. Not found → manual (amber) row, photo data
  stands alone.
- Runsheet row:
  - TAKEN BY  = box.reader_name          (picker, NOT the salesman)
  - Other ctn = box.carton + box.loose   (loose-item cartons are
                                          still cartons; no split)
  - Round items → frequent column if the item name matches a mapped
    column, else all-round matrix. Pieces from qty; ctn conversion
    via items master qty_per_ctn (fallback: the pack size printed in
    the item name, e.g. "5KG X 6" → 6/ctn).
  - struck round items → excluded, shown in review as "marked round
    but not supplied".
- pallet_no + notes → review screen; NOTES printing on the sheet is
  pending a template decision (v2 layout has no notes area).
- EVERYTHING lands on a review screen (photo beside extraction)
  before touching the grid. Nothing auto-commits.

## Expected outputs for two of the test photos

SO 42183 page 1 (image 5) →
  box: V.THESA, 1:56–2:26, carton 5, loose 6, total 11, VIKI
  round_items: [ {1, OOTY GOLD PONNI PARBOILED RICE - 5KG X 6, 12, PCS, false},
                 {2, OOTY GOLD PARBOILED PONNI RICE - 10KG X 3, 6, PCS, false},
                 {4, PILLSBURY ATTA - 2kg X 10, 1, CTN, true},
                 {5, PILLSBURY FLOUR (ATTA) - 5kg X 4, 1, CTN, false} ]
  pallet_no: PA-2
  notes: ["Please call sales person before delivery", "4-CTN"]

SO 42167 page 1 (image 2) →
  box: Shobana, 28/07/26, 1:45–2:13, carton 7, loose 4, total 11, M.Nan
  round_items: [ {8, PILLSBURY ATTA - 2kg X 10, 1, CTN, true} ]
  pallet_no: PA-29
  notes: ["6 ctn"]
