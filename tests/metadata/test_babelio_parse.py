import pytest
import requests

from shelfmark.core.cache import get_metadata_cache
from shelfmark.metadata_providers import MetadataSearchOptions, SearchType
from shelfmark.metadata_providers.babelio import (
    BabelioProvider,
    _author_matches,
    _book_id_from_link,
    _valid_isbn,
)

# Shortened copies of real pages from https://www.babelio.com (October 2026 layout)
SEARCH_HTML = """<main class="page-main rs">
<div class="lv-grid" role="list">
<div class="lv-item" role="listitem"><a class="lv-cover-link" href="/livres/Francioso-Fondation-Deus-tome-1--Le-veilleur/1356017"><img class="lv-cover" src="/img/books/1356017.jpg" alt="" /></a><p class="lv-title"><a href="/livres/Francioso-Fondation-Deus-tome-1--Le-veilleur/1356017">Fondation Deus, tome 1 : Le veilleur</a></p><p class="lv-author"><a href="/auteur/Pierre-Arnaud-Francioso/128373">Pierre-Arnaud Francioso</a></p><p class="lv-meta"><span class="lv-note">&#9733; 3,7</span><span class="lv-copies">(60)</span></p></div>
<div class="lv-item" role="listitem"><p class="lv-title"><a href="/livres/Colin-Passer-la-brume/1993737">Passer la brume</a></p><p class="lv-author"><a href="/auteur/Julia-Colin/654430">Julia Colin</a></p><p class="lv-status"><span class="lv-status-chip">Lu</span></p></div>
</div>
<div class="book-carousel" role="list">
<div class="book-card" role="listitem"><div class="book-card-cover-wrap"><a href="/livres/Damasio-La-Horde-du-Contrevent/5420"><img alt="" /></a><span class="book-card-badge"><svg></svg> 4,3</span></div><p class="book-card-title"><a href="/livres/Damasio-La-Horde-du-Contrevent/5420">La Horde du Contrevent</a></p><p class="book-card-author"><a href="/auteur/Alain-Damasio/5151">Alain Damasio</a></p></div>
<div class="book-card" role="listitem"><p class="book-card-title"><a href="/livres/Colin-Passer-la-brume/1993737">Passer la brume</a></p><p class="book-card-author"><a href="/auteur/Julia-Colin/654430">Julia Colin</a></p></div>
</div></main>"""

BOOK_HTML = """<html><head><title>Antelucem</title></head><body>
<nav class="breadcrumb"><a href="/selection/Les-livres-de-sf-fantasy-fantastique/365" class="livre_auteurs">Imaginaire</a><span>></span><a href="/selection/Les-livres-de-science-fiction/367" class="livre_auteurs">Science fiction</a><span>></span><a href="/livres/Barbati-Antelucem-tome-2--La-symphonie-des-ombres/1892050">Antelucem, tome 2</a></nav>
<article class="book-hero"><div class="book-meta"><h1 class="book-title">Antelucem, tome 2 : La symphonie des ombres</h1>
<div class="hero-biblio-line"><a href="/editeur/3674/Auto-edition" class="hero-biblio-link">Auto édition</a><span>2025</span><span>637 pages</span></div>
<div class="hero-summary" id="d_bio">Réfugiée dans les profondeurs d'Antelucem...<br /> Lyra voit son rêve <span><button type="button" class="hero-voir-plus">Voir plus</button></span></div></div></article>
<table class="biblio-table"><tbody><tr><td>Auteur</td><td><a href="/auteur/Elie-Barbati/621657">Elie Barbati</a></td></tr><tr><td>Publication</td><td>21/07/2025</td></tr><tr><td>Pages</td><td>637</td></tr></tbody></table>
<script type="application/ld+json"> {"@context":"https://schema.org","@graph":[ {"@type":"Book","name":"Antelucem, tome 2 : La symphonie des ombres","author":[{"@type":"Person","name":"Elie Barbati","url":"https:\\/\\/www.babelio.com\\/auteur\\/Elie-Barbati\\/621657"}],"numberOfPages":637,"publisher":{"@type":"Organization","name":"Auto édition"},"datePublished":"2025-07-21","aggregateRating":{"@type":"AggregateRating","ratingValue":4.47,"ratingCount":89,"reviewCount":20}}, {"@type":"BreadcrumbList"} ]}</script>
</body></html>"""

