# Bot Command Center

Bot Command Center (BCC) is a FastAPI browser app for operating and tracking OSRS bot accounts in one place.

It started with money maker planning, and now also includes account operations, wall visibility, account progress, P&L tracking, global cost allocation, watchdog tooling, and window spreader controls.

## What it does

- Manage OSRS accounts, credentials, proxies, notes, tags, and status
- Import account data from Botting Hub
- Track account progress, goals, revenue, and expenses
- View account P&L and global cost allocation
- Monitor account hygiene through the Action Center
- Match accounts to live client wall windows and focus/clear wall hints
- Use planner tools for progression and money maker planning
- Refresh live item pricing from the OSRS Wiki price API
- Run watchdog and window spreader workflows from the web UI

## Tech

- FastAPI
- Jinja2 templates
- SQLAlchemy ORM
- SQLite
- httpx
- psutil

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

## Main areas

- **Accounts**: account records, proxy details, bulk updates, wall status
- **Account P&L**: tracked revenue, cost, and monthly net per account
- **Action Center**: data gaps, hygiene issues, and system-health summary
- **Client Wall**: live window matching and wall ops alerts
- **Planner**: planning and progression workflows
- **Items / Money Makers**: OSRS item data and money maker calculations
- **Global Costs**: shared cost allocation across accounts
- **Watchdog / Spreader**: operations utilities for running the farm

## Notes

This app still contains older planner-oriented naming in a few places because the project grew out of an earlier money-maker planner. The current product direction is Bot Command Center.
