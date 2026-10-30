#!/usr/bin/env python3
"""Shared alerting library: dedup state machine + alerts.log + optional hook.

Usage:
    from alert import Alerter
    alerter = Alerter(state_path, alerts_log_path, hook_path=None, host="vm")
    alerter.fire("daemon_down", "CRITICAL", "mused process not found", {...})
    alerter.resolve("daemon_down")   # auto-called when condition clears
    alerter.flush()                  # emit state changes

Only emits on state transitions (firing -> resolved or vice versa).
"""

import json
import os
import subprocess
import time


class Alerter:
    def __init__(self, state_path, alerts_log_path, hook_path=None, host="vm"):
        self.state_path = state_path
        self.alerts_log_path = alerts_log_path
        self.hook_path = hook_path
        self.host = host
        self._state = self._load_state()
        self._pending = []  # alerts to emit on flush()

    def _load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _emit(self, alert_id, severity, message, details, state):
        alert = {
            "ts": int(time.time()),
            "id": alert_id,
            "severity": severity,
            "host": self.host,
            "message": message,
            "details": details or {},
            "state": state,
        }
        # 1. always append to alerts.log
        try:
            os.makedirs(os.path.dirname(self.alerts_log_path), exist_ok=True)
            with open(self.alerts_log_path, "a") as f:
                f.write(json.dumps(alert) + "\n")
        except OSError:
            pass
        # 2. optional hook (stdin = alert JSON)
        if self.hook_path and os.path.isfile(self.hook_path) \
                and os.access(self.hook_path, os.X_OK):
            try:
                subprocess.run(
                    [self.hook_path],
                    input=json.dumps(alert).encode(),
                    timeout=10, capture_output=True,
                )
            except (OSError, subprocess.SubprocessError):
                pass

    def fire(self, alert_id, severity, message, details=None):
        """Mark alert as firing. Emits only on transition into firing."""
        prev = self._state.get(alert_id, {}).get("state")
        self._state[alert_id] = {
            "state": "firing",
            "severity": severity,
            "since": self._state.get(alert_id, {}).get("since", int(time.time())),
            "last_eval": int(time.time()),
        }
        if prev != "firing":
            self._pending.append((alert_id, severity, message, details, "firing"))

    def resolve(self, alert_id):
        """Mark alert as resolved. Emits only on transition out of firing."""
        prev = self._state.get(alert_id, {}).get("state")
        if prev == "firing":
            self._state[alert_id] = {
                "state": "resolved",
                "last_eval": int(time.time()),
            }
            self._pending.append((alert_id, "INFO",
                                  f"{alert_id} resolved", {}, "resolved"))

    def check(self, alert_id, severity, condition, message, details=None):
        """Convenience: fire if condition true, else resolve."""
        if condition:
            self.fire(alert_id, severity, message, details)
        else:
            self.resolve(alert_id)

    def flush(self):
        for alert_id, severity, message, details, state in self._pending:
            self._emit(alert_id, severity, message, details, state)
        self._pending = []
        self._save_state()
