# PU Prime Android Emulator Bridge (Windows)

## 1) Install prerequisites

1. **Python 3.10+** (64-bit) from https://www.python.org/downloads/windows/
   - During install, check **Add Python to PATH**.
2. **Android Platform Tools (ADB)** from Google:
   - https://developer.android.com/tools/releases/platform-tools
   - Extract (example): `C:\Android\platform-tools`
3. **Tesseract OCR** (optional fallback, recommended):
   - Install from https://github.com/UB-Mannheim/tesseract/wiki
   - Add Tesseract install folder to PATH (for example `C:\Program Files\Tesseract-OCR`).

## 2) Prepare emulator ADB

1. Start your Android emulator (BlueStacks/LDPlayer/Nox/MEmu, etc).
2. Enable ADB in emulator settings.
3. In PowerShell/CMD:
   ```powershell
   adb devices
   ```
   You should see one device in `device` state.

> If `adb` is not recognized, add your platform-tools directory to PATH or run with full path.

## 3) Install Python dependencies

In project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 4) Run the bridge

```powershell
python main.py
```

The script will create:

- `C:\bridge\signal.json`
- `C:\bridge\bridge.log`

## 5) Signal format

OPEN example:
```json
{"action":"OPEN","symbol":"XAUUSD","side":"BUY","lot":0.1}
```

CLOSE example:
```json
{"action":"CLOSE","symbol":"XAUUSD"}
```

## 6) Notes / tuning

- The primary parser uses **ADB UIAutomator XML** from current screen.
- If no trades are detected from XML, it falls back to screenshot OCR.
- If your emulator uses a custom ADB path:
  ```powershell
  set ADB_PATH=C:\Android\platform-tools\adb.exe
  python main.py
  ```
- Polling interval default is 1.5s, can be changed:
  ```powershell
  set POLL_SECONDS=1.0
  python main.py
  ```

