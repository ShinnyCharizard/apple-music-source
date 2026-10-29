"""I bound a whole RPC, including queueing, restoration and HTTP reads."""
from contextlib import contextmanager
import socket
import threading
import time
from .state import Failure

LOCAL = threading.local()
RPC_SECONDS = 25


def remaining(cap=None):
    end = getattr(LOCAL, "end", None)
    seconds = RPC_SECONDS if end is None else end - time.monotonic()
    if seconds <= 0:
        raise Failure("The operation timed out. Check runtime status and retry the request.",
                      data={"category": "transport", "reason": "deadline", "retryable": True})
    return min(seconds, cap) if cap is not None else seconds


@contextmanager
def budget(end=None):
    previous = getattr(LOCAL, "end", None)
    LOCAL.end = min(previous, end) if previous is not None and end is not None else previous or end or time.monotonic() + RPC_SECONDS
    try:
        remaining()
        yield
        remaining()
    finally:
        LOCAL.end = previous


@contextmanager
def connection_deadline(connection, cap=None):
    expired = threading.Event()
    def interrupt():
        expired.set()
        channel = connection.sock
        if channel is not None:
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
    timer = threading.Timer(remaining(cap), interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield
        remaining()
    except OSError:
        if expired.is_set():
            raise Failure("The runtime request reached its deadline. Check status before retrying.",
                          data={"category": "transport", "reason": "deadline", "retryable": True}) from None
        raise
    finally:
        timer.cancel()
