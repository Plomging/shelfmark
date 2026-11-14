"""Babelio metadata provider. French community book catalog, no API key required.

babelio.com has no public API, so this reads the web pages. Book pages carry
schema.org JSON-LD, which is the primary source; the rest is HTML.

Pages used (ids are the number at the end of the path; the site redirects
``/livres/x/<id>`` to the canonical slug URL):
    POST /recherche.php    book search (form ``Recherche=<query>``), first page
    /livres/<slug>/<id>    book page
"""

import json
import re
import unicodedata
from typing import Any, ClassVar
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from shelfmark.core.cache import cacheable
from shelfmark.core.logger import setup_logger
from shelfmark.core.settings_registry import (
    ActionButton,
    CheckboxField,
    HeadingField,
    SettingsField,
    register_settings,
)
from shelfmark.download.network import get_proxies, get_ssl_verify
from shelfmark.metadata_providers import (
    BookMetadata,
    DisplayField,
    MetadataProvider,
    MetadataSearchOptions,
    SearchField,
    SearchType,
    SortOrder,
    TextSearchField,
    register_provider,
)
from shelfmark.metadata_providers.openlibrary import RateLimiter

logger = setup_logger(__name__)

BABELIO_BASE_URL = "https://www.babelio.com"
BABELIO_SEARCH_URL = f"{BABELIO_BASE_URL}/recherche.php"
BABELIO_BOOK_URL = f"{BABELIO_BASE_URL}/livres"
# AJAX endpoint behind the book page's "Voir plus": POST type=1, id_obj=<resume id>
# returns the full summary the page only shows truncated
BABELIO_RESUME_URL = f"{BABELIO_BASE_URL}/aj_voir_plus_a.php"

# The site publishes no limit; stay well below anything a reader would notice
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Babelio's own anti-bot interstitial (not Cloudflare): a "Vérification de
# sécurité" page with a slider captcha. It appears intermittently, so requests
# that run into it are retried once on cleared cookies, then given up on.
CAPTCHA_MARKERS = ("vérification de sécurité", "slider-track")

ISBN_13_LENGTH = 13
# The search POST has no known page parameter; only the first page is fetched
SEARCH_MAX_PAGE = 1
# search_by_isbn verifies candidates on their book page; checking every hit on
# an ISBN string match would fetch half the catalog
ISBN_MAX_CANDIDATES = 3
# An interstitial is a few KB of markup; a real page mentioning a marker is bigger
MAX_CAPTCHA_HTML_CHARS = 64 * 1024

# Chrome serves its own navigation error page on transient network changes; it is a
# long body the bypasser's length heuristic mistakes for content, so it is filtered
# before anything is parsed.
_CHROME_ERROR_PAGE_MARKER = 'id="main-frame-error"'

# Book and author ids are the trailing number of the path segment
_LIVRES_RE = re.compile(r"/livres/[^/]+/(\d+)")
_AUTEUR_RE = re.compile(r"/auteur/[^/]+/(\d+)")
_RATING_RE = re.compile(r"\d+(?:[.,]\d+)?")
_YEAR_RE = re.compile(r"\b(1\d{3}|20\d{2})\b")
# The book page truncates its summary and appends a button calling
# voir_plus_a('#d_bio', 1, <resume id>); the id feeds the full-summary endpoint
_VOIR_PLUS_RE = re.compile(r"voir_plus_a\('#d_bio',\s*1,\s*(\d+)\)")
_SERIE_LINK_RE = re.compile(r"/serie/[^/]+/(\d+)")
# 'tome 1 sur 7' in the series nav (position may be fractional for novellas)
_SERIE_POS_RE = re.compile(r"tome\s*([\d]+(?:[.,]\d+)?)\s*sur\s*(\d+)")

# Search card layouts: the grid (title or mixed queries) and the author
# bibliography carousel shown on an exact author match. Each maps to its
# (title class, rating class) pair.
_CARD_LAYOUTS = {
    "lv-item": ("lv-title", "lv-note"),
    "book-card": ("book-card-title", "book-card-badge"),
}

_rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def _clean_text(value: str | None) -> str:
    """Collapse whitespace."""
    return " ".join((value or "").split())


def _normalize_for_match(value: str | None) -> str:
    """Accent-insensitive, punctuation-insensitive comparison form."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = "".join(char if char.isalnum() else " " for char in value)
    return " ".join(value.lower().split())


def _author_matches(query: str, name: str) -> bool:
    """Author match where every word of the query appears in the name.

    Accent-insensitive and word-order tolerant: 'julia' matches 'Julia Colin',
    and 'Alain Damasio' matches 'Damasio Alain'.
    """
    query_words = set(_normalize_for_match(query).split())
    name_words = set(_normalize_for_match(name).split())
    return bool(query_words) and query_words <= name_words


def _book_id_from_link(link: str | None) -> str:
    """Return the book id of a link like /livres/la-horde-du-contrevent/5420."""
    match = _LIVRES_RE.search(link or "")
    return match.group(1) if match else ""


def _author_id_from_link(link: str | None) -> str:
    """Return the author id of a link like /auteur/alain-damasio/5151."""
    match = _AUTEUR_RE.search(link or "")
    return match.group(1) if match else ""


def _absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(BABELIO_BASE_URL + "/", url.lstrip("/"))


def _valid_isbn(candidate: str | None) -> str | None:
    """Return a normalized ISBN-10/13 (digits, with optional X check digit), else None."""
    digits = re.sub(r"[\s-]", "", candidate or "").upper()
    if len(digits) == ISBN_13_LENGTH and digits.isdigit():
        return digits
    if re.fullmatch(r"\d{9}[\dX]", digits):
        return digits
    return None


def _bypass_enabled() -> bool:
    """Whether the browser bypasser is enabled in Settings (Cloudflare bypass toggle)."""
    from shelfmark.core.config import config

    return bool(config.get("USE_CF_BYPASS", True))


def _browser_fetch(method: str, url: str, data: dict[str, str] | None) -> str | None:
    """Fetch a page through the internal browser bypasser.

    GETs use the bypasser directly; form POSTs run inside its cleared page, since
    the bypasser itself only does GET. The bypass module imports SeleniumBase at
    module scope, so the import stays lazy: an install without the browser extra
    must keep every provider loadable.
    """
    try:
        from shelfmark.bypass.internal_bypasser import get_bypassed_page, post
    except ImportError:
        logger.warning("Babelio needs the browser bypasser, but its dependencies are not installed")
        return None
    try:
        if method == "POST" and data is not None:
            return post(url, data)
        return get_bypassed_page(url)
    except (requests.RequestException, RuntimeError) as e:
        logger.warning("Babelio browser bypasser failed for %s: %s", url, e)
        return None


def _is_captcha_page(html: str) -> bool:
    """Recognize Babelio's slider-captcha interstitial."""
    if len(html) > MAX_CAPTCHA_HTML_CHARS:
        return False
    lowered = html.lower()
    return any(marker in lowered for marker in CAPTCHA_MARKERS)


def _is_browser_error_page(html: str) -> bool:
    """Recognize Chrome's own navigation-error page."""
    return _CHROME_ERROR_PAGE_MARKER in html


def _json_ld(soup: BeautifulSoup, wanted_type: str) -> dict[str, Any]:
    """Return the schema.org JSON-LD block of the wanted type, e.g. Book.

    The block may sit at the top level or inside an ``@graph`` array.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for node in [item, *item.get("@graph", [])]:
                if isinstance(node, dict) and node.get("@type") == wanted_type:
                    return node
    return {}


def _ld_names(value: Any) -> list[str]:
    """JSON-LD author/publisher names, given as an object or a list of them."""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    names = [_clean_text(str(node.get("name", ""))) for node in value if isinstance(node, dict)]
    return [name for name in names if name]


def _ld_number(value: Any) -> float | None:
    """Read a JSON-LD number that may be encoded as a French decimal string."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return None
    return None


