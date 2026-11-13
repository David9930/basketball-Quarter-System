"""Paced, robots-aware page fetching.

One request at a time, with a fixed pause after every request. The collector
identifies itself truthfully, obeys robots.txt, and ends the run if the site
refuses it. It does not rotate addresses, imitate a browser, or retry a refusal.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path

import requests


class StopRun(Exception):
    """The run must end now: the site refused us or keeps failing."""


class ReaderAlarm(StopRun):
    """The site's pages no longer read the way they did; stop before bad data is recorded."""


class SiteTrouble(StopRun):
    """The site sent its own short "we are having problems" notice instead of pages."""


class BudgetReached(Exception):
    """This run's time or request allowance is used up."""


class PageError(Exception):
    """One page could not be fetched; the run carries on."""


NOTICE_MAX_CHARS = 600          # a real page runs to tens of thousands of characters
NOTICE_WORDS = ("technical problem", "try later", "try again", "maintenance", "temporarily",
                "unavailable", "too many", "overload")


def site_notice(text):
    """Return the site's notice if that is all it sent, otherwise "".

    When the site is unwell it still answers HTTP 200, with one plain line such
    as "Currently we experience technical problems. Please try later or contact
    us." That is not a match report or a match list and must never be read as one.
    """
    body = " ".join((text or "").split())
    if body and len(body) <= NOTICE_MAX_CHARS and "<" not in body:
        if any(word in body.lower() for word in NOTICE_WORDS):
            return body
    return ""


