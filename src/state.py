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
    "auto_trade": {
        "pending_action": None,
        "expected_level": None,
        "expected_direction": None,
        "started_at": None,
        "client_ids": []
    },
    "stablecoin_transfers": {
        "initialized": False,
        "seen": {}
    },
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
            data.setdefault("auto_trade", copy.deepcopy(DEFAULT_STATE["auto_trade"]))
            data.setdefault("stablecoin_transfers", copy.deepcopy(DEFAULT_STATE["stablecoin_transfers"]))
            data["stablecoin_transfers"].setdefault("initialized", False)
            data["stablecoin_transfers"].setdefault("seen", {})
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

    @property
    def pending_action(self):
        return self.data.get("auto_trade", {}).get("pending_action")

    def mark_auto_pending(self, action, expected_level, expected_direction, client_ids):
        self.data["auto_trade"] = {
            "pending_action": action,
            "expected_level": expected_level,
            "expected_direction": expected_direction,
            "started_at": _now_iso(),
            "client_ids": client_ids,
        }

    def clear_auto_pending(self):
        self.data["auto_trade"] = copy.deepcopy(DEFAULT_STATE["auto_trade"])

    def reset_alerts_for_current_level(self):
        self._reset_alerts_for_level(self.level_state)

    def pending_confirmed(self, current_level, current_direction):
        pending = self.data.get("auto_trade", {})
        if not pending.get("pending_action"):
            return False
        if pending.get("expected_level") != current_level:
            return False
        expected_direction = pending.get("expected_direction")
        return expected_direction in (None, 0, current_direction)

    def pending_stale(self, stale_after_seconds):
        pending = self.data.get("auto_trade", {})
        started_at = pending.get("started_at")
        if not pending.get("pending_action") or not started_at:
            return False
        try:
            started_dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds()
        return elapsed >= stale_after_seconds

    @property
    def stablecoin_transfers_initialized(self):
        return self.data.get("stablecoin_transfers", {}).get("initialized", False)

    def seed_stablecoin_transfers(self, transfers):
        self.data["stablecoin_transfers"] = {
            "initialized": True,
            "seen": {
                str(t.get("id")): transfer_fingerprint(t)
                for t in transfers
                if t.get("id")
            }
        }

    def transfer_notice_due(self, transfer):
        transfer_id = transfer.get("id")
        if not transfer_id or not self.stablecoin_transfers_initialized:
            return False
        seen = self.data.get("stablecoin_transfers", {}).get("seen", {})
        return seen.get(str(transfer_id)) != transfer_fingerprint(transfer)

    def mark_transfer_seen(self, transfer):
        transfer_id = transfer.get("id")
        if not transfer_id:
            return
        bucket = self.data.setdefault("stablecoin_transfers", {
            "initialized": True,
            "seen": {}
        })
        bucket["initialized"] = True
        bucket.setdefault("seen", {})[str(transfer_id)] = transfer_fingerprint(transfer)

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


def transfer_fingerprint(transfer):
    keys = ["kind", "status", "direction", "token", "amount", "last_updated_at"]
    return "|".join(str(transfer.get(k, "")) for k in keys)
