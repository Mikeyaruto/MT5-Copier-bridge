# PU Prime emulator position reader

This program observes the **open positions screen** of a signed-in PU Prime Android emulator using ADB UIAutomator. It does not log in or collect a password. It writes `C:\bridge\events.jsonl`, which is the input to the companion [MT5-Copier](https://github.com/Mikeyaruto/MT5-Copier) project. Both Python programs must run on the same Windows machine or share this file reliably.

## Setup

1. Install Python 3.10+ and Android Platform Tools; make `adb` available on `PATH`.
2. Start the emulator, open PU Prime's **Positions** page, and verify `adb devices` reports one connected device.
3. In this repository, run `python main.py`. The first two valid snapshots establish a baseline; existing trades are **not** copied.
4. Start `python run_bridge.py` from the MT5-Copier repository. Its `config.yaml` feed path defaults to `C:/bridge/events.jsonl`. Follow that repository's `setup.md` for EA installation.
5. On an MT5 **demo account**, compile and attach the EA, then deliberately set `EnableExecution=true` with a small `MaxLots` cap. Check the MT5 account number and mapped broker symbols before creating a small source trade.

This reader intentionally does not interpret a missing or unrecognised screen as closed positions. It needs a visible `Positions` label and an explicit `No open positions` label for an empty list. Rows need a recognised symbol, BUY/SELL, and a volume labelled `lot`, `lots`, `vol`, or `volume`. If your actual app uses different wording, set `POSITION_SCREEN_MARKER`, `EMPTY_POSITION_MARKER`, and `SOURCE_SYMBOLS` as needed. **Check the XML structure with a demo account before relying on this reader.** It does not automate navigation, interpret trades off-screen, or guarantee the UI contains ticket IDs.

For example, in PowerShell:

```powershell
$env:SOURCE_SYMBOLS="XAUUSD,EURUSD,GBPUSD,USDJPY"
$env:POSITION_SCREEN_MARKER="Positions"
$env:EMPTY_POSITION_MARKER="No open positions"
python main.py
```

`ADB_PATH`, `POLL_SECONDS` (default 1.5), `CONFIRM_POLLS` (minimum 2), `BRIDGE_DIR`, and `EVENTS_PATH` are also configurable. Keep the state file and JSONL file together with regular backups. Do not clear `source_state.json`, `events.jsonl`, or its `.offset` file during operation; recovering or resynchronising requires checking source positions against MT5 positions first.

Each line contains a distinct, replay-safe event with `event_id`, `event`, `source`, `source_trade_id`, `symbol`, `side`, `lots`, `sl`, `tp`, and `timestamp`. Source trade IDs use visible ticket IDs if available, otherwise an assigned identity retained in `source_state.json`. With identical positions and no visible ticket ID, a close can be assigned to either equal position. Do not use this design for partial closes or position edits until the actual UI provides the required identifiers and update events.

If the page cannot be parsed, the reader logs an invalid snapshot in `C:\bridge\bridge.log` and emits no action. Validate a real PU Prime XML capture before going beyond the demo test. No screenshot OCR is attempted; uncertain OCR readings are unsafe for automatic orders.
