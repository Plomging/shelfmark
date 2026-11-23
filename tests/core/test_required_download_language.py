"""Mandatory language selection for downloads.

The global BOOK_LANGUAGE defines what a user may pick in the release list and
what auto-download may assume when a release has no language metadata.
"""

from __future__ import annotations

from shelfmark.core import auto_download
from shelfmark.core.config import config
from shelfmark.core.languages import allowed_download_languages
from shelfmark.release_sources import Release


def _set_book_language(monkeypatch, value: object) -> None:
    def fake_get(key, default=None, user_id=None):
        if key == "BOOK_LANGUAGE":
            return value
        return default

    monkeypatch.setattr(config, "get", fake_get)


def _release(**overrides) -> Release:
    fields = {"source": "prowlarr", "source_id": "r1", "title": "A book [M4B]"}
    fields.update(overrides)
    return Release(**fields)


def test_allowed_languages_follows_the_global_setting(monkeypatch):
    _set_book_language(monkeypatch, "fr, English")

    assert allowed_download_languages() == ["fr", "en"]


def test_all_or_empty_means_every_supported_language(monkeypatch):
    _set_book_language(monkeypatch, "all")
    assert len(allowed_download_languages()) > 10
    assert {"en", "fr"} <= set(allowed_download_languages())

    _set_book_language(monkeypatch, "")
    assert len(allowed_download_languages()) > 10


def test_single_allowed_language_is_the_auto_download_fallback(monkeypatch):
    _set_book_language(monkeypatch, "fr")
    allowed = allowed_download_languages()

    assert auto_download._required_download_language(_release(), allowed) == "fr"


def test_detected_language_wins_when_allowed(monkeypatch):
    _set_book_language(monkeypatch, "en,fr")
    allowed = allowed_download_languages()

    assert auto_download._required_download_language(_release(language="eng"), allowed) == "en"


def test_disallowed_detected_language_is_not_used(monkeypatch):
    _set_book_language(monkeypatch, "fr")
    allowed = allowed_download_languages()

    assert auto_download._required_download_language(_release(language="de"), allowed) is None


def test_several_allowed_languages_skip_unlabelled_releases(monkeypatch):
    _set_book_language(monkeypatch, "en,fr")
    allowed = allowed_download_languages()

    assert auto_download._required_download_language(_release(), allowed) is None
