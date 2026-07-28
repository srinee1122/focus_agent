"""
credentials.py — Reads login from credentials.xlsx
"""
import pandas as pd
import os
import sys

def get_credentials(filepath: str) -> tuple[str, str]:
    if not os.path.exists(filepath):
        print(f"❌ credentials.xlsx not found at '{filepath}'")
        print("   Create it with columns: username | password")
        sys.exit(1)
    try:
        df = pd.read_excel(filepath)
    except Exception as e:
        print(f"❌ Error reading credentials file: {e}")
        sys.exit(1)
    df.columns = df.columns.str.lower()
    if not {"username", "password"}.issubset(df.columns):
        print("❌ credentials.xlsx must have columns: username and password")
        sys.exit(1)
    row = df.iloc[0]
    username = str(row["username"]).strip()
    password = str(row["password"]).strip()
    print(f"✅ Credentials loaded for: {username}")
    return username, password
