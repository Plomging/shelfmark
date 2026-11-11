"""{ISBN} naming placeholder, and the provider ISBN reaching the download task."""

import pytest

from shelfmark.core.models import DownloadTask
from shelfmark.core.naming import KNOWN_TOKENS, build_library_path, parse_naming_template
from shelfmark.download import orchestrator
from shelfmark.download.postprocess.transfer import build_metadata_dict

TEMPLATE = "{Author}/{Title}{ ISBN}/{Title}"


def _metadata(isbn: str | None) -> dict:
    return {"Author": "Christopher Ruocchio", "Title": "Empire of Silence", "ISBN": isbn}


class TestIsbnPlaceholder:
    def test_known_token(self):
        assert "isbn" in KNOWN_TOKENS

    def test_substitution(self):
        assert parse_naming_template("{Author} - {Title} ({ISBN})", _metadata("9781529380331")) == (
            "Christopher Ruocchio - Empire of Silence (9781529380331)"
        )

    @pytest.mark.parametrize("isbn", [None, ""])
    def test_missing_isbn_drops_the_block(self, isbn):
        assert parse_naming_template("{Author} - {Title}{ ISBN}", _metadata(isbn)) == (
            "Christopher Ruocchio - Empire of Silence"
        )

    def test_library_path(self, tmp_path):
        path = build_library_path(str(tmp_path), TEMPLATE, _metadata("9781529380331"), "epub")
        assert path.parent.name == "Empire of Silence 9781529380331"


def test_task_isbn_reaches_template_metadata():
    task = DownloadTask(task_id="t", source="prowlarr", title="T", isbn="9781529380331")
    assert build_metadata_dict(task)["ISBN"] == "9781529380331"


def test_retry_payload_round_trips_isbn():
    task = DownloadTask(task_id="t", source="prowlarr", title="T", isbn="9781529380331")
    restored = orchestrator._restore_task_from_retry_payload(
        orchestrator.serialize_task_for_retry(task)
    )
    assert restored is not None
    assert restored.isbn == "9781529380331"


class TestQueueRelease:
    @pytest.fixture
    def queued(self, monkeypatch):
        captured: dict[str, DownloadTask] = {}

        def fake_add(task: DownloadTask) -> bool:
            captured["task"] = task
            return True

        monkeypatch.setattr(orchestrator.config, "get", lambda _key, default=None, **_kw: default)
        monkeypatch.setattr(orchestrator, "_source_unavailable_message", lambda _source: None)
        monkeypatch.setattr(orchestrator.book_queue, "add", fake_add)
        monkeypatch.setattr(orchestrator, "ws_manager", None)

        def queue(release: dict) -> DownloadTask:
            ok, error = orchestrator.queue_release(
                {"source": "prowlarr", "source_id": "x", "title": "Empire of Silence", **release},
                0,
            )
            assert ok, error
            return captured["task"]

        return queue

    def test_isbn_from_extra(self, queued):
        task = queued({"extra": {"isbn": "9781529380331"}})
        assert task.isbn == "9781529380331"

    def test_isbn_from_top_level(self, queued):
        task = queued({"isbn": "9781529380331", "extra": {"isbn": "ignored"}})
        assert task.isbn == "9781529380331"

    def test_no_isbn(self, queued):
        assert queued({"extra": {"isbn": "  "}}).isbn is None
