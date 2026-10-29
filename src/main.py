"""Bounded native JSON-RPC transport. No account or SDK output reaches logs."""
import argparse
import json
import os
from pathlib import Path
import sys
import queue
import threading
import time
from apple_music_plugin.service import Service
from apple_music_plugin.state import Failure
from apple_music_plugin.deadline import budget, RPC_SECONDS

MAX_LINE = 64 * 1024


class Dispatcher:
    """I reserve a bounded health lane while serializing account/data operations."""
    HEALTH = {"initialize", "inspect", "auth.status", "settings.open", "auth.login", "reporting.status"}

    def __init__(self, service, write):
        self.service, self.write = service, write
        self.output_lock = threading.Lock()
        self.lanes = (queue.Queue(maxsize=4), queue.Queue(maxsize=2))
        self.workers = [threading.Thread(target=self.run, args=(lane,), daemon=True) for lane in self.lanes]
        for worker in self.workers:
            worker.start()

    def emit(self, answer):
        if answer is not None:
            with self.output_lock:
                try:
                    self.write(answer)
                except (ValueError, TypeError):
                    self.write({"jsonrpc": "2.0", "id": answer.get("id"), "error": {
                        "code": -32000, "message": "The operation returned an invalid response."}})
                except (BrokenPipeError, OSError):
                    pass

    def submit(self, message):
        method = message.get("method") if isinstance(message, dict) else None
        lane = self.lanes[0 if isinstance(method, str) and method in self.HEALTH else 1]
        try:
            lane.put_nowait((message, time.monotonic() + RPC_SECONDS))
        except queue.Full:
            if isinstance(message, dict) and "id" in message:
                self.emit({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32001,
                    "message": "The plugin is busy. Retry after the current operation finishes.",
                    "data": {"category": "runtime", "reason": "busy", "retryable": True}}})

    def run(self, lane):
        while True:
            entry = lane.get()
            try:
                if entry is None:
                    return
                message, deadline = entry
                ran = False
                try:
                    with budget(deadline):
                        ran = True
                        answer = reply(self.service, message)
                except Failure as error:
                    data = error.data
                    if ran and isinstance(message, dict) and message.get("method") in {
                            "settings.submit", "auth.logout", "favorites.set", "page.action", "track.resolve", "playback.event"}:
                        data = {**(data or {}), "retryable": False, "outcome": "unknown"}
                    answer = ({"jsonrpc": "2.0", "id": message["id"], "error": {
                        "code": error.code, "message": str(error), "data": data}}
                        if isinstance(message, dict) and "id" in message else None)
                self.emit(answer)
            finally:
                lane.task_done()

    def drain(self):
        for lane in self.lanes:
            lane.join()

    def close(self):
        self.drain()
        for lane in self.lanes:
            lane.put(None)
        for worker in self.workers:
            worker.join(timeout=1)


def reply(service, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request."}}
    try:
        result = {"result": service.handle(message["method"], message.get("params", {}))}
    except Failure as error:
        result = {"error": {"code": error.code, "message": str(error)}}
        if error.data:
            result["error"]["data"] = error.data
    except Exception:
        result = {"error": {"code": -32000, "message": "The Apple Music operation failed. Refresh and try again."}}
    if "id" in message:
        return {"jsonrpc": "2.0", "id": message["id"], **result}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/data")
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    parser.add_argument("--runtime", default=str(base / "runtime"))
    args = parser.parse_args()
    service = Service(args.data_dir, args.runtime)
    dispatcher = Dispatcher(service, lambda answer: print(json.dumps(answer, ensure_ascii=False, allow_nan=False), flush=True))
    try:
        while line := sys.stdin.buffer.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE:
                # Terminate the malformed transport instead of buffering an unlimited line.
                break
            try:
                message = json.loads(line)
                dispatcher.submit(message)
                continue
            except (ValueError, UnicodeDecodeError):
                answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON."}}
            if answer is not None:
                dispatcher.emit(answer)
    finally:
        dispatcher.close()
        service.close()


if __name__ == "__main__":
    main()
