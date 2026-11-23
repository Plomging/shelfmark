import asyncio
import json
import threading
from pathlib import Path

import pytest


async def _no_sleep(_seconds) -> None:
    return None


def test_diamwall_is_detected_until_the_challenge_clears():
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class DiamWallPage:
        async def get_title(self):
            return "DiamWall"

        async def evaluate(self, _expression):
            return "Please wait while DiamWall checks your browser"

        async def get_current_url(self):
            return "https://example.com"

    assert asyncio.run(internal_bypasser._detect_challenge_type(DiamWallPage())) == "other"
    assert not internal_bypasser._is_bypassed_content(
        "DiamWall",
        "Please wait while DiamWall checks your browser",
        "https://example.com",
    )


def test_bypass_tries_all_methods_before_abort(monkeypatch):
    """Regression test for issue #524: don't abort before cycling through bypass methods."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    calls: list[str] = []

    def _make_method(name: str):
        async def _method(_sb) -> bool:
            calls.append(name)
            return False

        _method.__name__ = name
        return _method

    methods = [_make_method(f"m{i}") for i in range(6)]

    async def _always_false(*_args, **_kwargs) -> bool:
        return False

    async def _always_ddos_guard(*_args, **_kwargs) -> str:
        return "ddos_guard"

    async def _no_sleep(_seconds) -> None:
        return None

    monkeypatch.setattr(internal_bypasser, "BYPASS_METHODS", methods)
    monkeypatch.setattr(internal_bypasser, "_is_bypassed", _always_false)
    monkeypatch.setattr(internal_bypasser, "_detect_challenge_type", _always_ddos_guard)
    monkeypatch.setattr(internal_bypasser.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(internal_bypasser.random, "uniform", lambda _a, _b: 0)

    assert asyncio.run(internal_bypasser._bypass(object(), max_retries=10)) is False
    assert calls == [f"m{i}" for i in range(6)]


def test_extract_cookies_from_cdp_filters_and_stores_ua():
    import time

    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class FakeCookie:
        def __init__(self, name, value, domain, path, expires, secure=True):
            self.name = name
            self.value = value
            self.domain = domain
            self.path = path
            self.expires = expires
            self.secure = secure

    class FakeCookies:
        async def get_all(self, requests_cookie_format=False):
            assert requests_cookie_format is True
            return [
                FakeCookie("cf_clearance", "abc", "example.com", "/", int(time.time()) + 3600),
                FakeCookie("sessionid", "zzz", "example.com", "/", int(time.time()) + 3600),
            ]

    class FakeDriver:
        cookies = FakeCookies()

    class FakePage:
        async def evaluate(self, _expr):
            return "TestUA/1.0"

    internal_bypasser.clear_cf_cookies()
    asyncio.run(
        internal_bypasser._extract_cookies_from_cdp(
            FakeDriver(),
            FakePage(),
            "https://www.example.com/path",
        )
    )

    cookies = internal_bypasser.get_cf_cookies_for_domain("example.com")
    assert cookies == {"cf_clearance": "abc"}
    assert internal_bypasser.get_cf_user_agent_for_domain("example.com") == "TestUA/1.0"


def test_extract_cookies_from_cdp_keeps_full_session_cookies_for_configured_zlib_domains(
    monkeypatch,
):
    import time

    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class FakeCookie:
        def __init__(self, name, value, domain, path, expires, secure=True):
            self.name = name
            self.value = value
            self.domain = domain
            self.path = path
            self.expires = expires
            self.secure = secure

    class FakeCookies:
        async def get_all(self, requests_cookie_format=False):
            assert requests_cookie_format is True
            return [
                FakeCookie("cf_clearance", "abc", "z-lib.fm", "/", int(time.time()) + 3600),
                FakeCookie("sessionid", "zzz", "z-lib.fm", "/", int(time.time()) + 3600),
            ]

    class FakeDriver:
        cookies = FakeCookies()

    class FakePage:
        async def evaluate(self, _expr):
            return "TestUA/1.0"

    from shelfmark.bypass import cookie_store

    monkeypatch.setattr(cookie_store, "_get_full_cookie_domains", lambda: {"z-lib.fm"})

    internal_bypasser.clear_cf_cookies()
    asyncio.run(
        internal_bypasser._extract_cookies_from_cdp(
            FakeDriver(),
            FakePage(),
            "https://z-lib.fm/books/example",
        )
    )

    cookies = internal_bypasser.get_cf_cookies_for_domain("z-lib.fm")
    assert cookies == {"cf_clearance": "abc", "sessionid": "zzz"}


def test_extract_cookies_from_cdp_normalizes_session_expiry():
    import time

    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class FakeCookie:
        def __init__(self, name, value, domain, path, expires, secure=True):
            self.name = name
            self.value = value
            self.domain = domain
            self.path = path
            self.expires = expires
            self.secure = secure

    class FakeCookies:
        async def get_all(self, requests_cookie_format=False):
            assert requests_cookie_format is True
            return [
                FakeCookie("cf_clearance", "abc", "example.com", "/", 0),
            ]

    class FakeDriver:
        cookies = FakeCookies()

    class FakePage:
        async def evaluate(self, _expr):
            return "TestUA/1.0"

    internal_bypasser.clear_cf_cookies()
    asyncio.run(
        internal_bypasser._extract_cookies_from_cdp(
            FakeDriver(),
            FakePage(),
            "https://example.com",
        )
    )

    from shelfmark.bypass import cookie_store

    stored = cookie_store._cf_cookies.get("example.com", {})
    assert stored["cf_clearance"]["expiry"] is None
    assert internal_bypasser.get_cf_cookies_for_domain("example.com") == {"cf_clearance": "abc"}

    # Verify fallback to "expires" key for expiry checks
    cookie_store._cf_cookies["example.com"]["cf_clearance"]["expires"] = int(time.time()) - 10
    assert internal_bypasser.get_cf_cookies_for_domain("example.com") == {}


def test_get_page_info_returns_safe_defaults_on_cdp_errors():
    from seleniumbase.undetected.cdp_driver.connection import ProtocolException

    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class FakePage:
        async def get_title(self):
            raise ProtocolException("no title")

        async def evaluate(self, _expr):
            raise ProtocolException("no body")

        async def get_current_url(self):
            raise ProtocolException("no url")

    title, body, current_url = asyncio.run(internal_bypasser._get_page_info(FakePage()))

    assert title == ""
    assert body == ""
    assert current_url == ""


def test_create_cdp_browser_times_out_and_cleans_up(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    async def _never_start(*_args, **_kwargs):
        await asyncio.Event().wait()

    cleanup_calls = []

    monkeypatch.setattr(internal_bypasser, "_BROWSER_START_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(internal_bypasser.cdp_driver, "start_async", _never_start)
    monkeypatch.setattr(internal_bypasser, "_get_browser_args", lambda: [])
    monkeypatch.setattr(internal_bypasser, "get_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(internal_bypasser, "_get_proxy_string", lambda _url: None)
    monkeypatch.setattr(internal_bypasser.env, "DOCKERMODE", True)
    monkeypatch.setattr(
        internal_bypasser,
        "_cleanup_orphan_processes",
        lambda: cleanup_calls.append("cleanup") or 1,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(internal_bypasser._create_cdp_browser("https://example.com"))

    assert cleanup_calls == ["cleanup"]


def test_create_cdp_browser_wraps_plain_startup_exception_and_cleans_up(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    async def _fail_to_start(*_args, **_kwargs):
        raise Exception("Failed to connect to the browser")

    cleanup_calls = []

    monkeypatch.setattr(internal_bypasser.cdp_driver, "start_async", _fail_to_start)
    monkeypatch.setattr(internal_bypasser, "_get_browser_args", lambda: [])
    monkeypatch.setattr(internal_bypasser, "get_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(internal_bypasser, "_get_proxy_string", lambda _url: None)
    monkeypatch.setattr(internal_bypasser.env, "DOCKERMODE", True)
    monkeypatch.setattr(
        internal_bypasser,
        "_cleanup_orphan_processes",
        lambda: cleanup_calls.append("cleanup") or 1,
    )

    with pytest.raises(RuntimeError, match="Pure CDP browser startup failed"):
        asyncio.run(internal_bypasser._create_cdp_browser("https://example.com"))

    assert cleanup_calls == ["cleanup"]


def test_run_child_process_writes_failure_for_unexpected_exception(monkeypatch, tmp_path):
    import io
    import json

    import shelfmark.bypass.internal_bypasser as internal_bypasser

    result_path = tmp_path / "result.json"
    request = {
        "url": "https://example.com",
        "retry": 1,
        "result_path": str(result_path),
    }

    def _raise_unexpected(*_args, **_kwargs):
        raise Exception("plain SeleniumBase startup failure")

    monkeypatch.setattr(internal_bypasser, "get", _raise_unexpected)
    monkeypatch.setattr(internal_bypasser.sys, "stdin", io.StringIO(json.dumps(request)))

    assert internal_bypasser._run_child_process() == 1

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result["error_type"] == "Exception"
    assert result["error"] == "plain SeleniumBase startup failure"
    assert "plain SeleniumBase startup failure" in result["traceback"]


def test_run_child_process_applies_parent_dns_config(monkeypatch, tmp_path):
    """Regression test for issue #1028: the helper subprocess must mirror the parent's
    DNS provider, otherwise it pre-resolves AA hostnames against (possibly hijacked)
    system DNS and Chrome loads the wrong page."""
    import io
    import json

    import shelfmark.bypass.internal_bypasser as internal_bypasser

    result_path = tmp_path / "result.json"
    request = {
        "url": "https://annas-archive.pk/slow_download/abc/0/0",
        "retry": 1,
        "result_path": str(result_path),
        "dns_config": {
            "provider": "cloudflare",
            "servers": ["1.1.1.1", "1.0.0.1"],
            "doh_url": "https://cloudflare-dns.com/dns-query",
            "doh_enabled": True,
            "is_auto_mode": True,
        },
    }

    applied: list[tuple] = []
    monkeypatch.setattr(
        internal_bypasser.network,
        "set_dns_provider",
        lambda provider, manual=None, *, use_doh=None: applied.append((provider, manual, use_doh)),
    )
    monkeypatch.setattr(internal_bypasser, "get", lambda *_a, **_k: "<html>ok</html>")
    monkeypatch.setattr(internal_bypasser.sys, "stdin", io.StringIO(json.dumps(request)))

    assert internal_bypasser._run_child_process() == 0
    assert applied == [("cloudflare", None, True)]


def test_apply_parent_dns_config_skips_auto_and_empty(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    calls: list = []
    monkeypatch.setattr(
        internal_bypasser.network,
        "set_dns_provider",
        lambda *a, **k: calls.append((a, k)),
    )

    internal_bypasser._apply_parent_dns_config({"provider": "auto"})
    internal_bypasser._apply_parent_dns_config({})

    assert calls == []


def test_apply_parent_dns_config_forwards_manual_servers(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    calls: list = []
    monkeypatch.setattr(
        internal_bypasser.network,
        "set_dns_provider",
        lambda provider, manual=None, *, use_doh=None: calls.append((provider, manual, use_doh)),
    )

    internal_bypasser._apply_parent_dns_config(
        {"provider": "manual", "servers": ["9.9.9.9"], "doh_enabled": False}
    )

    assert calls == [("manual", ["9.9.9.9"], False)]


def test_prepare_child_browser_env_uses_writable_runtime_paths(monkeypatch, tmp_path):
    import stat

    import shelfmark.bypass.internal_bypasser as internal_bypasser

    home_dir = tmp_path / "browser" / "home"
    runtime_dir = tmp_path / "browser" / "runtime"
    monkeypatch.setattr(internal_bypasser, "BROWSER_HOME_DIR", home_dir)
    monkeypatch.setattr(internal_bypasser, "BROWSER_XDG_RUNTIME_DIR", runtime_dir)

    env = internal_bypasser._prepare_child_browser_env({"HOME": "/app"})

    assert env["HOME"] == str(home_dir)
    assert env["XDG_CONFIG_HOME"] == str(home_dir / ".config")
    assert env["XDG_CACHE_HOME"] == str(home_dir / ".cache")
    assert env["XDG_RUNTIME_DIR"] == str(runtime_dir)
    assert home_dir.is_dir()
    assert (home_dir / ".config").is_dir()
    assert (home_dir / ".cache").is_dir()
    assert stat.S_IMODE(runtime_dir.stat().st_mode) == stat.S_IRWXU


def test_try_with_cached_cookies_returns_none_on_request_exception(monkeypatch):
    import time

    import requests

    import shelfmark.bypass.internal_bypasser as internal_bypasser

    internal_bypasser.clear_cf_cookies()
    from shelfmark.bypass import cookie_store

    cookie_store._cf_cookies["example.com"] = {
        "cf_clearance": {
            "value": "abc",
            "domain": "example.com",
            "path": "/",
            "expiry": int(time.time()) + 3600,
            "secure": True,
            "httpOnly": True,
        }
    }

    def _raise(*_args, **_kwargs):
        raise requests.RequestException("boom")

    monkeypatch.setattr(internal_bypasser.requests, "get", _raise)

    assert internal_bypasser._try_with_cached_cookies("https://example.com", "example.com") is None


def test_get_bypassed_page_retries_next_mirror_after_runtime_error(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class FakeSelector:
        def __init__(self):
            self.urls = ["https://mirror-one.example/book", "https://mirror-two.example/book"]
            self.index = 0

        def rewrite(self, _url):
            return self.urls[self.index]

        def next_mirror_or_rotate_dns(self, *, allow_dns=True):
            del allow_dns
            self.index = 1
            return "https://mirror-two.example", "mirror"

    calls: list[str] = []

    def _fake_get(url, retry=None, cancel_flag=None):
        del retry, cancel_flag
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("browser hiccup")
        return "<html>ok</html>"

    monkeypatch.setattr(
        internal_bypasser, "_try_with_cached_cookies", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(internal_bypasser, "get", _fake_get)

    selector = FakeSelector()
    result = internal_bypasser.get_bypassed_page("https://orig.example/book", selector=selector)

    assert result == "<html>ok</html>"
    assert calls == [
        "https://mirror-one.example/book",
        "https://mirror-two.example/book",
    ]


def test_max_duration_seconds_allows_for_a_mirror_rotation_retry():
    """get_bypassed_page() may call get() twice, so the budget must cover both."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    assert internal_bypasser.max_duration_seconds() == (
        2 * internal_bypasser._BYPASS_SUBPROCESS_TIMEOUT_SECONDS
    )


