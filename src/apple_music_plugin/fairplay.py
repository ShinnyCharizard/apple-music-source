"""Explicit, verified installation of Apple's Android FairPlay components."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .state import Failure, private_dir

COMPANIONS = ("libOpenSLES.so", "libandroid.so", "libdl.so", "libplugin_guard.so")
MAX_FILE = 32 * 1024 * 1024


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


_opener = build_opener(RejectRedirect)


def _open(request, timeout):
    return _opener.open(request, timeout=timeout)


def inventory():
    directory = Path(__file__).resolve().parent
    sources, entries = [], []
    sizes = json.loads((directory / "fairplay-sizes.json").read_text())
    for line in (directory / "fairplay-libs.lock").read_text().splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0] == "source" and len(fields) == 2:
            url = fields[1]
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" or not parsed.path.endswith("/rootfs/system/lib64"):
                raise Failure("Invalid FairPlay source lock.")
            sources.append(url)
        elif len(fields) == 2 and len(fields[0]) == 64 and all(c in "0123456789abcdef" for c in fields[0]):
            digest, name = fields
            if Path(name).name != name or not name.endswith(".so") or name not in sizes:
                raise Failure("Invalid FairPlay component lock.")
            size = sizes[name]
            if not isinstance(size, int) or not 0 < size <= MAX_FILE:
                raise Failure("Invalid FairPlay component size.")
            entries.append((name, size, digest))
        else:
            raise Failure("Invalid FairPlay component lock.")
    if len(sources) != 2 or len(entries) != 22 or len({entry[0] for entry in entries}) != 22 or set(sizes) != {entry[0] for entry in entries}:
        raise Failure("Incomplete FairPlay component lock.")
    return tuple(sources), tuple(entries)


class FairPlayComponents:
    def __init__(self, data, runtime):
        self.runtime = Path(runtime).resolve()
        self.root = Path(data) / "fairplay" / "drm"
        self.sources, self.entries = inventory()
        self.total = sum(size for _, size, _ in self.entries)

    @property
    def library_dir(self):
        return self.root / "rootfs" / "system" / "lib64"

    @staticmethod
    def _matches(path, size, digest):
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
                return False
            hasher = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return hasher.hexdigest() == digest
        except OSError:
            return False

    def ready(self):
        if any(path.is_symlink() for path in (self.root, self.root / "rootfs", self.root / "rootfs/system", self.library_dir)):
            return False
        if not all(self._matches(self.library_dir / name, size, digest) for name, size, digest in self.entries):
            return False
        return all((self.library_dir / name).is_file() for name in COMPANIONS) and (self.root / "hybris-linker/q.so").is_file()

    def installed_bytes(self):
        return sum(size for name, size, digest in self.entries if self._matches(self.library_dir / name, size, digest))

    def _companions(self):
        source_dir = self.runtime / "drm/rootfs/system/lib64"
        for name in COMPANIONS:
            source = source_dir / name
            if not source.is_file() or source.is_symlink():
                raise Failure("The local FairPlay adapter is incomplete. Reinstall the plugin.")
            shutil.copy2(source, self.library_dir / name)
        source = self.runtime / "drm/hybris-linker/q.so"
        if not source.is_file() or source.is_symlink():
            raise Failure("The local FairPlay linker is missing. Reinstall the plugin.")
        destination = private_dir(self.root / "hybris-linker") / "q.so"
        shutil.copy2(source, destination)

    def install(self, progress, cancelled):
        private_dir(self.root)
        private_dir(self.root / "rootfs")
        private_dir(self.root / "rootfs/system")
        private_dir(self.library_dir)
        self._companions()
        done = self.installed_bytes()
        progress(done, self.total, "Checking existing files")
        for index, (name, size, digest) in enumerate(self.entries, 1):
            if cancelled():
                raise Failure("Download cancelled.")
            destination = self.library_dir / name
            if self._matches(destination, size, digest):
                continue
            last_error = None
            for source in self.sources:
                temporary = None
                try:
                    progress(done, self.total, f"Downloading {index}/{len(self.entries)}: {name}")
                    request = Request(source + "/" + name, headers={"User-Agent": "Plugin-Apple-Music/0.1"})
                    with _open(request, timeout=30) as response:
                        final = urlsplit(response.geturl())
                        if final.scheme != "https" or response.geturl() != source + "/" + name or response.status != 200:
                            raise Failure("The FairPlay source changed domains or returned an unexpected response.")
                        fd, temporary = tempfile.mkstemp(prefix=".fairplay-", dir=self.library_dir)
                        hasher, received = hashlib.sha256(), 0
                        with os.fdopen(fd, "wb") as output:
                            while chunk := response.read(256 * 1024):
                                if cancelled():
                                    raise Failure("Download cancelled.")
                                received += len(chunk)
                                if received > size or received > MAX_FILE:
                                    raise Failure("A FairPlay file exceeds its pinned size.")
                                output.write(chunk)
                                hasher.update(chunk)
                                progress(done + received, self.total, f"Downloading {index}/{len(self.entries)}: {name}")
                            output.flush()
                            os.fsync(output.fileno())
                    if received != size or hasher.hexdigest() != digest:
                        raise Failure("FairPlay file SHA-256 verification failed.")
                    os.replace(temporary, destination)
                    temporary = None
                    done += size
                    progress(done, self.total, f"Verified {index}/{len(self.entries)}: {name}")
                    break
                except (OSError, HTTPError, URLError, http.client.HTTPException, Failure) as error:
                    last_error = error
                    if cancelled():
                        raise Failure("Download cancelled.") from None
                finally:
                    if temporary:
                        Path(temporary).unlink(missing_ok=True)
            else:
                raise Failure(f"Could not download and verify {name}. Try again in settings.") from last_error
        if cancelled():
            raise Failure("Download cancelled.")
        if not self.ready():
            raise Failure("FairPlay installation is incomplete.")
        progress(self.total, self.total, "Components verified")

    def remove(self):
        if self.root.is_dir() and not self.root.is_symlink():
            shutil.rmtree(self.root)