class Fetcher:
    def __init__(self, cfg, root, max_minutes=0, max_requests=0, use_cache=True, log=print):
        ident, pace = cfg["identity"], cfg["pace"]
        self.base = cfg["site"]["base_url"].rstrip("/")
        agent = ident["user_agent"].strip()
        contact = (ident.get("contact") or "").strip()
        self.user_agent = f"{agent} (+{contact})" if contact else agent
        self.agent_token = agent.split("/")[0].split()[0]
        self.delay = float(pace["delay_seconds"])
        self.timeout = float(pace["timeout_seconds"])
        self.max_retries = int(pace["max_retries"])
        self.max_wait = float(pace["max_wait_seconds"])
        self.failure_limit = int(pace["stop_after_consecutive_failures"])
        self.cache_dir = Path(root) / "cache"
        self.use_cache = use_cache
        self.log = log
        self.max_requests = int(max_requests or 0)
        self.deadline = time.monotonic() + float(max_minutes) * 60 if max_minutes else None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "en",
        })
        self.requests = 0
        self.cache_hits = 0
        self.robots_refusals = 0
        self.consecutive_failures = 0
        self.robots_text = None
        self._robots = None
        self._last_done = 0.0

    # ------------------------------------------------------------------ public

    def url(self, path_or_url):
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        return self.base + "/" + path_or_url.lstrip("/")

    def get(self, path_or_url, data=None, cache=False, max_age_hours=None, refresh=False):
        """Return the page text. `data` makes it a form POST.

        cache=True keeps a compressed copy on disk and reuses it; max_age_hours
        limits how old a reused copy may be; refresh=True forces a new download.
        """
        url = self.url(path_or_url)
        file = self._cache_file(url, data)
        if cache and self.use_cache and not refresh and file.exists():
            age_ok = max_age_hours is None or (time.time() - file.stat().st_mtime) < max_age_hours * 3600
            if age_ok:
                self.cache_hits += 1
                return gzip.decompress(file.read_bytes()).decode("utf-8", "replace")
        text = self._download(url, data)
        if cache and self.use_cache:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(gzip.compress(text.encode("utf-8")))
        return text

    def cached(self, path_or_url, data=None):
        """Return a stored copy of the page, or None. Never touches the network."""
        file = self._cache_file(self.url(path_or_url), data)
        if not file.exists():
            return None
        return gzip.decompress(file.read_bytes()).decode("utf-8", "replace")

    def stats(self):
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "robots_refusals": self.robots_refusals,
            "delay_seconds": self.delay,
            "user_agent": self.user_agent,
        }

    # ----------------------------------------------------------------- private

    def _cache_file(self, url, data):
        raw = url if not data else url + "|" + json.dumps(data, sort_keys=True)
        key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.html.gz"

    def _check_budget(self):
        if self.max_requests and self.requests >= self.max_requests:
            raise BudgetReached(f"request allowance of {self.max_requests} used")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise BudgetReached("time allowance used")

    def _pause(self):
        wait = self._last_done + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _send(self, url, data=None):
        self._pause()
        self.requests += 1
        try:
            if data:
                return self.session.post(url, data=data, timeout=self.timeout)
            return self.session.get(url, timeout=self.timeout)
        finally:
            self._last_done = time.monotonic()

    def _load_robots(self):
        if self._robots is not None:
            return
        parser = urllib.robotparser.RobotFileParser()
        try:
            resp = self._send(self.base + "/robots.txt")
        except requests.RequestException as exc:
            raise StopRun(f"Could not reach the site to read robots.txt ({exc.__class__.__name__}).")
        code = resp.status_code
        if code == 200 and "user-agent" not in resp.text.lower() and site_notice(resp.text):
            raise SiteTrouble(f'The site sent a notice instead of robots.txt: "{site_notice(resp.text)}". '
                              "Nothing was fetched. Try again later.")
        if code == 200:
            self.robots_text = resp.text
            parser.parse(resp.text.splitlines())
        elif code in (401, 403, 451):
            raise StopRun(f"The site refused the request for robots.txt (HTTP {code}). Stopping.")
        elif 400 <= code < 500:
            self.robots_text = ""
            parser.allow_all = True          # no robots.txt: no stated restrictions
        else:
            raise StopRun(f"Could not read robots.txt (HTTP {code}). Try again later.")
        self._robots = parser
        wanted = parser.crawl_delay(self.agent_token)
        rate = parser.request_rate(self.agent_token)
        if rate and rate.requests:
            wanted = max(float(wanted or 0), rate.seconds / rate.requests)
        if wanted and float(wanted) > self.delay:
            self.delay = float(wanted)
            self.log(f"robots.txt asks for {self.delay:g}s between requests; using that.")

    def _download(self, url, data):
        self._check_budget()
        self._load_robots()
        if not self._robots.can_fetch(self.agent_token, url):
            self.robots_refusals += 1
            raise StopRun(f"robots.txt does not allow {urllib.parse.urlsplit(url).path}. "
                          "The collector does not fetch pages the site rules out, so it stops here.")
        last_error = "no response"
        slowed = False
        notice = ""
        for attempt in range(self.max_retries + 1):
            self._check_budget()
            try:
                resp = self._send(url, data)
            except requests.RequestException as exc:
                last_error, slowed, notice = f"network error ({exc.__class__.__name__})", False, ""
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            code = resp.status_code
            if code == 200:
                text = self._decode(resp)
                notice = site_notice(text)
                if not notice:
                    self.consecutive_failures = 0
                    return text
                # The site's own trouble notice: wait and ask again a few times, then stop.
                last_error, slowed = "site notice", False
                if attempt < self.max_retries:
                    wait = min(self.max_wait, 60.0 * 2 ** attempt)
                    self.log(f'The site sent a notice instead of the page ("{notice[:90]}"); '
                             f"waiting {wait:.0f}s.")
                    time.sleep(wait)
                continue
            notice = ""
            if code in (401, 403, 451):
                raise StopRun(f"The site refused the request (HTTP {code}) for {url}. "
                              "Stopping; a refusal is not retried.")
            if code in (404, 410):
                self._fail(f"HTTP {code} (page not found)")
            if code in (429, 503):
                wait = self._retry_after(resp, attempt)
                if wait > self.max_wait:
                    raise StopRun(f"The site asked us to wait {wait:.0f}s (HTTP {code}). Stopping.")
                self.log(f"Site asked us to slow down (HTTP {code}); waiting {wait:.0f}s.")
                time.sleep(wait)
                last_error, slowed = f"HTTP {code}", True
                continue
            last_error, slowed = f"HTTP {code}", False
            time.sleep(min(60, 5 * 2 ** attempt))
        if notice:
            raise SiteTrouble(f'The site is sending a notice instead of pages: "{notice}". '
                              "Nothing was recorded from it. Stopping; try again later.")
        if slowed:
            raise StopRun(f"The site is still asking us to slow down ({last_error}). Stopping.")
        self._fail(last_error)

    def _fail(self, reason):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_limit:
            raise StopRun(f"{self.consecutive_failures} requests in a row failed (last: {reason}). Stopping.")
        raise PageError(reason)

    @staticmethod
    def _retry_after(resp, attempt):
        header = (resp.headers.get("Retry-After") or "").strip()
        if header.isdigit():
            return max(1.0, float(header))
        return 30.0 * 2 ** attempt

    @staticmethod
    def _decode(resp):
        if "charset=" in resp.headers.get("Content-Type", "").lower():
            return resp.text
        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError:
            return resp.content.decode(resp.apparent_encoding or "latin-1", "replace")