def _rating_value(text: str | None) -> str | None:
    """'★ 3,7' or ' 4,3' -> '3.7'."""
    match = _RATING_RE.search(text or "")
    if not match:
        return None
    return f"{float(match.group().replace(',', '.')):.1f}"


def _parse_biblio_table(soup: BeautifulSoup) -> dict[str, str]:
    """Return the 'Informations bibliographiques' rows keyed by label."""
    rows: dict[str, str] = {}
    table = soup.find("table", class_="biblio-table")
    if not table:
        return rows
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = _clean_text(cells[0].get_text(" ")).rstrip(" :")
        rows[label] = _clean_text(cells[1].get_text(" "))
    return rows


def _parse_genres(soup: BeautifulSoup) -> list[str]:
    """Genres from the breadcrumb (Imaginaire > Science fiction), minus the book link."""
    genres: list[str] = []
    breadcrumb = soup.find("nav", class_="breadcrumb")
    if not breadcrumb:
        return genres
    for link in breadcrumb.find_all("a", href=True):
        if _book_id_from_link(str(link.get("href"))):
            continue
        name = _clean_text(link.get_text(" "))
        if name and name not in genres:
            genres.append(name)
    return genres


def _parse_synopsis(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """The description block, minus the 'Voir plus' footer Babelio appends.

    Babelio serves the summary truncated, with a button whose onclick carries
    the resume id the full-summary endpoint wants; both are returned so the
    caller can fetch the rest.
    """
    node = soup.find(id="d_bio")
    if not node:
        return None, None
    # The truncation footer carries the resume id the full-summary endpoint wants
    match = _VOIR_PLUS_RE.search(str(node))
    resume_id = match.group(1) if match else None
    text = _clean_text(node.get_text(" "))
    for cut in (">Voir plus", "Voir plus", "Contributeurs :"):
        index = text.find(cut)
        if index >= 0:
            text = text[:index]
    return text.strip() or None, resume_id


def _parse_series(soup: BeautifulSoup) -> tuple[str | None, str | None, float | None, int | None]:
    """Series id, name, this book's position and the series' book count.

    The page carries them in a dedicated nav (name, 'tome 1 sur 7', a link to
    the series page); standalone books have none of it.
    """
    nav = soup.find("nav", class_="hero-serie")
    if not nav:
        return None, None, None, None
    series_id: str | None = None
    series_link = nav.select_one("a[href*='/serie/']")
    if series_link:
        match = _SERIE_LINK_RE.search(str(series_link.get("href") or ""))
        series_id = match.group(1) if match else None
    name_node = nav.select_one(".hero-serie-name")
    series_name = _clean_text(name_node.get_text(" ")) if name_node else None
    position: float | None = None
    count: int | None = None
    pos_node = nav.select_one(".hero-serie-pos")
    if pos_node:
        match = _SERIE_POS_RE.search(_clean_text(pos_node.get_text(" ")))
        if match:
            position = float(match.group(1).replace(",", "."))
            count = int(match.group(2))
    if not (series_id or series_name or position is not None):
        return None, None, None, None
    return series_id, series_name, position, count


def _book_url(book_id: str) -> str:
    """Any slug works: the site redirects to the canonical one."""
    return f"{BABELIO_BOOK_URL}/x/{book_id}"


@register_provider("babelio")
class BabelioProvider(MetadataProvider):
    """Babelio metadata provider (HTML scraping, French community catalog)."""

    name = "babelio"
    display_name = "Babelio"
    requires_auth = False
    supported_sorts: ClassVar[tuple[SortOrder, ...]] = (SortOrder.RELEVANCE,)
    search_fields: ClassVar[tuple[SearchField, ...]] = (
        TextSearchField(
            key="author",
            label="Author",
            description="Search by author name",
        ),
        TextSearchField(
            key="title",
            label="Title",
            description="Search by book title",
        ),
    )

    def __init__(self) -> None:
        """Initialize provider."""
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)

    def is_available(self) -> bool:
        """Babelio needs no authentication."""
        return True

    def _fetch(self, url: str, timeout: int = 15) -> str | None:
        return self._request("GET", url, timeout=timeout)

    def _search_post(self, query: str, timeout: int = 15) -> str | None:
        """Run a site search and return the results page, or None on failure."""
        return self._request(
            "POST",
            BABELIO_SEARCH_URL,
            data={"Recherche": query, "recherche": "OK"},
            timeout=timeout,
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> str | None:
        """Perform a request, falling back to the browser bypasser past the captcha.

        The captcha arrives as a 403 or a small interstitial page. A challenged
        session keeps being challenged, so the cookies are dropped before the
        retry: a fresh request sometimes passes where a flagged one never will.
        When the captcha still shows, the request goes through the internal
        browser bypasser, whose real Chrome Babelio does not challenge.
        """
        for attempt in (1, 2):
            _rate_limiter.wait_if_needed()
            try:
                response = self.session.request(
                    method,
                    url,
                    verify=get_ssl_verify(url),
                    proxies=get_proxies(url),
                    **kwargs,
                )
            except requests.Timeout:
                logger.warning("Babelio request timed out: %s", url)
                return None
            except requests.RequestException as e:
                logger.warning("Babelio request failed: %s (%s)", url, e.__class__.__name__)
                return None
            if response.status_code == 403 or _is_captcha_page(response.text):
                if attempt == 1:
                    logger.info("Babelio anti-bot captcha received; clearing session cookies")
                    self.session.cookies.clear()
                    continue
                return self._browser_fallback(method, url, kwargs.get("data"))
            if response.status_code >= 400:
                logger.warning("Babelio request failed: HTTP %s for %s", response.status_code, url)
                return None
            return response.text
        return None

    def _browser_fallback(self, method: str, url: str, data: dict[str, str] | None) -> str | None:
        """Fetch through the internal browser bypasser when HTTP keeps being challenged."""
        if not _bypass_enabled():
            logger.warning(
                "Babelio is showing its security captcha and the browser bypasser is "
                "disabled. Enable Cloudflare bypass in Settings, or try again later."
            )
            return None
        logger.info("Babelio captcha persists; retrying through the browser bypasser: %s", url)
        for attempt in (1, 2):
            html = _browser_fetch(method, url, data)
            if html and not _is_captcha_page(html) and not _is_browser_error_page(html):
                return html
            if attempt == 1:
                # The site serves its interstitial intermittently, even to real
                # Chrome, and Chrome's own error pages surface as long bodies the
                # bypasser mistakes for content. Both are worth one more fetch.
                reason = "its security captcha" if html and _is_captcha_page(html) else "no page"
                logger.info("Babelio browser bypasser received %s; retrying once", reason)
        logger.warning("Babelio browser bypasser returned no usable page for %s", url)
        return None

    def _full_synopsis(self, truncated: str, resume_id: str | None) -> str:
        """Replace a 'Voir plus'-truncated summary with the site's full one.

        The book page cuts long summaries and serves the rest through a POST
        endpoint keyed by the resume id found next to the truncation. A short
        summary has no such button, so nothing is requested. The fetched text
        only wins if it is longer: a failed or empty answer keeps the original.
        """
        if not resume_id:
            return truncated
        html = self._request(
            "POST",
            BABELIO_RESUME_URL,
            data={"type": "1", "id_obj": resume_id},
        )
        if not html:
            logger.info("Babelio full summary request returned nothing for resume %s", resume_id)
            return truncated
        text = _clean_text(BeautifulSoup(html, "html.parser").get_text(" "))
        # The endpoint answers with the whole summary, not only the missing part
        return text if len(text) > len(truncated) else truncated

    # ------------------------------------------------------------------ search

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        """Search Babelio by title, or list an author's books."""
        if options.search_type == SearchType.ISBN:
            result = self.search_by_isbn(options.query)
            return [result] if result else []

        author_value = _clean_text(options.fields.get("author"))
        title_value = _clean_text(options.fields.get("title"))
        query = _clean_text(options.query)
        if options.search_type == SearchType.AUTHOR and not author_value:
            author_value, query = query, ""
        if not (author_value or title_value or query):
            return []
        if options.page > SEARCH_MAX_PAGE:
            return []

        fields_key = ":".join(f"{k}={v}" for k, v in sorted(options.fields.items()))
        cache_key = (
            f"{query}:{options.search_type.value}:{options.language}:"
            f"{options.page}:{options.limit}:{fields_key}"
        )
        return (
            self._search_cached(
                cache_key,
                query,
                title_value,
                author_value,
                options.limit,
            )
            or []
        )

    @cacheable(ttl_key="METADATA_CACHE_SEARCH_TTL", ttl_default=300, key_prefix="babelio:search")
    def _search_cached(
        self,
        cache_key: str,
        query: str,
        title: str,
        author: str,
        limit: int,
    ) -> list[BookMetadata] | None:
        # Return None (not []) on fetch failure so the failure is not cached.
        if author and not title:
            # The site search only matches titles, so ask for the author by
            # name: an exact match renders their bibliography as a carousel
            books = self._search_author_books(author)
        else:
            books = self._search_titles(title or query)
            if books is not None and author:
                books = [
                    book for book in books if any(_author_matches(author, a) for a in book.authors)
                ]
        if books is None:
            return None
        logger.info(
            "Babelio search '%s' returned %s results",
            " / ".join(t for t in (author, title or query) if t),
            len(books),
        )
        return books[:limit]

    def _search_titles(self, query: str) -> list[BookMetadata] | None:
        html = self._search_post(query)
        if html is None:
            return None
        return self._parse_search_results(BeautifulSoup(html, "html.parser"))

    def _search_author_books(self, author: str) -> list[BookMetadata] | None:
        html = self._search_post(author)
        if html is None:
            return None
        soup = BeautifulSoup(html, "html.parser")
        # The site answers with the title grid and, on an exact author match,
        # a bibliography carousel; keep the hits by this author from both
        books = self._parse_search_results(soup)
        return [book for book in books if any(_author_matches(author, a) for a in book.authors)]

    def _parse_search_results(self, soup: BeautifulSoup) -> list[BookMetadata]:
        """Parse the search card layouts, de-duplicated by book id.

        The title grid (``lv-item``) serves title and mixed queries; the
        ``book-card`` carousel is an exact author match's bibliography. Both
        share the same shape, only the title and rating classes differ.
        """
        books: list[BookMetadata] = []
        seen: set[str] = set()
        for card_class, (title_class, rating_class) in _CARD_LAYOUTS.items():
            for card in soup.find_all("div", class_=card_class):
                title_link = card.select_one(f".{title_class} a[href*='/livres/']")
                book_id = (
                    _book_id_from_link(str(title_link.get("href") or "")) if title_link else ""
                )
                title = _clean_text(title_link.get_text(" ")) if title_link else ""
                if not book_id or not title or book_id in seen:
                    continue
                seen.add(book_id)

                author_link = card.select_one("a[href*='/auteur/']")
                authors = [_clean_text(author_link.get_text(" "))] if author_link else []
                authors = [author for author in authors if author]

                display_fields: list[DisplayField] = []
                rating_node = card.select_one(f".{rating_class}")
                rating = _rating_value(
                    _clean_text(rating_node.get_text(" ")) if rating_node else None
                )
                if rating:
                    display_fields.append(DisplayField(label="Rating", value=rating, icon="star"))

                cover = card.find("img", src=True)
                source_url = str(title_link.get("href") or "") if title_link else ""

                books.append(
                    BookMetadata(
                        provider=self.name,
                        provider_id=book_id,
                        provider_display_name=self.display_name,
                        title=title,
                        authors=authors,
                        cover_url=_absolute_url(str(cover.get("src") or "")) if cover else None,
                        source_url=_absolute_url(source_url),
                        search_title=title,
                        search_author=authors[0] if authors else None,
                        display_fields=display_fields,
                    )
                )
        return books

    # ------------------------------------------------------------------ book

    @cacheable(ttl_key="METADATA_CACHE_BOOK_TTL", ttl_default=600, key_prefix="babelio:book")
    def get_book(self, book_id: str) -> BookMetadata | None:
        """Get book details by Babelio book id (e.g. '5420')."""
        book_id = _book_id_from_link(book_id) or book_id.strip()
        if not book_id.isdigit():
            return None
        html = self._fetch(_book_url(book_id))
        if html is None:
            return None
        book = self._parse_book_page(BeautifulSoup(html, "html.parser"), book_id)
        if book is None:
            logger.warning("Babelio book page missing title/authors: %s", book_id)
            return None
        return book

    def _parse_book_page(self, soup: BeautifulSoup, book_id: str) -> BookMetadata | None:
        """Parse a book page from its JSON-LD, or None if this is not a book page."""
        data = _json_ld(soup, "Book")
        table = _parse_biblio_table(soup)

        title = _clean_text(str(data.get("name", "")))
        if not title:
            heading = soup.select_one("h1.book-title")
            title = _clean_text(heading.get_text(" ")) if heading else ""
        authors = _ld_names(data.get("author"))
        if not authors:
            author_link = soup.select_one(
                "table.biblio-table a[href*='/auteur/'], .book-meta a[href*='/auteur/']"
            )
            if author_link:
                authors = [_clean_text(author_link.get_text(" "))]
        authors = [author for author in authors if author]
        if not book_id or not title or not authors:
            return None

        description, resume_id = _parse_synopsis(soup)
        if not description:
            description = _clean_text(str(data.get("description", ""))) or None
        if description:
            description = self._full_synopsis(description, resume_id)

        publisher = ""
        publisher_data = data.get("publisher")
        if isinstance(publisher_data, dict):
            publisher = _clean_text(str(publisher_data.get("name", "")))
        if not publisher:
            editor_link = soup.select_one("a[href*='/editeur/']")
            publisher = _clean_text(editor_link.get_text(" ")) if editor_link else ""

        publish_year = None
        published = table.get("Publication", "")
        match = _YEAR_RE.search(published)
        if match:
            publish_year = int(match.group())
        else:
            match = _YEAR_RE.search(str(data.get("datePublished", "")))
            if match:
                publish_year = int(match.group())

        isbn = _valid_isbn(table.get("ISBN") or table.get("EAN") or "")
        if not isbn:
            isbn = _valid_isbn(str(data.get("isbn", "")))

        display_fields: list[DisplayField] = []
        pages = _ld_number(data.get("numberOfPages"))
        if pages is None and (row := table.get("Pages", "")):
            pages = _ld_number(row)
        if pages:
            display_fields.append(DisplayField(label="Pages", value=str(int(pages)), icon="book"))
        rating = data.get("aggregateRating") or {}
        rating_value = _ld_number(rating.get("ratingValue") if isinstance(rating, dict) else None)
        rating_count = _ld_number(
            rating.get("ratingCount", rating.get("reviewCount"))
            if isinstance(rating, dict)
            else None
        )
        if rating_value is None:
            # No JSON-LD: the search-page badge carries the same average
            badge = soup.select_one(".book-card-badge, .lv-note")
            rating_value = (
                _ld_number(_rating_value(_clean_text(badge.get_text(" ")))) if badge else None
            )
        if rating_value:
            display_fields.append(
                DisplayField(label="Rating", value=f"{rating_value:.1f}", icon="star")
            )
        if rating_count:
            display_fields.append(
                DisplayField(label="Ratings", value=f"{int(rating_count):,}", icon="users")
            )

        # The JSON-LD genre list is the site's own curated set (the breadcrumb
        # mixes in editorial selections like 'best of' lists); the breadcrumb is
        # only there for pages without one
        raw_genres = data.get("genre") or []
        if isinstance(raw_genres, str):
            raw_genres = [raw_genres]
        genres = [_clean_text(str(genre)) for genre in raw_genres if _clean_text(str(genre))]
        if not genres:
            genres = _parse_genres(soup)

        image = str(data.get("image", "")) or ""
        if not image:
            og_image = soup.find("meta", property="og:image")
            image = str(og_image.get("content") or "") if og_image else ""

        series_id, series_name, series_position, series_count = _parse_series(soup)

        return BookMetadata(
            provider=self.name,
            provider_id=book_id,
            provider_display_name=self.display_name,
            title=title,
            authors=authors,
            isbn_13=isbn if isbn and len(isbn) == ISBN_13_LENGTH else None,
            isbn_10=isbn if isbn and len(isbn) != ISBN_13_LENGTH else None,
            cover_url=_absolute_url(image) or None,
            description=description,
            publisher=publisher or None,
            publish_year=publish_year,
            source_url=_absolute_url(str(data.get("url", ""))) or _book_url(book_id),
            search_title=title,
            search_author=authors[0],
            display_fields=display_fields,
            genres=genres,
            series_id=series_id,
            series_name=series_name,
            series_position=series_position,
            series_count=series_count,
        )

    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        """Best-effort ISBN search: query the site search, then verify candidates.

        The search index matches ISBN strings, but a hit's snippet does not show
        the ISBN, so each candidate's book page is checked against the
        bibliographic table before being returned.
        """
        isbn = _valid_isbn(isbn) or ""
        if not isbn:
            return None
        html = self._search_post(isbn)
        if html is None:
            return None
        candidates = self._parse_search_results(BeautifulSoup(html, "html.parser"))
        for candidate in candidates[:ISBN_MAX_CANDIDATES]:
            book = self.get_book(candidate.provider_id)
            if book and isbn in (book.isbn_13, book.isbn_10):
                return book
        return None


def _test_babelio_connection() -> dict[str, Any]:
    """Test connectivity to babelio.com."""
    try:
        provider = BabelioProvider()
        html = provider._search_post("test")
    except requests.RequestException as e:
        return {"success": False, "message": f"Connection failed: {e}"}
    if html is None:
        return {
            "success": False,
            "message": (
                "babelio.com blocked every attempt, including the browser bypasser "
                "(its security captcha). Check that the Cloudflare bypass is enabled "
                "and its browser dependencies are installed, then retry."
            ),
        }
    if "babelio" in html.lower():
        return {"success": True, "message": "Successfully connected to babelio.com"}
    return {"success": False, "message": "Unexpected response from babelio.com"}


@register_settings("babelio", "Babelio", icon="library", order=56, group="metadata_providers")
def babelio_settings() -> list[SettingsField]:
    """Babelio metadata provider settings."""
    return [
        HeadingField(
            key="babelio_heading",
            title="Babelio",
            description=(
                "French community book catalog with excellent coverage of French editions "
                "and translations. No API key required. It has no public API, so Shelfmark "
                "reads the web pages. Its security captcha gates plain HTTP requests, so "
                "searches automatically go through the browser bypasser; each lookup "
                "therefore takes a little longer, and the results are cached."
            ),
            link_url=BABELIO_BASE_URL,
            link_text="babelio.com",
        ),
        CheckboxField(
            key="BABELIO_ENABLED",
            label="Enable Babelio",
            description="Enable Babelio as a metadata provider for book searches",
            default=False,
        ),
        ActionButton(
            key="test_connection",
            label="Test Connection",
            description="Verify babelio.com is accessible",
            style="primary",
            callback=_test_babelio_connection,
        ),
    ]
