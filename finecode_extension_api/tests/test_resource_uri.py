"""Tests for the point at which a relative ``file://`` URI stops being relative.

A payload travels from whoever composed it through the WM into every ER the run
fans out to, and each ER runs in its own project directory.  A relative URI that
survives that trip is read against a different directory in every process that
reads it, so it has to be expanded while the composer's directory is still known.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from finecode_extension_api.resource_uri import (
    ResourceUri,
    absolutize_resource_uri,
    is_relative_file_uri,
    path_to_resource_uri,
    resource_location_to_uri,
    resource_uri_to_path,
)

_WS = pathlib.Path("/ws")


def test_relative_uri_is_expanded_against_the_given_base() -> None:
    # file://./pkg parses as netloc="." + path="/pkg"; the two halves have to be
    # rejoined before anything can be resolved
    assert absolutize_resource_uri(ResourceUri("file://./pkg"), _WS) == "file:///ws/pkg"
    assert absolutize_resource_uri(ResourceUri("file://pkg"), _WS) == "file:///ws/pkg"
    assert (
        absolutize_resource_uri(ResourceUri("file://pkg/mod.py"), _WS)
        == "file:///ws/pkg/mod.py"
    )


def test_expansion_does_not_depend_on_the_process_cwd() -> None:
    """The whole point: two processes with different CWDs must agree."""
    assert absolutize_resource_uri(
        ResourceUri("file://./pkg"), _WS
    ) == absolutize_resource_uri(ResourceUri("file://./pkg"), _WS)
    assert (
        absolutize_resource_uri(ResourceUri("file://./pkg"), _WS / "pkg")
        == "file:///ws/pkg/pkg"
    )


def test_parent_segments_are_collapsed_lexically() -> None:
    # resolve() would follow symlinks and hand back a path the WM does not key
    # its project state by; normpath leaves the base spelled as it was given
    assert (
        absolutize_resource_uri(ResourceUri("file://../other"), _WS / "pkg")
        == "file:///ws/other"
    )


def test_absolute_uri_is_returned_unchanged() -> None:
    absolute = ResourceUri("file:///elsewhere/mod.py")

    assert absolutize_resource_uri(absolute, _WS) == absolute


def test_non_file_scheme_is_returned_unchanged() -> None:
    # other schemes carry no local path, so there is nothing to expand
    other = ResourceUri("https://example.com/mod.py")

    assert absolutize_resource_uri(other, _WS) == other


def test_expansion_survives_the_round_trip_to_a_path() -> None:
    expanded = absolutize_resource_uri(ResourceUri("file://./pkg/mod.py"), _WS)

    assert resource_uri_to_path(expanded) == _WS / "pkg" / "mod.py"


def test_absolute_uris_still_convert_without_touching_the_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = path_to_resource_uri(_WS / "pkg" / "mod.py")
    monkeypatch.chdir(tmp_path)

    assert resource_uri_to_path(uri) == _WS / "pkg" / "mod.py"


def test_relative_uri_without_a_base_falls_back_to_the_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour ``absolutize_resource_uri`` exists to keep callers away from."""
    monkeypatch.chdir(tmp_path)

    assert resource_uri_to_path(ResourceUri("file://./pkg")) == tmp_path / "pkg"


def test_a_resource_may_be_named_as_a_uri_or_as_a_plain_path() -> None:
    """Callers who know a field holds a resource accept either spelling, so the
    same file can be named the short way without becoming a different file."""
    for spelling in ("file://./pkg", "file://pkg", "./pkg", "pkg"):
        assert resource_location_to_uri(spelling, _WS) == "file:///ws/pkg"


def test_an_absolute_plain_path_becomes_a_uri_unchanged_in_meaning() -> None:
    assert resource_location_to_uri("/elsewhere/mod.py", _WS) == (
        "file:///elsewhere/mod.py"
    )


def test_a_non_file_scheme_names_no_local_path() -> None:
    assert resource_location_to_uri("https://example.com/pkg", _WS) == (
        "https://example.com/pkg"
    )


def test_only_relative_file_uris_are_flagged() -> None:
    """What a sender must never let out: a URI whose meaning depends on where
    the reader happens to be standing."""
    assert is_relative_file_uri("file://./pkg") is True
    assert is_relative_file_uri("file://pkg/mod.py") is True
    assert is_relative_file_uri("file:///ws/pkg") is False
    # a plain path is not yet a URI, so it is not a *relative URI* to report;
    # a resource field turns it into an absolute one, and a text field keeps it
    assert is_relative_file_uri("./pkg") is False
    assert is_relative_file_uri("https://example.com/pkg") is False


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
def test_windows_drive_path_is_not_read_as_a_uri_scheme() -> None:
    # urlparse sees "C:/ws/mod.py" as scheme "c"; a one-letter scheme is a drive
    assert resource_location_to_uri("C:/ws/mod.py", _WS) == "file:///C:/ws/mod.py"


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
def test_windows_drive_uri_is_absolute() -> None:
    uri = ResourceUri("file:///C:/ws/mod.py")

    assert absolutize_resource_uri(uri, _WS) == uri
    assert resource_uri_to_path(uri) == pathlib.Path("C:/ws/mod.py")
