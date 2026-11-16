"""Write Shelfmark's known metadata directly into downloaded EPUB files.

An EPUB carries its metadata in the package document (content.opf). When the
`WRITE_EPUB_METADATA` setting is enabled, downloaded EPUBs get their title,
author, year, language, series, genres, aggregate rating and similar fields
overwritten with what the task learned from the metadata providers, so library
apps and e-readers show clean metadata instead of whatever the release source
embedded.

Editing uses `epublib`, which handles EPUBs non intrusively (it does not
recreate the manifest or rewrite unrelated entries). Any failure is logged and
the file is delivered unmodified: metadata writing must never fail a download.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from epublib.package.metadata import BookMetadata, GenericMetadataItem, ValuedMetadataItem

from shelfmark.core.config import config
from shelfmark.core.logger import setup_logger

if TYPE_CHECKING:
    from pathlib import Path

    from shelfmark.core.models import DownloadTask

logger = setup_logger(__name__)

#: Calibre-style series metadata, read by Calibre, Calibre-Web, KOReader...
_SERIES_META_PROPERTIES = ("calibre:series", "calibre:series_index")

#: Calibre-style aggregate rating, on the same 0-5 scale the providers use.
_RATING_META_PROPERTY = "calibre:rating"


def write_epub_metadata_enabled() -> bool:
    """Check whether metadata embedding is enabled in the settings."""
    return bool(config.get("WRITE_EPUB_METADATA", False))


def _format_series_position(position: float | None) -> str | None:
    """Render a series position without trailing .0 (3 -> "3", 1.5 -> "1.5")."""
    if position is None:
        return None
    if float(position).is_integer():
        return str(int(position))
    return str(position)


def _clear_existing_values(book: BookMetadata, names: tuple[str, ...]) -> None:
    """Remove existing items for the given metadata names, dc or meta alike.

    Items refined by other metadata entries (file-as, title-type, role...) go
    too: epublib only removes the item itself, and epubcheck flags refines
    pointing at ids that no longer exist.
    """
    stale_ids: set[str] = set()
    for item in list(book.items):
        if isinstance(item, ValuedMetadataItem) and item.name in names:
            item_id = getattr(item, "id", None)
            if item_id:
                stale_ids.add(str(item_id))
            book.remove_item(item)

    if not stale_ids:
        return
    for item in list(book.items):
        refines = getattr(item, "refines", None)
        if refines and str(refines).lstrip("#") in stale_ids:
            book.remove_item(item)


def _set_dc_value(book: BookMetadata, name: str, value: str | None) -> None:
    """Replace a Dublin Core metadata value, removing stale items first."""
    if not value or not str(value).strip():
        return
    _clear_existing_values(book, (name,))
    book.add_dc(name, str(value).strip())


#: Declared on <package> so epubcheck accepts calibre:* meta properties.
_CALIBRE_PREFIX_DECL = "calibre: https://calibre-ebook.com"


def _declare_calibre_prefix(book: BookMetadata) -> None:
    """Add the calibre prefix declaration to <package> if not already present.

    EPUB 3 requires property prefixes to be declared via xmlns or the prefix
    attribute; readers accept calibre:series either way, but epubcheck errors
    on the undeclared form.
    """
    metadata_tag = book.parent_tag
    package_tag = metadata_tag.parent if metadata_tag is not None else None
    if package_tag is None:
        return
    existing = package_tag.get("prefix")
    if not existing:
        package_tag["prefix"] = _CALIBRE_PREFIX_DECL
    elif "calibre:" not in existing:
        package_tag["prefix"] = f"{existing} {_CALIBRE_PREFIX_DECL}"


def _set_subjects(book: BookMetadata, task: DownloadTask) -> None:
    """Write the provider's genres as dc:subject entries, replacing old ones."""
    raw_genres = (task.book_data or {}).get("genres")
    if not isinstance(raw_genres, list):
        return
    genres = [str(genre).strip() for genre in raw_genres if str(genre).strip()]
    if not genres:
        return
    _clear_existing_values(book, ("subject",))
    for genre in genres:
        book.add_dc("subject", genre)


