# OSRS DWM Dashboard

A small Windows-only Python dashboard that shows **live DWM thumbnails** for many OSRS clients (Official `osclient.exe` + RuneLite `runelite.exe`) and brings a client to the foreground when you click its tile.

This works even when all game windows are perfectly stacked on top of each other.

## Features

- Mix of Official + RuneLite windows
- Overview mode (40 tiny tiles) and paged modes (bigger tiles)
- Click tile to focus/restore the underlying window
- **No input simulation** (no automated clicks/keys)

## Requirements

- Windows 11 (Windows 10 should also work)
- Python 3.10+

## Install

```bash
pip install -r requirements.txt
```

## Run

### Preferred (DWM thumbnails)
```bash
python osrs_dashboard_dwm.py
```

### Fallback (PrintWindow capture)
If DWM thumbnails fail on your system, try this version. It attempts to capture each window even when stacked/covered.
```bash
python osrs_dashboard_printwindow.py
```

## Notes

- DWM thumbnails update live; there is no FPS capture loop.
- If Windows refuses to foreground the target window on click, try running the dashboard "as Administrator".