BOOK_ISBN_HTML = BOOK_HTML.replace(
    "<tr><td>Pages</td><td>637</td></tr>",
    "<tr><td>Pages</td><td>637</td></tr><tr><td>EAN</td><td>9781032400548</td></tr>",
)

BOOK_ISBN_10_HTML = BOOK_HTML.replace(
    "<tr><td>Pages</td><td>637</td></tr>",
    "<tr><td>Pages</td><td>637</td></tr><tr><td>ISBN</td><td>2-35294-268-2</td></tr>",
)

BOOK_NO_JSONLD_HTML = BOOK_HTML.replace('type="application/ld+json"', 'type="text/plain"').replace(
    '<h1 class="book-title">Antelucem, tome 2 : La symphonie des ombres</h1>',
    '<h1 class="book-title">Antelucem, tome 2 : La symphonie des ombres</h1>'
    '<div class="book-meta"><a href="/auteur/Elie-Barbati/621657">Elie Barbati</a></div>',
)

CAPTCHA_HTML = (
    '<html lang="fr"><head><meta charset="ISO-8859-1">'
    "<title>Vérification de sécurité</title></head><body>"
    '<div class="captcha_container"><div id="slider-track" data-drag-distance="145">'
    '<div id="slider-thumb"></div></div></div>'
    '<script src="/js/rcaptcha.js?jj=1" defer></script></body></html>'
)


class _BabelioResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _BabelioCookies:
    """Stands in for the session's cookie jar so tests can watch clears."""

    def __init__(self):
        self.clear_calls = 0

    def clear(self):
        self.clear_calls += 1


class _BabelioSession:
    """Serves canned HTML per URL substring, first match wins.

    A page value may be a string (always served), an int (status code, empty
    body) or a list of strings served in order, the last one repeating.
    """

    def __init__(self, pages: dict[str, str | int | list[str]]):
        self.pages = pages
        self.urls: list[tuple[str, str]] = []
        self.post_data: list[dict | None] = []
        self.cookies = _BabelioCookies()
        self._served: dict[str, int] = {}

    @property
    def calls(self):
        return len(self.urls)

    def request(self, method: str, url: str, **kwargs):
        self.urls.append((method, url))
        if method == "POST":
            self.post_data.append(kwargs.get("data"))
        for fragment, page in self.pages.items():
            if fragment not in url:
                continue
            if isinstance(page, int):
                return _BabelioResponse("", status_code=page)
            responses = page if isinstance(page, list) else [page]
            index = self._served.get(fragment, 0)
            self._served[fragment] = index + 1
            return _BabelioResponse(responses[min(index, len(responses) - 1)])
        raise requests.HTTPError(f"unexpected URL: {url}")


def _provider(session: _BabelioSession) -> BabelioProvider:
    provider = BabelioProvider()
    provider.session = session
    return provider


def _search_options(**kwargs) -> MetadataSearchOptions:
    return MetadataSearchOptions(**kwargs)


