import json
import os
import copy
from datetime import datetime, timezone

STATE_FILE = "state.json"

DEFAULT_STATE = {
    "level_state": "FLAT",
    "direction": 0,
    "opened_at": None,
    "alerts_sent": {
        "L1_TP": False,
        "L2_open": False,
        "L2_close": False,
        "L3_open": False,
        "L3_close": False,
        "warning": False
    },
    "alert_last_sent": {},
    "last_total_pnl": 0.0,
    "last_update": None
}


class GridState:
    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                data = json.load(f)
            data.setdefault("alert_last_sent", {})
            data.setdefault("alerts_sent", copy.deepcopy(DEFAULT_STATE["alerts_sent"]))
            return data
        return copy.deepcopy(DEFAULT_STATE)

    def save(self):
        self.data["last_update"] = _now_iso()
        with open(self.state_file, "w") as f:
            json.dump(self.data, f, indent=2)

    # --- read helpers ---

    @property
    def level_state(self):
        return self.data["level_state"]

    @property
    def direction(self):
        return self.data["direction"]

    @property
    def direction_label(self):
        if self.data["direction"] == 1:
            return "LONG BTC / SHORT ETH"
        elif self.data["direction"] == -1:
            return "SHORT BTC / LONG ETH"
        return "NONE"

    def alert_sent(self, key):
        return self.data["alerts_sent"].get(key, False)

    def mark_alert(self, key):
        self.data["alerts_sent"][key] = True
        self.data.setdefault("alert_last_sent", {})[key] = _now_iso()

    def alert_due(self, key, repeat_interval_seconds):
        """Return True for first alert or when the repeat interval has elapsed."""
        if not self.alert_sent(key):
            return True

        last = self.data.get("alert_last_sent", {}).get(key)
        if not last:
            return True

        try:
            last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return True

        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed >= repeat_interval_seconds

    def update_pnl(self, pnl):
        self.data["last_total_pnl"] = pnl

    # --- state transitions ---

    def transition_to(self, new_level, new_dir):
        """Auto-detected level change: update state and reset relevant alerts."""
        old = self.data["level_state"]
        self.data["level_state"] = new_level
        if new_dir != 0:
            self.data["direction"] = new_dir

        # Entering a new level from FLAT means fresh start
        if old == "FLAT" and new_level != "FLAT":
            self.data["opened_at"] = _now_iso()
            self._reset_all_alerts()
        else:
            # Reset alerts relevant to the new level
            self._reset_alerts_for_level(new_level)

    def _reset_alerts_for_level(self, level):
        """Reset alerts that are relevant to this level so they can fire again."""
        mapping = {
            "FLAT": list(self.data["alerts_sent"].keys()),
            "L1": ["L1_TP", "L2_open"],
            "L1_L2": ["L2_close", "L3_open"],
            "L1_L2_L3": ["L3_close", "warning"],
        }
        for k in mapping.get(level, []):
            self.data["alerts_sent"][k] = False
            self.data.setdefault("alert_last_sent", {}).pop(k, None)

    def reset_warning(self):
        self.data["alerts_sent"]["warning"] = False
        self.data.setdefault("alert_last_sent", {}).pop("warning", None)
        self.save()

    # --- internal ---

    def _reset_all_alerts(self):
        for k in self.data["alerts_sent"]:
            self.data["alerts_sent"][k] = False
        self.data["alert_last_sent"] = {}

    def _reset_alerts(self, *keys):
        for k in keys:
            self.data["alerts_sent"][k] = False
            self.data.setdefault("alert_last_sent", {}).pop(k, None)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
