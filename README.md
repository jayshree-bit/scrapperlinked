# Prospect Verifier

## Setup
    pip install -r requirements.txt
    playwright install chromium
    # edit .env  (set CHOCODATA_PROFILE_PATH from the provider docs; rotate the API key)

## Run the web UI
    python -m app.web        # open http://127.0.0.1:5000
Upload any .xlsx/.xls/.csv with LinkedIn profile URLs, press "Start scraping",
watch results come in, then "Download Excel". Needs MongoDB running (MONGODB_URI).

## Run (provider route)
    python -m app.cli probe https://www.linkedin.com/in/jay-kumar-479861443/   # check raw JSON, adjust map_profile in app/provider.py
    python -m app.cli import-file sample_prospects.xlsx
    python -m app.cli requeue
    python -m app.cli process --limit 1
    python -m app.cli export

## Run (manual route, no provider)
    python -m app.browser_status        # sign in once (LinkedIn email+password)
    python -m app.cli import-file sample_prospects.xlsx
    python -m app.cli requeue
    python -m app.cli review

## Own connections export (free)
    python -m app.cli import-details Connections.csv

Keep .env private; it holds your API key.