def test_cdp_worker_run_times_out_and_cancels_the_orphaned_coroutine(monkeypatch):
    """A wedged in-process bypass must not block forever holding LOCKED.

    Regression guard: _CDP_WORKER.run() used to wait with timeout=None, so one hung CDP
    session blocked every subsequent bypass in the process indefinitely.
    """
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    started = threading.Event()

    async def _never_finish():
        started.set()
        await asyncio.Event().wait()

    coro = _never_finish()
    with pytest.raises(TimeoutError):
        internal_bypasser._CDP_WORKER.run(coro, timeout=0.05)

    assert started.is_set(), "coroutine should have been scheduled before timing out"


def test_run_bypass_in_current_process_bounds_its_wait(monkeypatch):
    """The in-process path passes a deadline rather than waiting forever."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    observed: dict[str, float | None] = {}

    class _FakeWorker:
        def run(self, coro, timeout=None):
            observed["timeout"] = timeout
            coro.close()
            return "html"

    monkeypatch.setattr(internal_bypasser, "_CDP_WORKER", _FakeWorker())
    monkeypatch.delenv("SHELFMARK_INTERNAL_BYPASSER_CHILD", raising=False)

    result = internal_bypasser._run_bypass_in_current_process("https://example.com", 1)

    assert result == "html"
    assert observed["timeout"] == internal_bypasser._IN_PROCESS_BYPASS_TIMEOUT_SECONDS


def _write_fake_proc_entry(proc_root, pid: int, pgid: int, argv: list[str]) -> None:
    """Create a /proc-shaped entry for a fake process."""
    entry = proc_root / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    # pid (comm) state ppid pgrp ... - comm is parenthesised and may contain spaces.
    (entry / "stat").write_text(f"{pid} (some (odd) name) S 1 {pgid} {pgid} 0 -1 4194304 0 0")


def test_cleanup_only_kills_own_and_abandoned_browser_sessions(monkeypatch, tmp_path):
    """Regression test for issue #1231: the sweep used a container-wide `pkill -f chrome`,
    so every worker that started a bypass killed the browsers the other workers were
    still driving. Only our own process group and groups whose leader is gone are ours."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_fake_proc_entry(proc_root, 1000, 1000, ["python", "-m", "shelfmark.bypass"])
    _write_fake_proc_entry(proc_root, 1001, 1000, ["/usr/bin/chromium", "--headless"])
    _write_fake_proc_entry(proc_root, 1002, 1000, ["Xvfb", ":99"])
    # Live sibling session: another worker is solving a challenge with these right now.
    _write_fake_proc_entry(proc_root, 2000, 2000, ["python", "-m", "shelfmark.bypass"])
    _write_fake_proc_entry(proc_root, 2001, 2000, ["/usr/bin/chromium", "--headless"])
    # Abandoned session: its leader (pid 3000) is gone, so its browser really is an orphan.
    _write_fake_proc_entry(proc_root, 3001, 3000, ["/usr/bin/chromium", "--headless"])

    killed: list[int] = []

    monkeypatch.setattr(internal_bypasser.env, "DOCKERMODE", True)
    monkeypatch.setattr(internal_bypasser, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(internal_bypasser.os, "getpid", lambda: 1000)
    monkeypatch.setattr(internal_bypasser.os, "getpgrp", lambda: 1000)
    monkeypatch.setattr(internal_bypasser.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(internal_bypasser.time, "sleep", lambda _seconds: None)

    assert internal_bypasser._cleanup_orphan_processes() == 3
    assert sorted(killed) == [1001, 1002, 3001]


def test_cleanup_is_skipped_without_proc(monkeypatch, tmp_path):
    """Without /proc there is no way to tell sessions apart, so kill nothing."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    monkeypatch.setattr(internal_bypasser.env, "DOCKERMODE", True)
    monkeypatch.setattr(internal_bypasser, "_PROC_ROOT", tmp_path / "missing")
    monkeypatch.setattr(
        internal_bypasser.os, "kill", lambda *_args: pytest.fail("must not kill anything")
    )

    assert internal_bypasser._cleanup_orphan_processes() == 0


class _FakeHelperStdin:
    """The request pipe: a write is how the helper receives one request."""

    def __init__(self, process):
        self._process = process
        self.closed = False

    def write(self, data):
        self._process.serve(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _FakeHelperProcess:
    """Stand-in for the bypass helper subprocess.

    The helper serves one request per line of stdin and answers by writing the result file
    the request named, so that is what this fakes: a write produces an answer.
    """

    def __init__(self, *_args, **kwargs):
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None
        self.answers = True
        self.killed = False
        self.waited = False
        self.stdin = _FakeHelperStdin(self)
        self.requests: list[dict] = []

    def serve(self, payload):
        request = json.loads(payload)
        self.requests.append(request)
        if not self.answers:
            return
        result = {"ok": True, "html": "<html>solved</html>", "cookies": {}, "user_agents": {}}
        Path(request["result_path"]).write_text(json.dumps(result), encoding="utf-8")

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _patch_helper_subprocess(monkeypatch, internal_bypasser, process, killed_groups):
    monkeypatch.setattr(internal_bypasser.subprocess, "Popen", lambda *a, **kw: process(*a, **kw))
    monkeypatch.setattr(internal_bypasser.network, "get_dns_config", dict)
    monkeypatch.setattr(
        internal_bypasser.os, "killpg", lambda pgid, _sig: killed_groups.append(pgid)
    )
    # A fresh helper per test: the module-level one is shared, and a process parked by one
    # test would be handed to the next.
    helper = internal_bypasser._BypassHelper()
    monkeypatch.setattr(internal_bypasser._BypassHelper, "_idle_timeout", lambda _self: 0.0)
    monkeypatch.setattr(internal_bypasser, "_BYPASS_HELPER", helper)
    return helper


def test_helper_runs_in_its_own_session_and_is_torn_down(monkeypatch):
    """Regression test for issue #1231: the helper's Chrome and Xvfb must belong to the
    helper's own process group, and the whole group must die with it - otherwise the
    leftovers break the next worker's browser and can only be cleared by a sweep broad
    enough to kill a concurrent worker's browser too.

    The helper outlives a single request, so the teardown happens when it is dropped rather
    than after every solve. Each bypass still closes its own browser, so what survives in
    between is the process, not a Chrome.
    """
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    processes: list[_FakeHelperProcess] = []
    killed_groups: list[int] = []

    def _make_process(*args, **kwargs):
        process = _FakeHelperProcess(*args, **kwargs)
        processes.append(process)
        return process

    helper = _patch_helper_subprocess(monkeypatch, internal_bypasser, _make_process, killed_groups)

    assert internal_bypasser._get_via_subprocess("https://example.com", 1) == "<html>solved</html>"
    assert processes[0].kwargs["start_new_session"] is True
    assert killed_groups == [], "the helper was torn down after a single request"

    helper._discard()

    assert killed_groups == [processes[0].pid]


def test_helper_serves_a_second_request_without_respawning(monkeypatch):
    """The interpreter start and imports are paid once, not per protected request."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    processes: list[_FakeHelperProcess] = []
    killed_groups: list[int] = []

    def _make_process(*args, **kwargs):
        process = _FakeHelperProcess(*args, **kwargs)
        processes.append(process)
        return process

    _patch_helper_subprocess(monkeypatch, internal_bypasser, _make_process, killed_groups)

    internal_bypasser._get_via_subprocess("https://example.com/one", 1)
    internal_bypasser._get_via_subprocess("https://example.com/two", 1)

    assert len(processes) == 1
    assert [request["url"] for request in processes[0].requests] == [
        "https://example.com/one",
        "https://example.com/two",
    ]


def test_helper_timeout_kills_the_whole_session(monkeypatch):
    """A timed-out solve must not leave a live browser behind for the next worker."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    processes: list[_FakeHelperProcess] = []
    killed_groups: list[int] = []

    def _make_process(*args, **kwargs):
        process = _FakeHelperProcess(*args, **kwargs)
        process.answers = False  # accepts the request, never writes a result
        processes.append(process)
        return process

    _patch_helper_subprocess(monkeypatch, internal_bypasser, _make_process, killed_groups)
    monkeypatch.setattr(internal_bypasser, "_BYPASS_SUBPROCESS_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(TimeoutError):
        internal_bypasser._get_via_subprocess("https://example.com", 1)

    assert killed_groups == [processes[0].pid]
    assert processes[0].killed is True


def test_helper_takes_the_browser_down_when_its_parent_dies(monkeypatch):
    """Cleanup only reclaims process groups whose leader is gone (#1231), so an orphaned
    helper must not sit there holding a browser no later bypass is allowed to touch."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    terminated: list[str] = []

    monkeypatch.setattr(internal_bypasser.os, "getppid", lambda: 1)
    monkeypatch.setattr(
        internal_bypasser, "_terminate_own_session", lambda: terminated.append("terminated")
    )
    monkeypatch.setattr(
        internal_bypasser.time, "sleep", lambda _seconds: pytest.fail("should not wait")
    )

    internal_bypasser._watch_parent_process(999, interval=0.0)

    assert terminated == ["terminated"]


def test_helper_watchdog_waits_while_its_parent_is_alive(monkeypatch):
    """The watchdog must only fire on a changed ppid, not on every poll."""
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    ppids = iter([999, 999, 1])
    sleeps: list[float] = []

    monkeypatch.setattr(internal_bypasser.os, "getppid", lambda: next(ppids))
    monkeypatch.setattr(internal_bypasser, "_terminate_own_session", lambda: None)
    monkeypatch.setattr(internal_bypasser.time, "sleep", sleeps.append)

    internal_bypasser._watch_parent_process(999, interval=0.5)

    assert sleeps == [0.5, 0.5]


def test_child_request_with_post_data_routes_to_post(monkeypatch, tmp_path):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    result_path = tmp_path / "result.json"
    request = {
        "url": "https://www.babelio.com/recherche.php",
        "retry": 1,
        "result_path": str(result_path),
        "post_data": {"Recherche": "horde", "recherche": "OK"},
    }

    posts: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        internal_bypasser,
        "post",
        lambda url, data, retry=None: posts.append((url, data)) or "<html>results</html>",
    )
    result = internal_bypasser._handle_child_request(json.dumps(request))

    assert result == 0
    assert json.loads(result_path.read_text(encoding="utf-8"))["html"] == "<html>results</html>"
    assert posts == [
        ("https://www.babelio.com/recherche.php", {"Recherche": "horde", "recherche": "OK"})
    ]


def test_child_request_without_post_data_routes_to_get(monkeypatch, tmp_path):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    result_path = tmp_path / "result.json"
    request = {"url": "https://example.com", "retry": 1, "result_path": str(result_path)}

    calls: list[str] = []

    def _fail_post(*_args, **_kwargs):
        calls.append("post")
        raise AssertionError("post must not be called without post_data")

    monkeypatch.setattr(internal_bypasser, "get", lambda url, retry=None: calls.append("get"))
    monkeypatch.setattr(internal_bypasser, "post", _fail_post)

    assert internal_bypasser._handle_child_request(json.dumps(request)) == 0
    assert calls == ["get"]


def test_post_form_js_builds_a_same_origin_sync_xhr():
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    js = internal_bypasser._post_form_js(
        "https://www.babelio.com/recherche.php?ref=1", {"Recherche": "horde & fils"}
    )

    assert "x.open('POST',\"/recherche.php?ref=1\",false)" in js
    assert 'x.send("Recherche=horde+%26+fils")' in js
    assert "JSON.stringify({s:x.status,t:x.responseText})" in js


def test_post_aborts_when_the_origin_page_is_a_challenge(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class _Page:
        async def wait(self):
            return None

        async def get_title(self):
            return "Vérification de sécurité"

        async def evaluate(self, js):  # the slider probe is fine, the XHR is not
            if "dataset.dragDistance" in js:
                return None  # no slider on this challenged page
            raise AssertionError("XHR must not run on a challenged page")

    class _Driver:
        async def get(self, _url):
            return _Page()

    assert (
        asyncio.run(internal_bypasser._post("https://www.babelio.com/recherche.php", {}, _Driver()))
        == ""
    )


def test_post_returns_the_page_html_and_extracts_cookies(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class _Page:
        async def wait(self):
            return None

        async def get_title(self):
            return "Babelio - Découvrez des livres"

        async def evaluate(self, _js):
            return '{"s": 200, "t": "<html>results</html>"}'

    class _Driver:
        async def get(self, _url):
            return _Page()

    extracted: list[str] = []

    async def _extract(driver, page, url):
        extracted.append(url)

    monkeypatch.setattr(internal_bypasser, "_extract_cookies_from_cdp", _extract)

    html = asyncio.run(
        internal_bypasser._post(
            "https://www.babelio.com/recherche.php", {"Recherche": "horde"}, _Driver()
        )
    )

    assert html == "<html>results</html>"
    assert extracted == ["https://www.babelio.com/recherche.php"]


def test_post_reports_a_non_200_status(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class _Page:
        async def wait(self):
            return None

        async def get_title(self):
            return "Babelio"

        async def evaluate(self, _js):
            return '{"s": 403, "t": ""}'

    class _Driver:
        async def get(self, _url):
            return _Page()

    assert (
        asyncio.run(internal_bypasser._post("https://www.babelio.com/recherche.php", {}, _Driver()))
        == ""
    )


def test_plain_http_is_blocked_for_babelio(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    # babelio.com scores client fingerprints even when serving 200, so no plain
    # request may ever be sent to it - not even one replaying browser cookies
    def _fail(*_args, **_kwargs):
        raise AssertionError("a plain HTTP request was sent to babelio.com")

    monkeypatch.setattr(internal_bypasser.requests, "get", _fail)
    monkeypatch.setattr(
        internal_bypasser, "get_cf_cookies_for_domain", lambda _d: {"cf_clearance": "abc"}
    )

    assert (
        internal_bypasser._try_with_cached_cookies("https://www.babelio.com/", "www.babelio.com")
        is None
    )
    # Subdomains are covered too, and unrelated hosts stay on the normal path
    assert internal_bypasser._plain_http_blocked("https://forum.babelio.com/livres")
    assert not internal_bypasser._plain_http_blocked("https://annas-archive.gl/search")


def test_seed_cached_cookies_replays_the_store_into_a_fresh_browser(monkeypatch):
    import shelfmark.bypass.cookie_store as cookie_store
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    cookie_store._cf_cookies["babelio.com"] = {
        "p": {
            "value": "old-session",
            "domain": ".babelio.com",
            "path": "/",
            "expiry": None,
            "secure": False,
            "httpOnly": True,
        },
        "dead": {
            "value": "gone",
            "domain": ".babelio.com",
            "path": "/",
            "expiry": 1,
            "secure": False,
            "httpOnly": True,
        },
    }
    seeded = []

    class FakeCookieParam:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeCookies:
        async def set_all(self, cookies):
            seeded.extend(cookies)

    class FakeDriver:
        cookies = FakeCookies()

    monkeypatch.setattr(internal_bypasser.cdp.network, "CookieParam", FakeCookieParam)

    asyncio.run(internal_bypasser._seed_cached_cookies(FakeDriver(), "https://www.babelio.com/"))

    # The expired cookie is filtered out, the live one seeded with its metadata
    assert len(seeded) == 1
    cookie = seeded[0]
    assert cookie.name == "p"
    assert cookie.value == "old-session"
    assert cookie.domain == ".babelio.com"
    assert cookie.path == "/"


def test_seed_cached_cookies_is_a_noop_without_a_store():
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class FakeCookies:
        async def set_all(self, cookies):
            raise AssertionError("set_all should not be called")

    class FakeDriver:
        cookies = FakeCookies()

    asyncio.run(internal_bypasser._seed_cached_cookies(FakeDriver(), "https://example.com/"))


def test_babelio_session_cookies_are_extracted_in_full(monkeypatch):
    import time

    import shelfmark.bypass.cookie_store as cookie_store
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class FakeCookie:
        def __init__(self, name, value, domain, path, expires, secure=True):
            self.name = name
            self.value = value
            self.domain = domain
            self.path = path
            self.expires = expires
            self.secure = secure

    class FakeCookies:
        async def get_all(self, requests_cookie_format=False):
            assert requests_cookie_format is True
            return [
                FakeCookie("cf_clearance", "abc", "babelio.com", "/", int(time.time()) + 3600),
                FakeCookie("g_state", "stored", "babelio.com", "/", int(time.time()) + 3600),
            ]

    class FakeDriver:
        cookies = FakeCookies()

    class FakePage:
        async def evaluate(self, _expr):
            return "TestUA/1.0"

    monkeypatch.setattr(
        cookie_store, "_get_full_cookie_domains", lambda: set(cookie_store.FULL_SESSION_DOMAINS)
    )

    internal_bypasser.clear_cf_cookies()
    asyncio.run(
        internal_bypasser._extract_cookies_from_cdp(
            FakeDriver(),
            FakePage(),
            "https://www.babelio.com/",
        )
    )

    cookies = internal_bypasser.get_cf_cookies_for_domain("babelio.com")
    assert cookies == {"cf_clearance": "abc", "g_state": "stored"}


def _slider_page(infos, challenge_answers=None):
    """Fake CDP page: answers the slider probes and records every mouse event."""
    events = []
    evaluates = []
    remaining = list(infos)
    answers = list(challenge_answers or [])

    class FakePage:
        async def evaluate(self, js):
            evaluates.append(js)
            if "dataset.dragDistance" in js:
                return remaining.pop(0) if remaining else None
            return answers.pop(0) if answers else False

        async def send(self, command):
            # CDP commands are generators yielding {method, params}; consume
            # them the way the real tab does and keep the params for asserts.
            params = next(command)["params"]
            command.close()
            events.append(params)
            return {}

    return FakePage(), events, evaluates


def test_babelio_slider_solves_the_challenge_with_real_mouse_events(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    monkeypatch.setattr(internal_bypasser.asyncio, "sleep", _no_sleep)
    page, events, evaluates = _slider_page([{"distance": 147, "thumbX": 395.0, "thumbY": 540.0}])

    assert asyncio.run(
        internal_bypasser._solve_babelio_slider(page, "https://www.babelio.com/livres/x/1")
    )

    # Pointer is moved onto the thumb first, then the button is held for the
    # whole drag (buttons=1) exactly as SeleniumBase's own drag does
    assert events[0]["type"] == "mouseMoved"
    press = next(e for e in events if e["type"] == "mousePressed")
    release = next(e for e in events if e["type"] == "mouseReleased")
    assert press["buttons"] == 1 and press["x"] == 395.0
    moves = events[events.index(press) + 1 :]
    assert all(e["buttons"] == 1 for e in moves if e["type"] == "mouseMoved")
    assert release["buttons"] == 0
    assert sum(e["type"] == "mouseMoved" for e in events) >= 26
    assert abs(release["x"] - (395.0 + 147)) <= 10
    assert len(evaluates) == 2  # info probe + challenge check


def test_babelio_slider_retries_with_a_fresh_distance(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    monkeypatch.setattr(internal_bypasser.asyncio, "sleep", _no_sleep)
    page, events, evaluates = _slider_page(
        [
            {"distance": 147, "thumbX": 395.0, "thumbY": 540.0},
            {"distance": 209, "thumbX": 364.0, "thumbY": 531.0},
        ],
        challenge_answers=[True, False],
    )

    assert asyncio.run(internal_bypasser._solve_babelio_slider(page, "https://www.babelio.com/"))
    presses = [e for e in events if e["type"] == "mousePressed"]
    releases = [e for e in events if e["type"] == "mouseReleased"]
    assert len(presses) == 2 and len(releases) == 2
    assert presses[1]["x"] == 364.0
    assert abs(releases[1]["x"] - (364.0 + 209)) <= 10
    assert len(evaluates) == 4  # 2 info probes + 2 challenge checks


def test_babelio_slider_skipped_for_other_hosts():
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    class FakePage:
        async def evaluate(self, _js):
            raise AssertionError("the slider solver must not touch other hosts")

    assert not asyncio.run(
        internal_bypasser._solve_babelio_slider(FakePage(), "https://example.com/")
    )


def test_babelio_slider_noop_without_a_slider(monkeypatch):
    import shelfmark.bypass.internal_bypasser as internal_bypasser

    monkeypatch.setattr(internal_bypasser.asyncio, "sleep", _no_sleep)

    class FakePage:
        async def evaluate(self, _js):
            return None

        async def send(self, _event):
            raise AssertionError("no mouse events expected without a slider")

    assert not asyncio.run(
        internal_bypasser._solve_babelio_slider(FakePage(), "https://www.babelio.com/")
    )
