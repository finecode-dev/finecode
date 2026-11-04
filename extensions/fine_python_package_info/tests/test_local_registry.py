"""Unit tests for ``LocalRegistry`` itself, over real HTTP with stdlib only.

These tests speak to the server the way any PEP 691 client would, so a
regression here means the rehearsal and handler-integration tests below it
would be exercising a server that no longer mirrors PyPI.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import typing
import urllib.parse
from pathlib import Path

from fine_python_package_info.testing.local_registry import (
    LocalRegistry,
    normalize_name,
)


def _encode_multipart(
    boundary: str, fields: dict[str, str], filename: str, data: bytes
) -> bytes:
    body = b""
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += value.encode() + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="content"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body


def _post_upload(
    registry: LocalRegistry,
    *,
    name: str,
    version: str,
    filename: str,
    data: bytes,
    authorization: str | None = None,
) -> tuple[int, str]:
    boundary = "test-boundary"
    body = _encode_multipart(
        boundary, {"name": name, "version": version}, filename, data
    )
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if authorization is not None:
        headers["Authorization"] = authorization
    connection = http.client.HTTPConnection(registry.host, registry.port)
    try:
        connection.request("POST", "/legacy/", body, headers)
        response = connection.getresponse()
        return (response.status, response.read().decode("utf-8", errors="replace"))
    finally:
        connection.close()


def _get_json(registry: LocalRegistry, path: str) -> tuple[int, typing.Any]:
    connection = http.client.HTTPConnection(registry.host, registry.port)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        if response.status != 200:
            return (response.status, raw)
        return (response.status, json.loads(raw))
    finally:
        connection.close()


def _get_bytes(registry: LocalRegistry, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection(registry.host, registry.port)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return (response.status, response.read())
    finally:
        connection.close()


def test_simple_index_lists_uploaded_version_with_pep700_versions_key(
    tmp_path: Path,
) -> None:
    """An uploaded file must show up in the PEP 691 index with its PEP 700 ``versions`` entry.

    The list handler refuses an index without a ``versions`` key, so a shape
    regression here would turn every "is it published?" check into an error
    instead of an answer. The file payload must also be downloadable, since
    installers follow the index ``url`` to fetch it.
    """
    data = b"fake-wheel-bytes"
    with LocalRegistry(tmp_path) as registry:
        status, _ = _post_upload(
            registry,
            name="pkg",
            version="1.0.0",
            filename="pkg-1.0.0.tar.gz",
            data=data,
        )
        assert status == 200

        status, payload = _get_json(registry, "/simple/pkg/")
        assert status == 200
        assert payload["meta"] == {"api-version": "1.1"}
        assert payload["versions"] == ["1.0.0"]
        assert len(payload["files"]) == 1
        assert payload["files"][0]["filename"] == "pkg-1.0.0.tar.gz"
        assert payload["files"][0]["hashes"] == {
            "sha256": hashlib.sha256(data).hexdigest()
        }

        file_url = payload["files"][0]["url"]
        status, downloaded = _get_bytes(registry, urllib.parse.urlsplit(file_url).path)
        assert status == 200
        assert downloaded == data


def test_simple_index_returns_404_for_unknown_package(tmp_path: Path) -> None:
    """An unknown name must 404 rather than return an empty index.

    The list handler reads 404 as "nothing published", matching PyPI, so a
    rehearsal against this server sees the same empty-registry answer it
    would see against a fresh real one.
    """
    with LocalRegistry(tmp_path) as registry:
        status, _ = _get_json(registry, "/simple/nothing-here/")
        assert status == 404


def test_simple_index_normalizes_names(tmp_path: Path) -> None:
    """Index lookups must normalize ``-``, ``_`` and ``.`` the way PyPI does.

    The list handler only lowercases underscores before querying, so the
    registry has to be the side that treats ``My_Pkg`` and ``my-pkg`` as one
    project -- otherwise the same release would look published under one
    spelling and missing under another.
    """
    with LocalRegistry(tmp_path) as registry:
        status, _ = _post_upload(
            registry,
            name="My_Pkg",
            version="1.0.0",
            filename="my_pkg-1.0.0.tar.gz",
            data=b"bytes",
        )
        assert status == 200

        for spelling in ("my-pkg", "MY_PKG", "my.pkg", "My_Pkg"):
            status, payload = _get_json(registry, f"/simple/{spelling}/")
            assert status == 200
            assert payload["versions"] == ["1.0.0"]

        assert normalize_name("My_Pkg") == "my-pkg"


def test_duplicate_filename_is_rejected_with_400(tmp_path: Path) -> None:
    """Re-uploading an existing filename must fail the way PyPI fails it.

    Twine only recognizes the conflict when the 400 body says the file
    already exists, so the wording is load-bearing: without it a forced
    re-upload would look like a server error instead of a duplicate.
    """
    with LocalRegistry(tmp_path) as registry:
        status, _ = _post_upload(
            registry,
            name="pkg",
            version="1.0.0",
            filename="pkg-1.0.0.tar.gz",
            data=b"first",
        )
        assert status == 200

        status, body = _post_upload(
            registry,
            name="pkg",
            version="1.0.0",
            filename="pkg-1.0.0.tar.gz",
            data=b"second",
        )
        assert status == 400
        assert "already exists" in body


def test_local_version_is_rejected_with_400(tmp_path: Path) -> None:
    """A version with a local segment must be refused at upload time.

    PyPI rejects ``+…`` versions outright, so accepting one here would let a
    rehearsal pass a release that the real registry would never take.
    """
    with LocalRegistry(tmp_path) as registry:
        status, _ = _post_upload(
            registry,
            name="pkg",
            version="1.0.0+local",
            filename="pkg-1.0.0+local.tar.gz",
            data=b"bytes",
        )
        assert status == 400


def test_reject_fails_uploads_with_configured_status(tmp_path: Path) -> None:
    """Fault injection must fail uploads while leaving index reads working.

    The failure-path rehearsal blocks a package's dependents by making its
    upload 500, and it still needs reads to answer -- a reject that also
    broke the index would not reproduce the partial outage under test.
    """
    with LocalRegistry(tmp_path) as registry:
        registry.reject("pkg", 500)

        status, _ = _post_upload(
            registry,
            name="pkg",
            version="1.0.0",
            filename="pkg-1.0.0.tar.gz",
            data=b"bytes",
        )
        assert status == 500

        status, _ = _get_json(registry, "/simple/pkg/")
        assert status == 404

        registry.reject("other")
        status, _ = _post_upload(
            registry,
            name="other",
            version="2.0.0",
            filename="other-2.0.0.tar.gz",
            data=b"bytes",
        )
        assert status == 500


def test_request_log_records_uploads_with_authorization(tmp_path: Path) -> None:
    """Every request must be logged with its credentials and uploaded filename.

    Assertions like "a dry run made no POST" or "the upload carried the
    configured credentials" have no other witness: the handlers return
    success or failure, never what they sent.
    """
    with LocalRegistry(tmp_path) as registry:
        _ = _get_json(registry, "/simple/pkg/")
        _ = _post_upload(
            registry,
            name="pkg",
            version="1.0.0",
            filename="pkg-1.0.0.tar.gz",
            data=b"bytes",
            authorization="Basic dGVzdA==",
        )

        log = registry.request_log
        assert [(record.method, record.path) for record in log] == [
            ("GET", "/simple/pkg/"),
            ("POST", "/legacy/"),
        ]
        assert log[0].authorization is None
        assert log[0].filename is None
        assert log[1].authorization == "Basic dGVzdA=="
        assert log[1].filename == "pkg-1.0.0.tar.gz"


def test_published_maps_names_to_versions_and_files(tmp_path: Path) -> None:
    """``published()`` must report each package's stored versions and files.

    Phase L asserts on what a rehearsal actually uploaded by reading this
    map, so a missing or mis-keyed entry would silently undercount a run.
    """
    with LocalRegistry(tmp_path) as registry:
        _ = _post_upload(
            registry,
            name="pkg",
            version="1.0.0",
            filename="pkg-1.0.0.tar.gz",
            data=b"sdist",
        )
        _ = _post_upload(
            registry,
            name="pkg",
            version="1.0.0",
            filename="pkg-1.0.0-py3-none-any.whl",
            data=b"wheel",
        )

        published = registry.published()
        assert set(published) == {"pkg"}
        assert published["pkg"].versions == ["1.0.0"]
        assert published["pkg"].files == [
            "pkg-1.0.0-py3-none-any.whl",
            "pkg-1.0.0.tar.gz",
        ]


def test_files_persist_across_restarts(tmp_path: Path) -> None:
    """Uploads must survive a server restart that keeps the directory.

    The failure-path rehearsal restarts the registry with the same ``--dir``
    and expects the earlier uploads to still be listed, so persistence has
    to live in the directory, not in the process.
    """
    storage = tmp_path / "registry"
    with LocalRegistry(storage) as registry:
        status, _ = _post_upload(
            registry,
            name="pkg",
            version="1.0.0",
            filename="pkg-1.0.0.tar.gz",
            data=b"bytes",
        )
        assert status == 200

    with LocalRegistry(storage) as registry:
        assert registry.published()["pkg"].versions == ["1.0.0"]
        status, payload = _get_json(registry, "/simple/pkg/")
        assert status == 200
        assert payload["versions"] == ["1.0.0"]
