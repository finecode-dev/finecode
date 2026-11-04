"""Throwaway local Python package registry for tests and local rehearsals.

Test support, not a registry for real use: it reproduces only the PyPI
behavior the ``fine_python_package_info`` handlers rely on (the PEP 691 JSON
simple index with the PEP 700 ``versions`` key, file downloads, and twine's
multipart ``file_upload``), and nothing else. Pointing a real release flow at
this server proves only that the handlers spoke the protocol, not that PyPI
would accept the upload.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import json
import re
import threading
import types
import typing
import urllib.parse
from pathlib import Path

__all__ = [
    "LocalRegistry",
    "PublishedPackage",
    "RequestRecord",
    "main",
    "normalize_name",
]

_INDEX_FILENAME = "index.json"
_SIMPLE_API_VERSION = "1.1"
_SIMPLE_CONTENT_TYPE = "application/vnd.pypi.simple.v1+json"

_NORMALIZE_PATTERN = re.compile(r"[-_.]+")
_BOUNDARY_PATTERN = re.compile(
    r'boundary="(?P<quoted>[^"]+)"|boundary=(?P<token>[^; ]+)'
)
_FIELD_NAME_PATTERN = re.compile(r'name="(?P<name>[^"]*)"')
_FILENAME_PATTERN = re.compile(r'filename="(?P<filename>[^"]*)"')


def normalize_name(name: str) -> str:
    """Return the PEP 503 normalized form of a distribution name."""
    return _NORMALIZE_PATTERN.sub("-", name).lower()


class PublishedPackage(typing.NamedTuple):
    """Snapshot of what the registry holds for one normalized package name."""

    versions: list[str]
    files: list[str]


class RequestRecord(typing.NamedTuple):
    """One HTTP request the registry received, kept so tests can assert on it."""

    method: str
    path: str
    authorization: str | None
    filename: str | None


class _StoredFile(typing.NamedTuple):
    name: str
    version: str
    sha256: str


class _BadUpload(Exception): ...


class _RegistryState:
    def __init__(self, storage_dir: Path, base_url: str) -> None:
        self.storage_dir = storage_dir
        self.base_url = base_url
        self.lock = threading.Lock()
        self.files: dict[str, _StoredFile] = {}
        self.rejections: dict[str, int] = {}
        self.request_log: list[RequestRecord] = []


def _send_bytes(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    content_type: str,
    body: bytes,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _ = handler.wfile.write(body)


def _send_text(
    handler: http.server.BaseHTTPRequestHandler, status: int, message: str
) -> None:
    _send_bytes(handler, status, "text/plain; charset=utf-8", message.encode("utf-8"))


def _multipart_boundary(content_type: str) -> bytes | None:
    if "multipart/form-data" not in content_type:
        return None
    match = _BOUNDARY_PATTERN.search(content_type)
    if match is None:
        return None
    boundary = match.group("quoted") or match.group("token")
    return boundary.encode("utf-8")


def _split_multipart(
    body: bytes, boundary: bytes
) -> list[tuple[str, str | None, bytes]]:
    """Split a multipart body into (field name, filename or None, raw data) parts."""
    delimiter = b"--" + boundary
    parts: list[tuple[str, str | None, bytes]] = []
    for chunk in body.split(delimiter):
        if chunk.strip(b"\r\n-") in (b"", b"--"):
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        header_block, separator, data = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        disposition: bytes | None = None
        for line in header_block.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition:"):
                disposition = line
                break
        if disposition is None:
            continue
        try:
            disposition_text = disposition.decode("ascii")
        except UnicodeDecodeError:
            continue
        name_match = _FIELD_NAME_PATTERN.search(disposition_text)
        if name_match is None:
            continue
        filename_match = _FILENAME_PATTERN.search(disposition_text)
        parts.append(
            (
                name_match.group("name"),
                filename_match.group("filename") if filename_match else None,
                data,
            )
        )
    return parts


def _parse_upload(content_type: str | None, body: bytes) -> tuple[str, str, str, bytes]:
    """Split a multipart upload into (name, version, filename, data)."""
    if content_type is None:
        raise _BadUpload("Missing Content-Type")
    boundary = _multipart_boundary(content_type)
    if boundary is None:
        raise _BadUpload("Not a multipart upload")
    fields: dict[str, str] = {}
    file_part: tuple[str, bytes] | None = None
    for field_name, filename, data in _split_multipart(body, boundary):
        if filename is not None:
            if field_name == "content":
                file_part = (filename, data)
        else:
            fields[field_name] = data.decode("utf-8", errors="replace")
    name = fields.get("name", "")
    version = fields.get("version", "")
    if not name or not version or file_part is None:
        raise _BadUpload("Upload is missing name, version, or file content")
    return (name, version, file_part[0], file_part[1])


def _make_handler(
    registry: LocalRegistry,
) -> type[http.server.BaseHTTPRequestHandler]:
    class _RegistryHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: typing.Any) -> None:
            _ = format
            _ = args

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            registry._record_request(
                "GET", parsed.path, self.headers.get("Authorization"), None
            )
            if parsed.path in ("/simple", "/simple/"):
                _send_text(self, 404, "Unknown package")
                return
            if parsed.path.startswith("/simple/"):
                raw_name = urllib.parse.unquote(
                    parsed.path[len("/simple/") :].strip("/")
                )
                if not raw_name or "/" in raw_name:
                    _send_text(self, 404, "Unknown package")
                    return
                status, content_type, body = registry._simple_response(raw_name)
                _send_bytes(self, status, content_type, body)
                return
            if parsed.path.startswith("/files/"):
                filename = urllib.parse.unquote(parsed.path[len("/files/") :])
                data = registry._read_file(filename)
                if data is None:
                    _send_text(self, 404, f"Unknown file {filename!r}")
                    return
                _send_bytes(self, 200, "application/octet-stream", data)
                return
            _send_text(self, 404, "Not found")

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            authorization = self.headers.get("Authorization")
            if parsed.path not in ("/legacy", "/legacy/"):
                registry._record_request("POST", parsed.path, authorization, None)
                _send_text(self, 404, "Not found")
                return
            length_raw = self.headers.get("Content-Length")
            try:
                size = int(length_raw) if length_raw is not None else 0
            except ValueError:
                size = 0
            body = self.rfile.read(size) if size > 0 else b""
            try:
                name, version, filename, data = _parse_upload(
                    self.headers.get("Content-Type"), body
                )
            except _BadUpload as error:
                registry._record_request("POST", parsed.path, authorization, None)
                _send_text(self, 400, str(error))
                return
            registry._record_request("POST", parsed.path, authorization, filename)
            status, content_type, response_body = registry._store_upload(
                name, version, filename, data
            )
            _send_bytes(self, status, content_type, response_body)

    return _RegistryHandler


class LocalRegistry:
    """A throwaway PEP 691 index plus twine upload endpoint for tests.

    Starts a ``ThreadingHTTPServer`` on an ephemeral port in a background
    thread, stores uploaded files in the given directory, and records every
    request so tests can assert on what the handlers sent. Restarting with
    the same directory keeps the previously uploaded files.
    """

    def __init__(
        self, storage_dir: Path | str, *, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._host = host
        self._state = _RegistryState(storage_dir=self._storage_dir, base_url="")
        self._load_index()
        self._server = http.server.ThreadingHTTPServer(
            (host, port), _make_handler(self)
        )
        bound_port: int = self._server.server_address[1]
        self._state.base_url = f"http://{host}:{bound_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._running = False
        self._closed = False

    def __enter__(self) -> typing.Self:
        if not self._running:
            self._thread.start()
            self._running = True
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: types.TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop serving. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if self._running:
            self._server.shutdown()
            self._thread.join()
        self._server.server_close()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def index_url(self) -> str:
        return f"{self._state.base_url}/simple/"

    @property
    def upload_url(self) -> str:
        return f"{self._state.base_url}/legacy/"

    @property
    def request_log(self) -> list[RequestRecord]:
        with self._state.lock:
            return list(self._state.request_log)

    def published(self) -> dict[str, PublishedPackage]:
        """Map each normalized package name to its stored versions and files."""
        with self._state.lock:
            versions_by_name: dict[str, set[str]] = {}
            files_by_name: dict[str, list[str]] = {}
            for filename, entry in self._state.files.items():
                if not (self._storage_dir / filename).is_file():
                    continue
                normalized = normalize_name(entry.name)
                versions_by_name.setdefault(normalized, set()).add(entry.version)
                files_by_name.setdefault(normalized, []).append(filename)
            return {
                name: PublishedPackage(
                    versions=sorted(versions),
                    files=sorted(files_by_name[name]),
                )
                for name, versions in versions_by_name.items()
            }

    def reject(self, name: str, status: int = 500) -> None:
        """Fail subsequent uploads of one package with an HTTP status."""
        with self._state.lock:
            self._state.rejections[normalize_name(name)] = status

    def _record_request(
        self,
        method: str,
        path: str,
        authorization: str | None,
        filename: str | None,
    ) -> None:
        with self._state.lock:
            self._state.request_log.append(
                RequestRecord(
                    method=method,
                    path=path,
                    authorization=authorization,
                    filename=filename,
                )
            )

    def _index_path(self) -> Path:
        return self._storage_dir / _INDEX_FILENAME

    def _load_index(self) -> None:
        try:
            raw = self._index_path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            payload: typing.Any = json.loads(raw)
        except json.JSONDecodeError:
            return
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict):
            return
        for filename, entry in files.items():
            if not isinstance(filename, str) or not isinstance(entry, dict):
                continue
            name = entry.get("name")
            version = entry.get("version")
            sha256 = entry.get("sha256")
            if not (
                isinstance(name, str)
                and isinstance(version, str)
                and isinstance(sha256, str)
            ):
                continue
            if not (self._storage_dir / filename).is_file():
                continue
            self._state.files[filename] = _StoredFile(
                name=name, version=version, sha256=sha256
            )

    def _save_index(self) -> None:
        payload = {
            "files": {
                filename: {
                    "name": entry.name,
                    "version": entry.version,
                    "sha256": entry.sha256,
                }
                for filename, entry in self._state.files.items()
            }
        }
        _ = self._index_path().write_text(json.dumps(payload), encoding="utf-8")

    def _read_file(self, filename: str) -> bytes | None:
        with self._state.lock:
            if filename not in self._state.files:
                return None
        try:
            return (self._storage_dir / filename).read_bytes()
        except OSError:
            return None

    def _simple_response(self, name: str) -> tuple[int, str, bytes]:
        normalized = normalize_name(name)
        with self._state.lock:
            entries = [
                (filename, entry)
                for filename, entry in self._state.files.items()
                if normalize_name(entry.name) == normalized
                and (self._storage_dir / filename).is_file()
            ]
            base_url = self._state.base_url
        if not entries:
            return (404, "text/plain; charset=utf-8", b"Unknown package")
        versions = sorted({entry.version for _, entry in entries})
        files = [
            {
                "filename": filename,
                "url": f"{base_url}/files/{urllib.parse.quote(filename)}",
                "hashes": {"sha256": entry.sha256},
            }
            for filename, entry in sorted(entries)
        ]
        payload = {
            "meta": {"api-version": _SIMPLE_API_VERSION},
            "name": entries[0][1].name,
            "versions": versions,
            "files": files,
        }
        return (200, _SIMPLE_CONTENT_TYPE, json.dumps(payload).encode("utf-8"))

    def _store_upload(
        self, name: str, version: str, filename: str, data: bytes
    ) -> tuple[int, str, bytes]:
        with self._state.lock:
            rejection = self._state.rejections.get(normalize_name(name))
            if rejection is not None:
                return (
                    rejection,
                    "text/plain; charset=utf-8",
                    f"Upload of {filename!r} was rejected".encode(),
                )
            if "/" in filename or "\\" in filename or filename.startswith("."):
                return (
                    400,
                    "text/plain; charset=utf-8",
                    f"Invalid filename {filename!r}".encode(),
                )
            if "+" in version:
                return (
                    400,
                    "text/plain; charset=utf-8",
                    f"Version {version!r} has a local segment, "
                    "which this registry does not support".encode(),
                )
            if (
                filename in self._state.files
                and (self._storage_dir / filename).is_file()
            ):
                return (
                    400,
                    "text/plain; charset=utf-8",
                    f"File {filename!r} already exists".encode(),
                )
            _ = (self._storage_dir / filename).write_bytes(data)
            self._state.files[filename] = _StoredFile(
                name=name, version=version, sha256=hashlib.sha256(data).hexdigest()
            )
            self._save_index()
            return (200, "text/plain; charset=utf-8", b"Uploaded")


def main(argv: list[str] | None = None) -> int:
    """Serve a throwaway registry from a directory until interrupted."""
    parser = argparse.ArgumentParser(
        description="Serve a throwaway local Python package registry "
        "for tests and local rehearsals."
    )
    _ = parser.add_argument(
        "--dir",
        required=True,
        type=Path,
        help="Directory holding the uploaded files; created when missing, "
        "deleted to reset the registry.",
    )
    _ = parser.add_argument(
        "--port", type=int, default=0, help="Port to listen on (0 picks one)."
    )
    _ = parser.add_argument(
        "--reject",
        action="append",
        default=[],
        metavar="NAME[:STATUS]",
        help="Fail uploads of one package with an HTTP status "
        "(default 500). Repeatable.",
    )
    args = parser.parse_args(argv)
    reject_specs: list[str] = args.reject
    rejections: list[tuple[str, int]] = []
    for spec in reject_specs:
        name, colon, status_text = spec.partition(":")
        if not name:
            parser.error(f"Invalid --reject {spec!r}: expected NAME[:STATUS]")
        status = 500
        if colon:
            try:
                status = int(status_text)
            except ValueError:
                parser.error(f"Invalid --reject {spec!r}: STATUS must be an integer")
        rejections.append((name, status))
    storage_dir: Path = args.dir
    registry = LocalRegistry(storage_dir, port=args.port)
    for reject_name, reject_status in rejections:
        registry.reject(reject_name, reject_status)
    with registry:
        print(f"index_url: {registry.index_url}")
        print(f"upload_url: {registry.upload_url}", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            _ = threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
