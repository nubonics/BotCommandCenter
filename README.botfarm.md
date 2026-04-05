# BotFarmPlanner

A FastAPI browser app for building and tracking OSRS money making methods using live item data from the OSRS Wiki price API.

## Features

- Browser GUI served by FastAPI
- SQLite database
- Live OSRS item refresh from the Wiki price API
- High alch values stored from live item mapping data
- Create money makers with inputs and outputs
- Profit per hour calculation
- Per-money-maker refresh

## Tech

- FastAPI
- Jinja2 templates
- SQLModel
- SQLite
- httpx

## Run

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

## First steps

1. Go to **Items**.
2. Click **Refresh All Items**.
3. Create a money maker.
4. Add input and output items.
5. Use valuation mode `high_alch` for output lines that should use high alchemy value instead of market price.

## Notes

This app uses the OSRS Wiki / RuneLite real-time price API endpoints such as `/mapping` and `/latest`, and it sends a custom `User-Agent`, which the API expects. FastAPI + SQLite follows the official documented patterns for a lightweight SQL-backed app. citeturn241255search1turn241255search4turn364419search0
