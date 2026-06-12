"""The Scanner's signal-source contract and the Hacker News Algolia source.

The Scanner discovers candidate startups from public signal sources, each
limited to activity within the most recent 90 days and a 30-second per-source
timeout (Requirement 4). Sources are pluggable behind a single
:class:`SignalSource` protocol so the Scanner orchestrator (built later in task
5.4) can run each enabled source uniformly and never branches on the concrete
source type.

This module defines:

* :class:`SignalSource` — the ``typing.Protocol`` every source satisfies
  (a ``name`` attribute and ``fetch(plan, since) -> list[Candidate]``).
* :class:`HackerNewsSource` — retrieves Show HN / Launch HN posts from the
  public Hacker News Algolia API (no auth) within the last 90 days, mapping each
  hit to a :class:`~angent.models.Candidate` with ``source="hackernews"``, a
  source-specific unique id (the Algolia ``objectID``), a cleaned name, a URL,
  and ``signals`` carrying the post's points and comment count (Requirement 4.2).

Network calls are kept robust: a single bounded request per query with a
30-second timeout, and any timeout/transport/parse error is swallowed so a
failing query never aborts the scan (the Scanner's own per-source retry and
failure-entry handling lands in task 5.4).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol, runtime_checkable

import requests

from angent.models import Candidate, TickPlan


# --- Signal-source contract -------------------------------------------------


@runtime_checkable
class SignalSource(Protocol):
    """The single interface every Scanner signal source implements.

    Implementations expose a stable ``name`` used for source attribution on the
    ``companies`` records (``'github' | 'hackernews' | 'huggingface'``) and a
    :meth:`fetch` that returns the candidates discovered for a given Tick plan,
    bounded to activity at or after ``since`` (the 90-day window) and completing
    within the source's 30-second timeout (Requirement 4).
    """

    name: str

    def fetch(self, plan: TickPlan, since: datetime) -> list[Candidate]:
        """Return candidates discovered for ``plan`` with activity >= ``since``."""
        ...


# --- Hacker News (Algolia) source ------------------------------------------

# Public Algolia search endpoint for Hacker News, ordered by recency so the
# 90-day window and the per-query timeout bound the work. No auth required.
_HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"

# Per-source request budget (Requirement 4.2): every source request must
# complete within 30 seconds.
_HN_TIMEOUT_SECONDS = 30

# Hard 90-day recency window (Requirement 4.2). The ``since`` argument may
# narrow this further, but never widen it past 90 days.
_HN_WINDOW_DAYS = 90

# How many hits to request per query. Kept modest so a Tick stays fast; the
# Scanner orchestrator (5.4) handles dedup/upsert across Ticks.
_HN_HITS_PER_PAGE = 50

# The two launch-signal flavors we mine. Show HN posts carry the ``show_hn``
# Algolia tag; Launch HN posts (YC launches) are not tagged, so we match them by
# query text and keep only titles that actually start with the prefix.
_HN_PREFIXES = ("Show HN:", "Launch HN:")


def _strip_prefix(title: str) -> str:
    """Remove a leading 'Show HN:' / 'Launch HN:' prefix from a post title.

    Falls back to the original (trimmed) title when no known prefix is present
    so the candidate always carries a meaningful name.
    """
    cleaned = (title or "").strip()
    for prefix in _HN_PREFIXES:
        if cleaned.lower().startswith(prefix.lower()):
            return cleaned[len(prefix):].strip(" :-\u2013\u2014") or cleaned
    return cleaned


class HackerNewsSource:
    """Hacker News Algolia signal source for Show HN / Launch HN launches.

    Implements :class:`SignalSource`. Each :meth:`fetch` issues bounded queries
    for Show HN and Launch HN posts created within the last 90 days (and at or
    after ``since``), then maps every qualifying hit to a
    :class:`~angent.models.Candidate`:

    * ``source`` = ``"hackernews"``
    * ``source_unique_id`` = the Algolia ``objectID`` (stable HN item id)
    * ``name`` = the post title with the ``Show HN:`` / ``Launch HN:`` prefix
      stripped
    * ``url`` = the submitted story URL when present, else a link to the HN item
    * ``signals`` = ``{"points": ..., "num_comments": ..., "kind": ...}``
    * ``first_activity`` = the post's creation time

    The HTTP session is injectable for testing; production uses a module-level
    :class:`requests.Session`.
    """

    name = "hackernews"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout: int = _HN_TIMEOUT_SECONDS,
        hits_per_page: int = _HN_HITS_PER_PAGE,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout
        self._hits_per_page = hits_per_page

    # -- public SignalSource interface --------------------------------------

    def fetch(self, plan: TickPlan, since: datetime) -> list[Candidate]:
        """Fetch Show HN + Launch HN candidates with activity in the 90-day window.

        ``since`` bounds the lower edge of the window but is clamped so it never
        reaches further back than 90 days. Results are de-duplicated by
        ``objectID`` across the two queries (a post can match both). Any network
        or parse failure for a query yields an empty list for that query rather
        than raising, so a transient HN/Algolia problem never aborts the scan.
        """
        floor = self._window_floor(since)
        floor_unix = int(floor.timestamp())

        candidates: dict[str, Candidate] = {}

        # Show HN: matched by the dedicated Algolia tag.
        for hit in self._search(tags="show_hn", numeric_filter=floor_unix):
            candidate = self._to_candidate(hit, kind="show_hn", floor=floor)
            if candidate is not None:
                candidates[candidate.source_unique_id] = candidate

        # Launch HN: not tagged, so match by query text and verify the prefix.
        for hit in self._search(query="Launch HN", numeric_filter=floor_unix):
            title = hit.get("title") or ""
            if not title.lower().startswith("launch hn:"):
                continue
            candidate = self._to_candidate(hit, kind="launch_hn", floor=floor)
            if candidate is not None:
                candidates.setdefault(candidate.source_unique_id, candidate)

        return list(candidates.values())

    # -- internals -----------------------------------------------------------

    def _window_floor(self, since: datetime) -> datetime:
        """Return the effective lower time bound: max(since, now - 90 days).

        Keeps the window at most 90 days wide regardless of the ``since`` the
        Planner passes, satisfying the "within the most recent 90 days" bound.
        """
        now = datetime.now(timezone.utc)
        ninety_days_ago = now - timedelta(days=_HN_WINDOW_DAYS)
        bound = since
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=timezone.utc)
        else:
            bound = bound.astimezone(timezone.utc)
        return max(bound, ninety_days_ago)

    def _search(
        self,
        *,
        tags: Optional[str] = None,
        query: Optional[str] = None,
        numeric_filter: Optional[int] = None,
    ) -> list[dict]:
        """Issue one bounded Algolia query and return its raw hits.

        Robust by design: a timeout, transport error, non-2xx status, or invalid
        JSON returns an empty list instead of raising, so the caller can degrade
        gracefully (the Scanner's retry/failure-entry handling is task 5.4).
        """
        params: dict[str, object] = {"hitsPerPage": self._hits_per_page}
        if tags:
            params["tags"] = tags
        if query:
            params["query"] = query
        if numeric_filter is not None:
            params["numericFilters"] = f"created_at_i>{numeric_filter}"

        try:
            response = self._session.get(
                _HN_SEARCH_URL, params=params, timeout=self._timeout
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return []

        hits = payload.get("hits") if isinstance(payload, dict) else None
        return hits if isinstance(hits, list) else []

    def _to_candidate(
        self, hit: dict, *, kind: str, floor: datetime
    ) -> Optional[Candidate]:
        """Map one Algolia hit to a Candidate, or None if it should be skipped.

        Skips hits without a stable ``objectID`` or a title, and hits whose
        creation time falls before the 90-day floor (defensive: the Algolia
        numeric filter already enforces this, but client-side parsing guards
        against malformed rows).
        """
        if not isinstance(hit, dict):
            return None

        object_id = hit.get("objectID")
        title = hit.get("title")
        if not object_id or not title:
            return None

        first_activity = self._parse_created_at(hit)
        if first_activity is not None and first_activity < floor:
            return None

        story_url = hit.get("url")
        url = story_url or f"https://news.ycombinator.com/item?id={object_id}"

        points = self._as_int(hit.get("points"))
        num_comments = self._as_int(hit.get("num_comments"))

        return Candidate(
            source=HackerNewsSource.name,
            source_unique_id=str(object_id),
            name=_strip_prefix(title),
            url=url,
            signals={
                "points": points,
                "num_comments": num_comments,
                "kind": kind,
                "author": hit.get("author"),
            },
            first_activity=first_activity,
        )

    @staticmethod
    def _parse_created_at(hit: dict) -> Optional[datetime]:
        """Extract the post's creation time as a tz-aware UTC datetime.

        Prefers the numeric ``created_at_i`` (unix seconds); falls back to the
        ISO ``created_at`` string. Returns ``None`` when neither is parseable.
        """
        created_i = hit.get("created_at_i")
        if isinstance(created_i, (int, float)):
            return datetime.fromtimestamp(int(created_i), tz=timezone.utc)

        created_at = hit.get("created_at")
        if isinstance(created_at, str) and created_at:
            try:
                # Algolia returns e.g. "2024-01-02T03:04:05.000Z".
                normalized = created_at.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                return None
        return None

    @staticmethod
    def _as_int(value: object) -> int:
        """Coerce an Algolia numeric field to a non-negative int (0 on failure)."""
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return max(0, int(value))
        return 0
