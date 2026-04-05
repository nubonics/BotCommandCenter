let latestLayout = [];
let fpsRequestInFlight = false;
let queuedFps = null;
let colsRequestInFlight = false;
let queuedCols = null;
let lastStatusTimer = null;

async function refresh() {
  try {
    // When the wall is mounted under /wall (as in BotCommandCenter),
    // the root-level /api/* pass-through routes exist, but they can
    // collide with other app routes and make debugging confusing.
    // Prefer a same-prefix API when available.
    const base = window.location.pathname.startsWith('/wall') ? '/wall' : '';
    const [statsResp, layoutResp] = await Promise.all([
      fetch(`${base}/api/stats`, { cache: 'no-store' }),
      fetch(`${base}/api/layout`, { cache: 'no-store' }),
    ]);

    const stats = await statsResp.json();
    latestLayout = await layoutResp.json();

    const fpsSlider = document.getElementById('fps-slider');
    const fpsValue = document.getElementById('fps-value');
    const colsSlider = document.getElementById('cols-slider');
    const colsValue = document.getElementById('cols-value');

    const targetFps = Number(stats.target_fps ?? fpsSlider.value ?? 0);
    const actualFps = Number(stats.actual_fps ?? 0);
    const gridCols = Number(stats.grid_cols ?? colsSlider.value ?? 0);

    if (fpsSlider && document.activeElement !== fpsSlider) {
      fpsSlider.value = String(targetFps);
    }
    if (colsSlider && document.activeElement !== colsSlider) {
      colsSlider.value = String(gridCols);
    }
    if (fpsValue) {
      fpsValue.textContent = `${targetFps} FPS`;
    }
    if (colsValue) {
      colsValue.textContent = `${gridCols} cols`;
    }

    document.getElementById('actual-fps').textContent = actualFps.toFixed(1).replace(/\.0$/, '');
    document.getElementById('window-count').textContent = stats.window_count ?? latestLayout.length;
    document.getElementById('frame-ms').textContent = stats.last_frame_ms ?? '-';
  } catch (err) {
    console.error(err);
  }
}

async function pushFps(fps) {
  queuedFps = Number(fps);
  if (fpsRequestInFlight) {
    return;
  }

  const base = window.location.pathname.startsWith('/wall') ? '/wall' : '';
  while (queuedFps !== null) {
    const next = queuedFps;
    queuedFps = null;
    fpsRequestInFlight = true;
    try {
      const resp = await fetch(`${base}/api/settings/fps`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fps: next }),
        cache: 'no-store',
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        const value = Number(data.target_fps);
        document.getElementById('fps-value').textContent = `${value} FPS`;
        setStatus(`Capture FPS set to ${value}.`);
      }
    } catch (err) {
      console.error(err);
      setStatus('Failed to change capture FPS.');
    } finally {
      fpsRequestInFlight = false;
    }
  }
}

async function pushCols(cols) {
  queuedCols = Number(cols);
  if (colsRequestInFlight) {
    return;
  }

  const base = window.location.pathname.startsWith('/wall') ? '/wall' : '';
  while (queuedCols !== null) {
    const next = queuedCols;
    queuedCols = null;
    colsRequestInFlight = true;
    try {
      const resp = await fetch(`${base}/api/settings/cols`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cols: next }),
        cache: 'no-store',
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        const value = Number(data.grid_cols);
        document.getElementById('cols-value').textContent = `${value} cols`;
        setStatus(`Columns set to ${value}.`);
      }
    } catch (err) {
      console.error(err);
      setStatus('Failed to change columns.');
    } finally {
      colsRequestInFlight = false;
    }
  }
}

function setStatus(text) {
  const el = document.getElementById('focus-status');
  el.textContent = text;
  if (lastStatusTimer) {
    clearTimeout(lastStatusTimer);
  }
  lastStatusTimer = setTimeout(() => {
    el.textContent = 'Ready.';
  }, 2500);
}

async function focusWindow(hwnd) {
  try {
    setStatus(`Focusing ${hwnd}...`);
    const base = window.location.pathname.startsWith('/wall') ? '/wall' : '';
    const resp = await fetch(`${base}/api/focus/${hwnd}`, {
      method: 'POST',
      cache: 'no-store',
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      setStatus(`Focus attempted for ${hwnd}.`);
      return;
    }
    const tile = latestLayout.find((x) => Number(x.hwnd) === Number(hwnd));
    setStatus(`Focused ${tile?.title || hwnd}.`);
  } catch (err) {
    console.error(err);
    setStatus('Focus failed.');
  }
}

function attachMosaicClick() {
  const mosaic = document.getElementById('mosaic');
  mosaic.addEventListener('click', async (event) => {
    if (!latestLayout.length) {
      return;
    }

    const rect = mosaic.getBoundingClientRect();
    const scaleX = mosaic.naturalWidth / rect.width;
    const scaleY = mosaic.naturalHeight / rect.height;
    const x = (event.clientX - rect.left) * scaleX;
    const y = (event.clientY - rect.top) * scaleY;

    const hit = latestLayout.find((tile) => {
      return (
        x >= Number(tile.x) &&
        x <= Number(tile.x) + Number(tile.width) &&
        y >= Number(tile.tile_top) &&
        y <= Number(tile.tile_top) + Number(tile.height)
      );
    });

    if (!hit) {
      return;
    }

    await focusWindow(Number(hit.hwnd));
  });
}

function attachSliders() {
  const fpsSlider = document.getElementById('fps-slider');
  const fpsValue = document.getElementById('fps-value');
  const colsSlider = document.getElementById('cols-slider');
  const colsValue = document.getElementById('cols-value');

  if (fpsSlider) {
    fpsSlider.addEventListener('input', () => {
      fpsValue.textContent = `${fpsSlider.value} FPS`;
      pushFps(Number(fpsSlider.value));
    });
  }

  if (colsSlider) {
    colsSlider.addEventListener('input', () => {
      colsValue.textContent = `${colsSlider.value} cols`;
      pushCols(Number(colsSlider.value));
    });
  }
}

attachMosaicClick();
attachSliders();
refresh();
setInterval(refresh, 1000);
