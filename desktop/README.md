# Trade Fix Radio desktop app

## Two modes

| Where the exe runs | What happens |
| --- | --- |
| Anywhere (a listener's machine) | **Installed mode.** First run installs everything into `%LOCALAPPDATA%\TradeFixRadio` (override with `TFR_INSTALL_DIR`), then plays. See `bootstrap.py`. |
| Inside a checkout with a `.venv` | **Checkout mode.** Runs the station from that checkout; ACE-Step from the directory in `.env`. |

Installed-mode steps, each marked done in `state/` so a retry resumes: uv → project source
(GitHub zip) → bundled UI → Python 3.10 venv + station → FFmpeg → *(GPU ≥ 6 GB only)*
ACE-Step source → Python 3.12 env (torch excluded) → torch CUDA wheel (resumable, sha256
checked) → models via `acestep-download` (retried until present) → `.env` with the Windows
default output device → database. Logs: `logs/install.log`, `logs/desktop-app.log`,
`logs/desktop.log`, `logs/acestep.log` under the install folder.


The Control Center runs in its own native window (Edge WebView2 inside this process),
with the Trade Fix Radio icon. No browser is opened.

| File | Purpose |
| --- | --- |
| `app.py` | Starts `tradefix dev`, waits for the API, shows the UI in a native window; closing the window stops the station |
| `make_icon.py` | Regenerates `tradefix.ico`, `tradefix.png` and `frontend/public/favicon.ico` |
| `tradefix.ico` | App icon used by the window, taskbar and shortcuts |

## Launch

* Double-click **Trade Fix Radio.lnk** (project root or Desktop) — no console.
* Or **Trade Fix Radio.bat** — same, with a console for logs.
* Or from a terminal, with options:

```powershell
.\.venv\Scripts\python desktop\app.py --scenario violent_breakout --port 8000
```

Station output goes to `logs/desktop.log`. If something is already listening on the
port, the window attaches to it instead of starting a second station.

Requires the project set up as in the main README (`.venv` on Python 3.10, frontend built)
plus `pywebview` and `pillow` in the venv.

## Sound

`TRADEFIX_AUDIO__DEVICE_NAME=default` (what the installer writes) follows the Windows default
output: the sink asks Windows every two seconds whether the default endpoint changed and, if so,
re-opens on the new one, so switching speakers to headphones moves the music within a couple of
seconds. A pinned device that is missing at start falls back to the default with a warning.

Development mode defaults to a silent `null_sink`. The project-root `.env` switches it to a
real device (`TRADEFIX_AUDIO__SINK=sounddevice`). Pick the device with
`TRADEFIX_AUDIO__DEVICE_NAME` (a substring from `tradefix audio devices`) and keep
`TRADEFIX_AUDIO__DEVICE_HOST_API=MME`: on this machine DirectSound's write does not block, so
audio runs ~100x too fast and is inaudible, and WASAPI needs `TRADEFIX_AUDIO__SAMPLE_RATE=48000`.
Change the device, then relaunch the app.

## Real music (ACE-Step)

With `TRADEFIX_GENERATION__PROVIDER=ace_step` in `.env`, the launcher also starts the
ACE-Step API server (`uv run acestep-api --no-init` in `D:\ace-step`, the directory named by
`TRADEFIX_GENERATION__ACE_STEP__WORKER_DIRECTORY`), waits for it, then starts the station,
which loads the configured models over the API. Both are stopped when the window closes.
Server output goes to `logs/acestep.log`.

ACE-Step lives outside the repo in its own Python 3.12 environment (`uv sync` there) and
needs its checkpoints downloaded once: `tradefix models install ace-step --yes` (about 6 GB).
The first model load after a cold start takes a minute or more; the station holds playout
until fresh tracks exist, so expect a short wait before music starts.

## Why the shortcuts run `python.exe --hide-console`, not `pythonw.exe`

Other tooling on this machine restarts itself by stopping every `pythonw.exe`, which
twice killed the app window while the station kept playing underneath. The shortcuts
therefore run the venv's `python.exe` with `--hide-console`, which hides the console at
startup; the process is then `python.exe` and not caught by that sweep. The station and
ACE-Step are also placed in a Windows job object, so if the launcher is ever ended from
outside, they end with it instead of running headless. The launcher's own log is
`logs/desktop-app.log`.

## Live market prices

Both modes default to `TRADEFIX_MARKET__FEED=public` (`tradefix_radio/market/feeds/public.py`):
gold quoted second by second from Binance PAXG ticks anchored to spot gold from gold-api.com
(spot updates about once a minute, so its level with PAXG's motion), Yahoo Finance one-minute
history for gold, and Bitcoin from Binance.
No account or key. History is replayed through the feature engine at startup so the regime is
classified within seconds instead of an hour, and the current price shows in the top bar.
Gold is closed from Friday evening to Sunday evening New York time; the market router then
plays against Bitcoin and returns to gold when it reopens. `metatrader5` (a logged-in terminal)
and `simulated` remain available.

The loading page stays up — with a progress bar fed by the buffer — until a generated track is
on air, so the first thing the Control Center shows is music.
