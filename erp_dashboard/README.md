# ERP Agent Dashboard

Web-based control centre for all ERP automation agents.

---

## Setup (one time)

```
pip install -r requirements.txt
```

The dashboard must be in the same parent folder as focus_agent:
```
parent_folder/
├── focus_agent/        ← existing agent code
│   ├── focus_scraper.py
│   ├── formatter.py
│   └── ...
└── erp_dashboard/      ← this folder
    ├── main.py
    ├── database.py
    ├── frontend/
    └── requirements.txt
```

---

## Run

```
cd erp_dashboard
uvicorn main:app --reload --port 8000
```

Then open your browser at:
**http://localhost:8000**

---

## Features

| Section | What it does |
|---|---|
| **Agents** | See status, run now, stop, set interval, enable schedule |
| **Live Logs** | Real-time output streamed via WebSocket |
| **Price Book** | Per-item landing cost % — override the default 5% |
| **Settings** | WhatsApp groups, Focus URL, credentials path, schedules |
