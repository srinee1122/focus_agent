# Focus ERP Low Price Monitoring Agent 🤖

Automatically detects sales orders with pricing issues and sends WhatsApp alerts.

---

## 📁 Files
```
focus_agent/
├── main.py               ← Run this for production
├── test_phase1.py        ← Run this for testing (no WhatsApp send)
├── config.py             ← Your settings (edit this first)
├── credentials.py        ← Reads login from Excel
├── focus_scraper.py      ← Focus ERP browser automation
├── whatsapp_sender.py    ← Sends WhatsApp messages
├── formatter.py          ← Formats the WhatsApp message
├── excel_exporter.py     ← Saves Excel backup of alerts
├── requirements.txt      ← Python dependencies
└── credentials.xlsx      ← YOU CREATE THIS (see below)
```

---

## ⚙️ First-Time Setup

### 1. Install Python
Download from https://python.org (3.11+). Check ✅ "Add Python to PATH".

### 2. Open CMD in this folder
Click the address bar → type `cmd` → Enter

### 3. Install dependencies
```
pip install -r requirements.txt
python -m playwright install chromium
```

### 4. Create credentials.xlsx
Create an Excel file named `credentials.xlsx` in this folder:

| username              | password    |
|-----------------------|-------------|
| your@email.com        | yourpassword |

⚠️ If password starts with @, type an apostrophe first: `'@yourpassword`

### 5. Edit config.py
- Set `WHATSAPP_RECIPIENTS` to your phone number with country code

---

## ▶️ Running

```bash
# Test run (preview WhatsApp messages, no actual send)
python test_phase1.py

# Full run (scrape + Excel + WhatsApp)
python main.py --now

# Schedule daily at time set in config.py
python main.py
```

## 📱 First WhatsApp Run
On first run a browser opens → scan the QR code in WhatsApp on your phone
(WhatsApp → Linked Devices → Link a Device). Session saved permanently after that.

---

## 📊 Output
- **WhatsApp**: One message per flagged Sales Order sent to configured numbers
- **Excel backup**: `downloads/LowPrice_Alert_YYYY-MM-DD.xlsx` — always saved