def _set_rating(book: BookMetadata, task: DownloadTask) -> None:
    """Write the aggregate rating as a calibre:rating meta (0-5 scale)."""
    raw_rating = (task.book_data or {}).get("rating")
    try:
        rating = float(raw_rating)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return
    if not 0 < rating <= 5:
        return
    _clear_existing_values(book, (_RATING_META_PROPERTY,))
    book.add(_RATING_META_PROPERTY, f"{rating:.1f}")
    _declare_calibre_prefix(book)


def _set_series_metadata(book: BookMetadata, task: DownloadTask) -> None:
    """Write calibre:series / calibre:series_index when the task knows them."""
    series_name = (task.series_name or "").strip()
    position = _format_series_position(task.series_position)
    if not series_name and position is None:
        return

    _clear_existing_values(book, _SERIES_META_PROPERTIES)
    if series_name:
        book.add("calibre:series", series_name)
    if position is not None:
        book.add("calibre:series_index", position)
    _declare_calibre_prefix(book)


def _set_subtitle(book: BookMetadata, task: DownloadTask) -> None:
    """Write the subtitle as title-type="subtitle" when the task knows one."""
    subtitle = (task.subtitle or "").strip()
    if not subtitle:
        return
    item = book.add_dc("title", subtitle)
    # Mark this second dc:title so readers can tell it apart from the main title.
    try:
        item.id = book.get_new_id("subtitle")
        item.tag["id"] = item.id
        refines = GenericMetadataItem(
            soup=book.tag,  # type: ignore[arg-type]  # metadata.soup is a PackageDocumentSoup
            name="title-type",
            value="subtitle",
            refines=f"#{item.id}",
        )
        book.add_item(refines)
    except AttributeError, KeyError, TypeError:
        logger.debug("Could not tag subtitle item; keeping plain second dc:title")


def write_epub_metadata(path: Path, task: DownloadTask) -> bool:
    """Write task metadata into an EPUB file in place.

    Returns True when the file was rewritten, False when it was left untouched
    (disabled, not an EPUB, or any error). Never raises.
    """
    if not write_epub_metadata_enabled():
        return False
    if path.suffix.lower() != ".epub" or not path.exists():
        return False

    try:
        return _write_epub_metadata_inner(path, task)
    except Exception:
        logger.exception(
            "Task %s: failed to write metadata into %s; delivering it unmodified",
            task.task_id,
            path.name,
        )
        return False


def _write_epub_metadata_inner(path: Path, task: DownloadTask) -> bool:
    from epublib import EPUB

    # Write to a sibling temp file, then atomically replace: epublib keeps the
    # source zip open while writing, so writing straight into `path` corrupts
    # the very file being read (EOFError on any real-sized EPUB).
    temp_path = path.with_name(f".{path.stem}.shelfmark-meta.tmp")
    try:
        with EPUB(path) as book:
            metadata = book.metadata

            _set_dc_value(metadata, "title", task.title)
            _set_dc_value(metadata, "creator", task.author)
            _set_dc_value(metadata, "publisher", (task.book_data or {}).get("publisher"))
            _set_dc_value(metadata, "description", (task.book_data or {}).get("description"))
            _set_dc_value(metadata, "date", str(task.year) if task.year else None)
            _set_dc_value(metadata, "language", task.language)
            isbn = (task.book_data or {}).get("isbn_13") or (task.book_data or {}).get("isbn_10")
            if isbn:
                # Add the ISBN as a second identifier rather than replacing the
                # existing unique identifier, which the spine/manifest may reference.
                metadata.add_dc("identifier", str(isbn).strip())
            _set_series_metadata(metadata, task)
            _set_subtitle(metadata, task)
            _set_subjects(metadata, task)
            _set_rating(metadata, task)

            # Cover embedding: download/cache the cover, add it as a resource with
            # book.resources.add(create_resource_from_path(...)), then promote it
            # with book.resources.set_cover_image(...). Intentionally not implemented
            # yet: the cached cover is a proxy URL, and cover-less EPUBs need a
            # title-page XHTML fallback for some readers.
            metadata.modified = datetime.now(UTC)

            book.write(temp_path)

        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)

    logger.info("Task %s: wrote metadata into %s", task.task_id, path.name)
    return True