class TestBabelioSearch:
    def test_search_parses_both_card_layouts(self):
        get_metadata_cache().clear()
        provider = _provider(_BabelioSession({"recherche.php": SEARCH_HTML}))

        books = provider.search(_search_options(query="horde du contrevent"))

        assert [b.provider_id for b in books] == ["1356017", "1993737", "5420"]
        book = books[0]
        assert book.provider == "babelio"
        assert book.title == "Fondation Deus, tome 1 : Le veilleur"
        assert book.authors == ["Pierre-Arnaud Francioso"]
        assert book.cover_url == "https://www.babelio.com/img/books/1356017.jpg"
        assert book.source_url == (
            "https://www.babelio.com/livres/Francioso-Fondation-Deus-tome-1--Le-veilleur/1356017"
        )
        assert {f.label: f.value for f in book.display_fields} == {"Rating": "3.7"}
        # A card without a rating or cover leaves both empty
        assert books[1].display_fields == []
        assert books[1].cover_url is None

    def test_search_posts_the_query(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"recherche.php": SEARCH_HTML})
        provider = _provider(session)

        provider.search(_search_options(query="horde du contrevent"))

        method, url = session.urls[0]
        assert (method, url) == ("POST", "https://www.babelio.com/recherche.php")
        assert session.post_data[0] == {
            "Recherche": "horde du contrevent",
            "recherche": "OK",
        }

    def test_search_filters_by_author(self):
        get_metadata_cache().clear()
        provider = _provider(_BabelioSession({"recherche.php": SEARCH_HTML}))

        books = provider.search(
            _search_options(
                query="passer la brume",
                fields={"title": "passer la brume", "author": "Julia Colin"},
            )
        )

        assert [b.provider_id for b in books] == ["1993737"]
        # Accent-insensitive author match
        books = provider.search(
            _search_options(
                query="passer la brume",
                fields={"title": "passer la brume", "author": "julia colin"},
            )
        )
        assert [b.provider_id for b in books] == ["1993737"]

    def test_author_search_filters_by_name(self):
        get_metadata_cache().clear()
        provider = _provider(_BabelioSession({"recherche.php": SEARCH_HTML}))

        books = provider.search(
            _search_options(query="Alain Damasio", search_type=SearchType.AUTHOR)
        )

        # The bibliography carousel's Damasio book, not the grid's hit by
        # another author
        assert [b.provider_id for b in books] == ["5420"]
        assert books[0].authors == ["Alain Damasio"]

    def test_author_search_matches_a_partial_name(self):
        get_metadata_cache().clear()
        provider = _provider(_BabelioSession({"recherche.php": SEARCH_HTML}))

        books = provider.search(_search_options(query="julia", search_type=SearchType.AUTHOR))

        assert [b.provider_id for b in books] == ["1993737"]

    def test_search_pages_beyond_the_first_are_empty(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"recherche.php": SEARCH_HTML})
        provider = _provider(session)

        books = provider.search(_search_options(query="horde", page=2))

        assert books == []
        assert session.calls == 0

    def test_search_isbn_type_delegates(self):
        get_metadata_cache().clear()
        provider = _provider(_BabelioSession({"recherche.php": SEARCH_HTML}))

        books = provider.search(_search_options(query="9781032400548", search_type=SearchType.ISBN))

        assert books == []


class TestBabelioBook:
    def test_get_book_parses_the_page(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"livres": BOOK_HTML})
        provider = _provider(session)

        book = provider.get_book("1892050")

        assert book is not None
        assert book.provider == "babelio"
        assert book.provider_id == "1892050"
        assert book.title == "Antelucem, tome 2 : La symphonie des ombres"
        assert book.authors == ["Elie Barbati"]
        assert book.publisher == "Auto édition"
        assert book.publish_year == 2025
        assert book.description == "Réfugiée dans les profondeurs d'Antelucem... Lyra voit son rêve"
        assert book.genres == ["Imaginaire", "Science fiction"]
        assert book.source_url == "https://www.babelio.com/livres/x/1892050"
        assert {f.label: f.value for f in book.display_fields} == {
            "Pages": "637",
            "Rating": "4.5",
            "Ratings": "89",
        }
        # Any slug works: /livres/x/<id> redirects to the canonical one
        assert session.urls[0] == ("GET", "https://www.babelio.com/livres/x/1892050")

    def test_get_book_accepts_a_link(self):
        get_metadata_cache().clear()
        provider = _provider(_BabelioSession({"livres": BOOK_HTML}))

        book = provider.get_book("/livres/Barbati-Antelucem-tome-2/1892050")

        assert book is not None
        assert book.provider_id == "1892050"

    def test_get_book_falls_back_to_html_without_json_ld(self):
        get_metadata_cache().clear()
        provider = _provider(_BabelioSession({"livres": BOOK_NO_JSONLD_HTML}))

        book = provider.get_book("1892050")

        assert book is not None
        assert book.title == "Antelucem, tome 2 : La symphonie des ombres"
        assert book.authors == ["Elie Barbati"]
        assert book.publisher == "Auto édition"
        assert book.publish_year == 2025
        assert {f.label: f.value for f in book.display_fields} == {"Pages": "637"}

    def test_get_book_rejects_a_non_book_page(self):
        get_metadata_cache().clear()
        provider = _provider(_BabelioSession({"livres": "<html><body>nothing</body></html>"}))

        assert provider.get_book("1892050") is None

    def test_get_book_rejects_a_bad_id(self):
        provider = _provider(_BabelioSession({}))

        assert provider.get_book("not-an-id") is None


