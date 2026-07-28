# ============================================================
#  FOCUS ERP AGENT — CONFIGURATION
#  Edit this file before running.
# ============================================================

# --- Focus ERP ---
FOCUS_URL = "https://ymt-9.focus9erp.com/focusx"

# --- Credentials Excel file ---
# Place credentials.xlsx in the same folder with columns: username | password
CREDENTIALS_FILE = "credentials.xlsx"

# --- WhatsApp group name ---
# Must match the group name EXACTLY as it appears in WhatsApp
# Add as many group names as needed — must match exactly as shown in WhatsApp
WHATSAPP_GROUPS = [
    "Test Low Price",     # ← Group 1
    # "Sales Team",          # ← uncomment and add more groups here
    # "Management",
]
# Backward compatibility
WHATSAPP_GROUP = WHATSAPP_GROUPS[0]

# --- Schedule ---
# Time to run daily (24-hour format)
SCHEDULE_TIME = "08:00"

# --- Download folder ---
DOWNLOAD_DIR = "downloads"

# --- WhatsApp persistent session ---
# Scan QR once — session saved here forever
WHATSAPP_SESSION_DIR = "whatsapp_session"

# --- Browser visibility ---
# False = visible (for testing), True = runs invisibly in background
HEADLESS = False
