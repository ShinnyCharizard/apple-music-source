"""I send only measured listening through the account's local runtime."""
from .event_queue import EventQueue


class Reports(EventQueue):
    def __init__(self, data, engine):
        self.engine = engine
        self.user = None
        super().__init__(data / "history.json", lambda: self.user, self.send)

    def resolve(self, track):
        self.user = self.engine.request("GET", "/reporting/account")["account"]
        return self.ticket(track)

    def clear(self):
        super().clear()
        self.user = None

    def flush(self):
        # After a restart I establish the account before touching persisted events.
        if self.user is None:
            with self.lock:
                if not self.state["outbox"]:
                    return
            try:
                self.user = self.engine.request("GET", "/reporting/account")["account"]
            except Exception:
                return
        super().flush()

    def send(self, entry):
        play = entry["play"]
        result = self.engine.request("POST", "/reporting/play", {
            "account": entry["user"], "id": entry["id"],
            "song": play["track"]["ref"].split(":")[-1],
            "duration_ms": play["track"].get("duration_ms", 0),
            "reason": entry.get("reason") or "stopped", "spans": play["spans"]})
        return "discard" if result.get("discard") else result.get("accepted", False)
