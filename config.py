import os

# config.py

# --- Google Sheets ---
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID') 
WORKSHEET_NAME = 'Ad Scraper'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, 'creds.json')
if not os.path.exists(CREDENTIALS_FILE):
    alt_creds = r'C:\Users\muntaha\Desktop\final scraper video ads\creds.json.txt'
    if os.path.exists(alt_creds):
        CREDENTIALS_FILE = alt_creds

# --- Scraper settings ---
HEADLESS = False

WAIT_TIMEOUT = 20      # Seconds to wait for the video to load after clicking play
