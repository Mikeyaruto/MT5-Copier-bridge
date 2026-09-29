"""Conservative, durable source position reader for MT5-Copier.

Validate UI selectors on the user's actual PU Prime positions screen before use.
"""
import json
import logging
import os
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

ADB_PATH = os.environ.get("ADB_PATH", "adb")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "1.5"))
CONFIRM_POLLS = max(2, int(os.environ.get("CONFIRM_POLLS", "2")))
SCREEN_MARKER = os.environ.get("POSITION_SCREEN_MARKER", "Positions").casefold()
EMPTY_MARKER = os.environ.get("EMPTY_POSITION_MARKER", "No open positions").casefold()
BRIDGE_DIR = Path(os.environ.get("BRIDGE_DIR", r"C:\bridge"))
EVENTS_PATH = Path(os.environ.get("EVENTS_PATH", str(BRIDGE_DIR / "events.jsonl")))
STATE_PATH = BRIDGE_DIR / "source_state.json"
LOG_PATH = BRIDGE_DIR / "bridge.log"
ANDROID_DUMP_PATH = "/sdcard/window_dump.xml"

SYMBOL_RE = re.compile(r"\b([A-Z]{3,12}(?:\.[A-Z0-9]+)?)\b")
SIDE_RE = re.compile(r"\b(BUY|SELL)\b", re.I)
LOT_RE = re.compile(r"\b(?:lots?|volume|vol)\s*[:=]?\s*(\d+(?:\.\d+)?)\b", re.I)
TICKET_RE = re.compile(r"\b(?:ticket|position\s*id|order\s*id|#)\s*[:#]?\s*(\d{5,})\b", re.I)
RESERVED = {"BUY", "SELL", "POSITION", "POSITIONS", "VOLUME", "LOT", "LOTS", "PRICE", "PROFIT", "LOSS"}


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str
    lot: float
    ticket: Optional[str] = None


