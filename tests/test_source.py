import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("source_bridge", Path(__file__).resolve().parents[1] / "main.py")
module = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        module.BRIDGE_DIR = root
        module.EVENTS_PATH = root / "events.jsonl"
        module.STATE_PATH = root / "source_state.json"
        module.LOG_PATH = root / "bridge.log"
        self.bridge = module.SignalBridge()

    def test_valid_screen_and_invalid_screen(self):
        xml = ('<hierarchy><node text="Positions"><node text="XAUUSD"/>'
               '<node text="BUY"/><node text="Volume 0.10"/></node></hierarchy>')
        self.assertEqual(self.bridge._extract_positions_from_xml(xml),
                         [module.Position("XAUUSD", "BUY", 0.1)])
        self.assertIsNone(self.bridge._extract_single_record("BUY XAUUSD 2345.67"))
        with self.assertRaises(RuntimeError):
            self.bridge._extract_positions_from_xml('<hierarchy><node text="Positions"/></hierarchy>')
        with self.assertRaises(RuntimeError):
            self.bridge._extract_positions_from_xml('<hierarchy><node text="Dashboard"/></hierarchy>')

    def test_baseline_replay_and_distinct_equal_positions(self):
        empty = []
        trade = module.Position("XAUUSD", "BUY", 0.1)
        for _ in range(2):
            self.bridge.observe(empty)
        self.assertEqual(self.bridge.state["sequence"], 0)
        for _ in range(2):
            self.bridge.observe([trade, trade])
        emitted = [json.loads(line) for line in module.EVENTS_PATH.read_text().splitlines()]
        self.assertEqual([e["event"] for e in emitted], ["OPEN", "OPEN"])
        self.assertEqual(len({e["source_trade_id"] for e in emitted}), 2)

        # Simulate a crash after append but before the state commit.
        module.STATE_PATH.write_text(json.dumps({"sequence": 0, "positions": []}))
        restarted = module.SignalBridge()
        for _ in range(2):
            restarted.observe([trade, trade])
        replay = [json.loads(line) for line in module.EVENTS_PATH.read_text().splitlines()][2:]
        for first, second in zip(emitted, replay):
            self.assertEqual(first["event_id"], second["event_id"])
            self.assertEqual(first["source_trade_id"], second["source_trade_id"])

        for _ in range(2):
            restarted.observe([trade])
        close = json.loads(module.EVENTS_PATH.read_text().splitlines()[-1])
        self.assertEqual(close["event"], "CLOSE")
        self.assertEqual(close["source_trade_id"], emitted[1]["source_trade_id"])

    def test_baseline_position_does_not_emit_unmatched_close(self):
        trade = module.Position("EURUSD", "SELL", 0.2)
        for _ in range(2):
            self.bridge.observe([trade])
        for _ in range(2):
            self.bridge.observe([])
        self.assertFalse(module.EVENTS_PATH.exists())
        self.assertEqual(self.bridge.state["positions"], [])

    def test_missing_state_with_existing_events_stops(self):
        module.EVENTS_PATH.write_text('{"event_id":"old"}\n')
        with self.assertRaises(RuntimeError):
            module.SignalBridge()


if __name__ == "__main__":
    unittest.main()
