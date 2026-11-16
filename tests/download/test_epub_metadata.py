"""Tests for writing metadata into downloaded EPUB files."""

import zipfile
from types import SimpleNamespace

import pytest
from epublib import EPUB

from shelfmark.download.postprocess.epub_metadata import (
    write_epub_metadata,
    write_epub_metadata_enabled,
)

_MINIMAL_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:old</dc:identifier>
    <dc:title>Old Title</dc:title>
    <dc:creator>Wrong Author</dc:creator>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
  </metadata>
  <manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""


def _make_epub(path, opf=_MINIMAL_OPF):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/ch1.xhtml", "<html><head><title>x</title></head><body><p>hi</p></body></html>")


def _task(**overrides):
    fields = {
        "task_id": "t1",
        "title": "Dune",
        "author": "Frank Herbert",
        "year": "1965",
        "language": "fr",
        "series_name": "Dune Saga",
        "series_position": 1.0,
        "subtitle": "The Long Novel",
        "book_data": {"publisher": "Chilton", "isbn_13": "9780441013593", "description": "Spice."},
        "ignored_files": None,
        "book_plan": None,
        "multi_book": False,
        "content_type": "book (fiction)",
        "format": "epub",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def metadata_enabled(monkeypatch):
    from shelfmark.core.config import config

    monkeypatch.setattr(
        config,
        "get",
        lambda key, default=None, user_id=None: True
        if key == "WRITE_EPUB_METADATA"
        else default,
    )
    return True


@pytest.fixture
def metadata_disabled(monkeypatch):
    from shelfmark.core.config import config

    monkeypatch.setattr(config, "get", lambda key, default=None, user_id=None: default)
    return False


class TestEnabled:
    def test_disabled_by_default(self, metadata_disabled):
        assert write_epub_metadata_enabled() is False

    def test_enabled(self, metadata_enabled):
        assert write_epub_metadata_enabled() is True


class TestWriteEpubMetadata:
    def test_writes_all_fields(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.epub"
        _make_epub(path)

        assert write_epub_metadata(path, _task()) is True

        with EPUB(path) as book:
            opf = book.package_document.soup.prettify()
            assert "<dc:title>" in opf and "Dune" in opf
            assert "Frank Herbert" in opf
            assert "Chilton" in opf  # publisher from book_data
            assert "Spice." in opf  # description from book_data
            assert "1965" in opf  # year
            assert "fr" in opf  # language
            assert "9780441013593" in opf  # ISBN added as second identifier
            assert "Dune Saga" in opf  # calibre:series
            assert "calibre:series_index" in opf

    def test_series_position_not_zero_padded(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.epub"
        _make_epub(path)
        write_epub_metadata(path, _task(series_position=3.0))

        with EPUB(path) as book:
            opf = str(book.package_document.soup)
            assert ">3<" in opf
            assert ">3.0<" not in opf

    def test_float_series_position_kept(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.epub"
        _make_epub(path)
        write_epub_metadata(path, _task(series_position=1.5))

        with EPUB(path) as book:
            assert "1.5" in str(book.package_document.soup)

    def test_no_series_writes_no_series_meta(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.epub"
        _make_epub(path)
        write_epub_metadata(path, _task(series_name=None, series_position=None))

        with EPUB(path) as book:
            opf = str(book.package_document.soup)
            assert "calibre:series" not in opf

    def test_subtitle_marked_as_subtitle(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.epub"
        _make_epub(path)
        write_epub_metadata(path, _task())

        with EPUB(path) as book:
            opf = str(book.package_document.soup)
            assert "title-type" in opf
            assert "The Long Novel" in opf

    def test_old_values_replaced_not_duplicated(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.epub"
        _make_epub(path)
        write_epub_metadata(path, _task())

        with EPUB(path) as book:
            titles = book.package_document.soup.find_all("dc:title")
            main_titles = [t for t in titles if not t.get("id")]
            assert len(main_titles) == 1
            creators = book.package_document.soup.find_all("dc:creator")
            assert len(creators) == 1
            assert creators[0].get_text(strip=True) == "Frank Herbert"

    def test_corrupted_epub_left_untouched(self, tmp_path, metadata_enabled):
        path = tmp_path / "bad.epub"
        path.write_bytes(b"not an epub at all")

        assert write_epub_metadata(path, _task()) is False
        assert path.read_bytes() == b"not an epub at all"

    def test_disabled_leaves_file_alone(self, tmp_path, metadata_disabled):
        path = tmp_path / "book.epub"
        _make_epub(path)
        before = path.read_bytes()

        assert write_epub_metadata(path, _task()) is False
        assert path.read_bytes() == before

    def test_non_epub_ignored(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.pdf"
        path.write_bytes(b"%PDF-1.4 fake")

        assert write_epub_metadata(path, _task()) is False

    def test_missing_file_ignored(self, tmp_path, metadata_enabled):
        path = tmp_path / "missing.epub"

        assert write_epub_metadata(path, _task()) is False

    def test_empty_fields_not_written(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.epub"
        _make_epub(path)
        task = _task(
            title="Only Title", author=None, year=None, language=None,
            series_name=None, series_position=None, subtitle=None,
            book_data=None,
        )
        write_epub_metadata(path, task)

        with EPUB(path) as book:
            opf = str(book.package_document.soup)
            assert "Only Title" in opf
            assert "dc:publisher" not in opf
            assert "dc:description" not in opf


class TestRealEpubRegressions:
    """Regressions found by validating against real, downloaded EPUBs."""

    _WITH_REFINES_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:old</dc:identifier>
    <dc:title id="title">Old Title</dc:title>
    <meta property="file-as" refines="#title">Title, Old</meta>
    <meta property="title-type" refines="#title">main</meta>
    <dc:creator id="creator">Wrong Author</dc:creator>
    <meta property="role" refines="#creator" scheme="marc:relators">aut</meta>
    <dc:language>en</dc:language>
  </metadata>
  <manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""

    def test_no_temp_file_left_behind(self, tmp_path, metadata_enabled):
        """The write goes through a sibling temp file that must be cleaned up."""
        path = tmp_path / "book.epub"
        _make_epub(path)

        assert write_epub_metadata(path, _task()) is True

        leftovers = list(tmp_path.glob("*.shelfmark-meta.tmp"))
        assert leftovers == []
        # And the original file was actually replaced in place.
        with EPUB(path) as book:
            assert "Dune" in str(book.package_document.soup)

    def test_refines_of_removed_items_purged(self, tmp_path, metadata_enabled):
        """Metas refining a replaced item must not be left orphaned."""
        path = tmp_path / "book.epub"
        _make_epub(path, self._WITH_REFINES_OPF)
        write_epub_metadata(path, _task())

        with EPUB(path) as book:
            soup = book.package_document.soup
            md = soup.find("metadata")
            defined = {tag.get("id") for tag in md.find_all(id=True)}
            for meta in md.find_all("meta"):
                refines = meta.get("refines", "").lstrip("#")
                if refines:
                    assert refines in defined, f"orphan refines: #{refines}"

    def test_calibre_prefix_declared_on_package(self, tmp_path, metadata_enabled):
        """calibre:* metas require a prefix declaration on <package> (OPF-028)."""
        path = tmp_path / "book.epub"
        _make_epub(path)
        write_epub_metadata(path, _task())

        with EPUB(path) as book:
            soup = book.package_document.soup
            pkg = soup.find("package")
            md = soup.find("metadata")
            assert "calibre:" in (pkg.get("prefix") or "")
            # epublib's parent_tag is <metadata>; the declaration belongs one
            # level up, on <package>.
            assert "calibre:" not in (md.get("prefix") or "")

    def test_no_prefix_added_without_series(self, tmp_path, metadata_enabled):
        path = tmp_path / "book.epub"
        _make_epub(path)
        write_epub_metadata(path, _task(series_name=None, series_position=None))

        with EPUB(path) as book:
            pkg = book.package_document.soup.find("package")
            assert "calibre:" not in (pkg.get("prefix") or "")


class TestTransferIntegration:
    def test_transfer_writes_metadata_before_transfer(self, tmp_path, monkeypatch, metadata_enabled):
        from shelfmark.core.config import config
        from shelfmark.download.postprocess.transfer import transfer_book_files

        monkeypatch.setattr(
            config,
            "get",
            lambda key, default=None, user_id=None: {
                "WRITE_EPUB_METADATA": True,
                "FILE_ORGANIZATION": "none",
            }.get(key, default),
        )

        source = tmp_path / "src"
        source.mkdir()
        (tmp_path / "library").mkdir()
        epub = source / "book.epub"
        _make_epub(epub)

        paths, error, _ = transfer_book_files(
            [epub],
            tmp_path / "library",
            _task(),
            use_hardlink=False,
            is_torrent=True,
        )

        assert error is None
        assert len(paths) == 1
        with EPUB(paths[0]) as book:
            assert "Dune" in str(book.package_document.soup)

    def test_hardlink_skips_metadata_writing(self, tmp_path, monkeypatch):
        """With hardlinking, the source file must not be modified (seeding)."""
        import os

        from shelfmark.core.config import config
        from shelfmark.download.postprocess.transfer import transfer_book_files

        monkeypatch.setattr(
            config,
            "get",
            lambda key, default=None, user_id=None: {
                "WRITE_EPUB_METADATA": True,
                "FILE_ORGANIZATION": "none",
            }.get(key, default),
        )

        source = tmp_path / "src"
        source.mkdir()
        (tmp_path / "library").mkdir()
        epub = source / "book.epub"
        _make_epub(epub)
        before = epub.read_bytes()

        paths, error, _ = transfer_book_files(
            [epub],
            tmp_path / "library",
            _task(),
            use_hardlink=True,
            is_torrent=True,
        )

        assert error is None
        assert paths
        assert epub.read_bytes() == before  # source untouched
        # Same inode: it really is a hardlink.
        assert os.stat(epub).st_ino == os.stat(paths[0]).st_ino