class TestBabelioIsbn:
    def test_search_by_isbn_verifies_the_candidate(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"recherche.php": SEARCH_HTML, "livres/x": BOOK_ISBN_HTML})
        provider = _provider(session)

        book = provider.search_by_isbn("978-1-03-240054-8")

        assert book is not None
        assert book.isbn_13 == "9781032400548"
        assert book.isbn_10 is None

    def test_search_by_isbn_accepts_isbn_10(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"recherche.php": SEARCH_HTML, "livres/x": BOOK_ISBN_10_HTML})
        provider = _provider(session)

        book = provider.search_by_isbn("2352942682")

        assert book is not None
        assert book.isbn_10 == "2352942682"

    def test_search_by_isbn_skips_candidates_without_a_matching_isbn(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"recherche.php": SEARCH_HTML, "livres/x": BOOK_HTML})
        provider = _provider(session)

        assert provider.search_by_isbn("9782070541270") is None
        # One verification fetch per candidate, capped at three
        assert len([m for m, _ in session.urls if m == "GET"]) == 3

    def test_search_by_isbn_rejects_an_invalid_isbn(self):
        provider = _provider(_BabelioSession({}))

        assert provider.search_by_isbn("not-an-isbn") is None


class TestBabelioCaptcha:
    def test_captcha_clears_cookies_then_gives_up(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"recherche.php": CAPTCHA_HTML})
        provider = _provider(session)

        books = provider.search(_search_options(query="horde"))

        assert books == []
        assert session.cookies.clear_calls == 1
        # Cleared cookies, retried once, gave up
        assert session.calls == 2

    def test_captcha_retry_succeeds(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"recherche.php": [CAPTCHA_HTML, SEARCH_HTML]})
        provider = _provider(session)

        books = provider.search(_search_options(query="horde"))

        assert [b.provider_id for b in books] == ["1356017", "1993737", "5420"]

    def test_get_book_on_captcha_returns_none(self):
        get_metadata_cache().clear()
        session = _BabelioSession({"livres": CAPTCHA_HTML})
        provider = _provider(session)

        assert provider.get_book("1892050") is None


class TestBabelioHelpers:
    @pytest.mark.parametrize(
        ("link", "expected"),
        [
            ("/livres/la-horde-du-contrevent/5420", "5420"),
            ("https://www.babelio.com/livres/X/1892050/critiques", "1892050"),
            ("/livres/x/1356017", "1356017"),
            ("/auteur/Elie-Barbati/621657", ""),
            (None, ""),
        ],
    )
    def test_book_id_from_link(self, link, expected):
        assert _book_id_from_link(link) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("978-1-03-240054-8", "9781032400548"),
            ("2-35294-268-2", "2352942682"),
            ("2-35294-268-X", "235294268X"),
            ("12345", None),
            ("", None),
        ],
    )
    def test_valid_isbn(self, value, expected):
        assert _valid_isbn(value) == expected

    def test_author_matches(self):
        assert _author_matches("Julia Colin", "Julia Colin")
        assert _author_matches("julia", "Julia Colin")
        assert _author_matches("Alain Damasio", "Damasio Alain")
        assert _author_matches("barbati élie", "Elie Barbati")
        assert not _author_matches("julia roberts", "Julia Colin")
        assert not _author_matches("", "Julia Colin")
