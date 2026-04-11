# OSClient Wall

A Windows-focused wall view that is mounted inside the main BotCommandCenter FastAPI app.

It:

- finds visible `osclient.exe` windows
- captures them with `mss`
- scales them into a single live 1920x1080 mosaic
- serves the result at `/wall/` inside the main app

## Install

Use the project root dependencies:

```bash
pip install -r requirements.txt
```

## Run

Start the main app from the repository root:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open either:

```text
http://127.0.0.1:8000/wall/
http://127.0.0.1:8000/client-wall
```

## Environment variables

```text
TARGET_PROCESS=osclient.exe
BOARD_WIDTH=1920
BOARD_HEIGHT=1080
CAPTURE_FPS=15
JPEG_QUALITY=70
DISCOVERY_INTERVAL=1.0
WINDOW_PADDING=4
TITLE_BAR_HEIGHT=18
SHOW_TITLES=1
```

## Important limitation

`mss` captures what is actually visible on the active desktop.

That means:

- minimized windows are not usable
- hidden windows are not usable
- windows on other virtual desktops are not usable
- overlapped windows will show overlap

If you need per-window capture while hidden/minimized, that becomes a different project using Win32/DWM/Windows Graphics Capture instead of plain `mss`.
