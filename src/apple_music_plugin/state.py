"""Private preferences and sanitized errors; account secrets belong to the SDK."""
import json
import os
import tempfile
from pathlib import Path


class Failure(Exception):
    def __init__(self, message, code=-32000, data=None):
        super().__init__(message)
        self.code, self.data = code, data


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Failure("The plugin data directory must not be a symbolic link.")
    path.chmod(0o700)
    return path


def save(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".preferences-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