class SignalBridge:
    def __init__(self):
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(LOG_PATH), level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(message)s")
        self._repair_partial_tail()
        self.state = self._load_state()
        if self.state is None and EVENTS_PATH.exists() and EVENTS_PATH.stat().st_size:
            raise RuntimeError("Source state is missing while events exist; reconcile before restarting")
        self.candidate = None
        self.candidate_count = 0

    def _repair_partial_tail(self):
        if not EVENTS_PATH.exists():
            return
        with EVENTS_PATH.open("rb+") as handle:
            content = handle.read()
            if content and not content.endswith(b"\n"):
                last_line = content[content.rfind(b"\n") + 1:]
                try:
                    json.loads(last_line)
                except (ValueError, UnicodeDecodeError):
                    handle.truncate(content.rfind(b"\n") + 1)
                    logging.warning("Removed incomplete trailing event")
                else:
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())

    def _load_state(self):
        if not STATE_PATH.exists():
            return None
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))

    def _save_state(self, state):
        temp = STATE_PATH.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, STATE_PATH)

    def _run_adb(self, args):
        return subprocess.run([ADB_PATH, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15, check=False)

    def _read_live_positions(self) -> List[Position]:
        proc = self._run_adb(["shell", "uiautomator", "dump", ANDROID_DUMP_PATH])
        if proc.returncode:
            raise RuntimeError(f"UI dump failed: {proc.stderr.strip()}")
        proc = self._run_adb(["exec-out", "cat", ANDROID_DUMP_PATH])
        if proc.returncode or "<hierarchy" not in proc.stdout:
            raise RuntimeError("UI hierarchy unavailable")
        return self._extract_positions_from_xml(proc.stdout)

    def _extract_single_record(self, text):
        side = SIDE_RE.search(text)
        lot = LOT_RE.search(text)
        allowed = {s.strip() for s in os.environ.get(
            "SOURCE_SYMBOLS", "XAUUSD,EURUSD,GBPUSD,USDJPY").upper().split(",")}
        symbols = [w.group(1) for w in SYMBOL_RE.finditer(text.upper())
                   if w.group(1) not in RESERVED and w.group(1) in allowed]
        if not (side and lot and len(set(symbols)) == 1):
            return None
        volume = float(lot.group(1))
        if not 0 < volume <= 100:
            return None
        ticket = TICKET_RE.search(text)
        return Position(symbols[0], side.group(1).upper(), volume,
                        ticket.group(1) if ticket else None)

    def _extract_positions_from_xml(self, xml_text):
        root = ET.fromstring(xml_text)
        labels = [(n.attrib.get("text") or n.attrib.get("content-desc") or "").strip()
                  for n in root.iter("node")]
        if not any(SCREEN_MARKER in label.casefold() for label in labels):
            raise RuntimeError("Positions screen marker absent; refusing to infer closures")

        def rows(node):
            children = list(node)
            combined = " ".join([node.attrib.get("text", ""), node.attrib.get("content-desc", "")]
                                + [label for child in children
                                   for label in (child.attrib.get("text", ""),
                                                 child.attrib.get("content-desc", ""))])
            child_rows = [record for child in children for record in rows(child)]
            if child_rows:
                return child_rows
            record = self._extract_single_record(combined)
            return [record] if record else []
        records = rows(root)
        if not records and not any(EMPTY_MARKER in label.casefold() for label in labels):
            raise RuntimeError("No parsed trades and no explicit empty-state label")
        return records

    @staticmethod
    def _key(position):
        return f"{position.ticket or ''}|{position.symbol}|{position.side}|{position.lot}"

    def _reconcile(self, positions):
        previous = self.state["positions"] if self.state else []
        pools = defaultdict(deque)
        for item in previous:
            pools[item["key"]].append(item)
        current = []
        occurrences = defaultdict(int)
        for position in positions:
            key = self._key(position)
            occurrence = occurrences[key]
            occurrences[key] += 1
            # Stable across a crash after appending, before saving the snapshot.
            sequence = self.state["sequence"] + 1 if self.state else 0
            item = pools[key].popleft() if pools[key] else {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                     f"puprime:{sequence}:{key}:{occurrence}")),
                "key": key, **asdict(position)
            }
            current.append(item)
        closes = [item for pool in pools.values() for item in pool if item.get("tracked", True)]
        old_ids = {item["id"] for item in previous}
        opens = [item for item in current if item["id"] not in old_ids]
        return current, opens, closes

    def observe(self, positions):
        keys = tuple(sorted(self._key(p) for p in positions))
        self.candidate_count = self.candidate_count + 1 if keys == self.candidate else 1
        self.candidate = keys
        if self.candidate_count < CONFIRM_POLLS:
            return
        current, opens, closes = self._reconcile(positions)
        if self.state is None:
            for item in current:
                item["tracked"] = False
            self.state = {"sequence": 0, "positions": current}
            self._save_state(self.state)
            logging.info("Initial snapshot saved without copying existing positions")
            return
        if not opens and not closes:
            # Untracked baseline positions can disappear without a trade event.
            if len(current) != len(self.state["positions"]):
                next_state = {"sequence": self.state["sequence"], "positions": current}
                self._save_state(next_state)
                self.state = next_state
            return
        sequence = self.state["sequence"] + 1
        timestamp = datetime.now(timezone.utc).isoformat()
        events = [{"event": "CLOSE", "source_trade_id": item["id"], "symbol": item["symbol"],
                   "side": item["side"], "lots": item["lot"]} for item in closes]
        events += [{"event": "OPEN", "source_trade_id": item["id"], "symbol": item["symbol"],
                    "side": item["side"], "lots": item["lot"]} for item in opens]
        with EVENTS_PATH.open("a", encoding="utf-8", newline="\n") as handle:
            for index, event in enumerate(events):
                event.update(event_id=f"puprime:{sequence}:{index}", source="puprime",
                             sl=None, tp=None, timestamp=timestamp)
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        next_state = {"sequence": sequence, "positions": current}
        self._save_state(next_state)
        self.state = next_state
        logging.info("Committed %d source events", len(events))

    def run(self):
        devices = self._run_adb(["devices"])
        if devices.returncode or "\tdevice" not in devices.stdout:
            raise RuntimeError("No connected ADB device; check the emulator")
        while True:
            try:
                self.observe(self._read_live_positions())
            except (RuntimeError, ValueError, ET.ParseError, subprocess.TimeoutExpired):
                self.candidate = None
                self.candidate_count = 0
                logging.exception("Invalid source snapshot; no trade signal emitted")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    SignalBridge().run()
