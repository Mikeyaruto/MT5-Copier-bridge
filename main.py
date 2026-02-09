import json
import logging
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import cv2  # noqa: F401  # imported for fallback pipeline readiness
    import numpy as np  # noqa: F401
    from PIL import Image
    import pytesseract
except Exception:
    # OCR fallback stays optional; UIAutomator path is primary.
    pytesseract = None

ADB_PATH = os.environ.get("ADB_PATH", "adb")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "1.5"))
ANDROID_DUMP_PATH = "/sdcard/window_dump.xml"

BRIDGE_DIR = Path(r"C:\bridge")
SIGNAL_PATH = BRIDGE_DIR / "signal.json"
LOG_PATH = BRIDGE_DIR / "bridge.log"

# Tune this to what appears in PU Prime UI.
SYMBOL_PATTERN = re.compile(r"\b([A-Z]{3,7}(?:USD|JPY|EUR|GBP|AUD|NZD|CHF|CAD)?)\b")
LOT_PATTERN = re.compile(r"(?:lot|volume|vol)?\s*[:=]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
SIDE_PATTERN = re.compile(r"\b(BUY|SELL)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str
    lot: float


class SignalBridge:
    def __init__(self) -> None:
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(LOG_PATH),
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
        self.prev_positions: Counter = Counter()
        self.recent_signals: Dict[str, float] = {}
        self.recent_ttl_seconds = 5.0

    def run(self) -> None:
        logging.info("Starting bridge monitor.")
        self._ensure_adb_connected()

        while True:
            try:
                current_positions = self._read_live_positions()
                self._emit_deltas(current_positions)
            except Exception as exc:
                logging.exception("Polling cycle failed: %s", exc)
            time.sleep(POLL_SECONDS)

    def _ensure_adb_connected(self) -> None:
        result = self._run_adb(["devices"])
        if result.returncode != 0:
            raise RuntimeError(
                f"ADB is unavailable. Install Android platform-tools and ensure adb in PATH. stderr={result.stderr.strip()}"
            )

        if "\tdevice" not in result.stdout:
            raise RuntimeError(
                "No ADB device detected. Enable ADB for the emulator and verify `adb devices` shows one attached device."
            )
        logging.info("ADB connected device(s): %s", result.stdout.strip().replace("\n", " | "))

    def _read_live_positions(self) -> Counter:
        xml_text = self._dump_ui_xml()
        positions = self._extract_positions_from_xml(xml_text)

        # Fallback OCR when UI dump is missing/empty.
        if not positions:
            positions = self._extract_positions_from_screenshot()
            if positions:
                logging.info("Using OCR fallback for this cycle.")

        return Counter((p.symbol, p.side, p.lot) for p in positions)

    def _dump_ui_xml(self) -> str:
        dump_result = self._run_adb(["shell", "uiautomator", "dump", ANDROID_DUMP_PATH])
        if dump_result.returncode != 0:
            raise RuntimeError(f"uiautomator dump failed: {dump_result.stderr.strip()}")

        read_result = self._run_adb(["exec-out", "cat", ANDROID_DUMP_PATH])
        if read_result.returncode != 0 or "<?xml" not in read_result.stdout:
            raise RuntimeError(f"Unable to read UI XML: {read_result.stderr.strip()}")
        return read_result.stdout

    def _extract_positions_from_xml(self, xml_text: str) -> List[Position]:
        root = ET.fromstring(xml_text)
        chunks: List[str] = []

        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").strip()
            desc = (node.attrib.get("content-desc") or "").strip()
            if text:
                chunks.append(text)
            if desc:
                chunks.append(desc)

        return self._extract_positions_from_texts(chunks)

    def _extract_positions_from_screenshot(self) -> List[Position]:
        if pytesseract is None:
            return []

        with tempfile.TemporaryDirectory() as td:
            local_image = Path(td) / "screen.png"
            with open(local_image, "wb") as f:
                proc = subprocess.run(
                    [ADB_PATH, "exec-out", "screencap", "-p"],
                    stdout=f,
                    stderr=subprocess.PIPE,
                )
            if proc.returncode != 0:
                logging.warning("Screenshot fallback failed: %s", proc.stderr.decode(errors="ignore"))
                return []

            text = pytesseract.image_to_string(Image.open(local_image))
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return self._extract_positions_from_texts(lines)

    def _extract_positions_from_texts(self, texts: Iterable[str]) -> List[Position]:
        records: List[Position] = []
        joined = "\n".join(texts)

        # 1) Parse line-by-line strings that already include symbol/side/lot.
        for raw in list(texts):
            rec = self._extract_single_record(raw)
            if rec:
                records.append(rec)

        # 2) Parse multi-node combinations in proximity through sliding windows.
        tokens = [t for t in re.split(r"[\n|,;]", joined) if t.strip()]
        for idx in range(len(tokens)):
            window_text = " ".join(tokens[idx : idx + 6])
            rec = self._extract_single_record(window_text)
            if rec:
                records.append(rec)

        unique = []
        seen = set()
        for r in records:
            key = (r.symbol, r.side, r.lot)
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique

    def _extract_single_record(self, text: str) -> Optional[Position]:
        symbol_match = SYMBOL_PATTERN.search(text.upper())
        side_match = SIDE_PATTERN.search(text.upper())
        lot_match = LOT_PATTERN.search(text)

        if not (symbol_match and side_match and lot_match):
            return None

        symbol = symbol_match.group(1).upper()
        side = side_match.group(1).upper()
        lot = float(lot_match.group(1))

        if lot <= 0:
            return None

        return Position(symbol=symbol, side=side, lot=lot)

    def _emit_deltas(self, current: Counter) -> None:
        opens = current - self.prev_positions
        closes = self.prev_positions - current

        for (symbol, side, lot), count in opens.items():
            for _ in range(count):
                self._write_signal(
                    {
                        "action": "OPEN",
                        "symbol": symbol,
                        "side": side,
                        "lot": lot,
                    }
                )

        for (symbol, _, _), count in closes.items():
            for _ in range(count):
                self._write_signal({"action": "CLOSE", "symbol": symbol})

        self.prev_positions = current

    def _write_signal(self, payload: Dict) -> None:
        signature = json.dumps(payload, sort_keys=True)
        now = time.time()

        # Clean old entries.
        self.recent_signals = {
            sig: ts for sig, ts in self.recent_signals.items() if now - ts <= self.recent_ttl_seconds
        }

        if signature in self.recent_signals:
            return

        temp_path = SIGNAL_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temp_path, SIGNAL_PATH)

        self.recent_signals[signature] = now
        logging.info("Signal emitted: %s", payload)

    def _run_adb(self, args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [ADB_PATH, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )


if __name__ == "__main__":
    bridge = SignalBridge()
    bridge.run()
