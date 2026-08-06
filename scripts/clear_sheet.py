import sys
from config.settings import get_settings
from tools.sheets.sheets_sync import _get_service, _tab

def clear_sheet():
    settings = get_settings()
    svc = _get_service()
    sid = settings.google_sheet_id
    tab = _tab()
    
    print(f"Clearing sheet {tab} A2:N...")
    try:
        svc.spreadsheets().values().clear(
            spreadsheetId=sid,
            range=f"{tab}!A2:N"
        ).execute()
        print("Google Sheet cleared.")
    except Exception as e:
        print(f"Error clearing sheet: {e}")

if __name__ == "__main__":
    clear_sheet()
