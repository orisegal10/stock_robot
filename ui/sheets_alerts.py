"""
Google Sheets bridge for the stocks_alert bot rules.

The stocks_alert bot reads its alert rules from a Google Sheet with columns:
  A: Ticker | B: Formula | C: Message | D: Interval (min) | E: Active (Yes/No)

This module lets the dashboard read and update that same sheet, so alert
rules can be managed straight from the Trading Command Center.
"""
import os
from typing import List

import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "/app/google_credentials.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SHEET_RANGE = os.getenv("SHEET_RANGE", "Sheet1!A2:E100")

COLUMNS = ["Ticker", "Formula", "Message", "Interval (min)", "Active"]


def is_configured() -> bool:
    return bool(SPREADSHEET_ID) and os.path.exists(CREDENTIALS_FILE)


def _values():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds).spreadsheets().values()


def load_alert_rules() -> pd.DataFrame:
    result = _values().get(spreadsheetId=SPREADSHEET_ID, range=SHEET_RANGE).execute()
    rows = result.get("values", [])
    padded = [
        (row + [""] * 5)[:5]
        for row in rows
        if any(str(cell).strip() for cell in row)
    ]
    return pd.DataFrame(padded, columns=COLUMNS)


def append_alert_rule(ticker: str, formula: str, message: str,
                      interval: int, active: bool) -> None:
    _values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=SHEET_RANGE,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[
            ticker.strip().upper(),
            formula.strip(),
            message.strip(),
            str(interval),
            "Yes" if active else "No",
        ]]},
    ).execute()


def save_alert_rules(df: pd.DataFrame) -> None:
    """Overwrite the whole sheet range with the given rules (edit/delete)."""
    values: List[List[str]] = df.fillna("").astype(str).values.tolist()
    values = [row for row in values if any(cell.strip() for cell in row)]
    _values().clear(spreadsheetId=SPREADSHEET_ID, range=SHEET_RANGE).execute()
    if values:
        _values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=SHEET_RANGE,
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
