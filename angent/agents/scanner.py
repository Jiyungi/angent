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

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

import requests

from angent.models import Candidate, TickPlan

if TYPE_CHECKING:  # avoid a hard import cycle; only needed for type hints
    from angent.persistence.clickhouse import ClickHouseClient

logger = logging.getLogger("angent.agents.scanner")


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


# --- GitHub (Airbyte Agents API) source ------------------------------------

# Confirmed two-step Airbyte OAuth client-credentials flow (folded in from the
# now-removed probe scripts ``test_airbyte.py`` / ``check_connectors.py`` — see
# the design doc's Research Notes):
#
#   1. POST https://api.airbyte.com/v1/applications/token
#        json={client_id, client_secret, grant_type="client_credentials"}
#        -> JSON body carries the bearer token under ``access_token``.
#   2. Call the Agents API at
#        https://api.airbyte.ai/api/v1/integrations/connectors
#        with ``Authorization: Bearer <token>`` PLUS ``X-Organization-Id``.
#
# The connectors endpoint confirmed-returns the *catalog of available connector
# definitions* (GitHub is present as ``connector_name="github"``); the exact
# shape of an actual GitHub repo/stargazer/commit *records* response from the
# Agents API is uncertain for this tier. We therefore treat GitHub discovery as
# a best-effort integration: do the token exchange, call the connectors /
# discovery endpoint, defensively parse any repo-like records out of whatever
# JSON comes back, and degrade gracefully (return an empty list) when no usable
# repo data is available or credentials/tier are missing. The Scanner (task 5.4)
# tolerates a per-source empty/failed result, so an unavailable GitHub tier
# never crashes a Tick.

# Step 1 — OAuth client-credentials token endpoint (api.airbyte.com).
_AIRBYTE_TOKEN_URL = "https://api.airbyte.com/v1/applications/token"

# Step 2 — Agents API connectors / discovery endpoint (api.airbyte.ai).
_AIRBYTE_CONNECTORS_URL = "https://api.airbyte.ai/api/v1/integrations/connectors"

# Per-source request budget (Requirement 4.1): each request completes within 30s.
_GH_TIMEOUT_SECONDS = 30

# Hard 90-day recency window (Requirement 4.1).
_GH_WINDOW_DAYS = 90

# Keys we probe, in order, when extracting repo-like record arrays from an
# uncertain Agents-API response shape.
_GH_RECORD_CONTAINER_KEYS = (
    "repositories",
    "repos",
    "records",
    "results",
    "items",
    "data",
)

# Per-record field aliases for defensive parsing across possible shapes.
_GH_NAME_KEYS = ("full_name", "name", "repo", "repository", "title")
_GH_URL_KEYS = ("html_url", "url", "clone_url", "git_url", "link")
_GH_ID_KEYS = ("id", "node_id", "full_name", "name")
_GH_STARS_KEYS = ("stargazers_count", "stars", "stargazers", "star_count", "watchers_count")
_GH_COMMITS_KEYS = ("commits", "commit_count", "total_commits", "commits_count")
_GH_FORKS_KEYS = ("forks_count", "forks", "fork_count")
_GH_ACTIVITY_KEYS = ("pushed_at", "updated_at", "created_at", "last_activity_at")


class GitHubSource:
    """GitHub signal source via the Airbyte Agents API (Requirement 4.1).

    Implements :class:`SignalSource`. Uses the confirmed two-step OAuth
    client-credentials flow to obtain a bearer token, then calls the Agents API
    connectors / discovery endpoint with ``Authorization: Bearer`` plus
    ``X-Organization-Id``. Any repo-like records returned are mapped to a
    :class:`~angent.models.Candidate`:

    * ``source`` = ``"github"``
    * ``source_unique_id`` = the repo ``full_name`` (or numeric ``id`` fallback)
    * ``name`` = the repo name
    * ``url`` = the repo ``html_url``
    * ``signals`` = ``{"stars": ..., "commits": ..., "forks": ..., ...}``
    * ``first_activity`` = the repo's most recent activity timestamp, required to
      fall within the last 90 days.

    Best-effort + graceful degradation: missing Airbyte credentials, a failed
    token exchange, a non-2xx/invalid connectors response, or a response with no
    usable repo records all yield an empty list rather than raising. The HTTP
    session and config are injectable for testing.
    """

    name = "github"

    def __init__(
        self,
        config: Optional["object"] = None,
        session: Optional[requests.Session] = None,
        timeout: int = _GH_TIMEOUT_SECONDS,
    ) -> None:
        # Lazy import keeps this module importable even if config wiring changes.
        if config is None:
            from angent.config import load_config

            config = load_config()
        self._config = config
        self._airbyte = getattr(config, "airbyte", None)
        self._session = session or requests.Session()
        self._timeout = timeout

    # -- public SignalSource interface --------------------------------------

    def fetch(self, plan: TickPlan, since: datetime) -> list[Candidate]:
        """Fetch GitHub repo candidates with activity in the last 90 days.

        Returns an empty list (never raises) when Airbyte credentials are
        absent, the token exchange fails, the Agents API is unreachable, or the
        response carries no usable repo records — so the Scanner can continue
        with the remaining sources and still complete the Tick.
        """
        if self._airbyte is None or not getattr(self._airbyte, "is_configured", False):
            # No Airbyte client_id/secret configured: degrade gracefully.
            return []

        token = self._get_token()
        if not token:
            return []

        records = self._discover_repos(token)
        if not records:
            return []

        floor = self._window_floor(since)
        candidates: dict[str, Candidate] = {}
        for record in records:
            candidate = self._to_candidate(record, floor=floor)
            if candidate is not None:
                candidates.setdefault(candidate.source_unique_id, candidate)
        return list(candidates.values())

    # -- step 1: OAuth client-credentials token -----------------------------

    def _get_token(self) -> Optional[str]:
        """Exchange client_id/client_secret for a bearer ``access_token``.

        POSTs to the Airbyte token endpoint with ``grant_type=client_credentials``
        exactly as the confirmed probe scripts did. Any timeout/transport/parse
        error or a missing token returns ``None`` so callers degrade gracefully.
        """
        try:
            response = self._session.post(
                _AIRBYTE_TOKEN_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "client_id": self._airbyte.client_id,
                    "client_secret": self._airbyte.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None

        if not isinstance(payload, dict):
            return None
        token = payload.get("access_token")
        return token if isinstance(token, str) and token else None

    # -- step 2: Agents API connectors / discovery --------------------------

    def _discover_repos(self, token: str) -> list[dict]:
        """Call the Agents API connectors/discovery endpoint and extract repos.

        Sends ``Authorization: Bearer`` plus the required ``X-Organization-Id``
        header (the confirmed Agents-API contract). Because the exact records
        shape is uncertain for this tier, the JSON is handed to a defensive
        extractor that pulls repo-like dicts out of whatever container key is
        present. Any failure returns an empty list.
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        org_id = getattr(self._airbyte, "organization_id", None)
        if org_id:
            headers["X-Organization-Id"] = org_id

        try:
            response = self._session.get(
                _AIRBYTE_CONNECTORS_URL,
                params={"workspace_name": "default"},
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return []

        return self._extract_repo_records(payload)

    @staticmethod
    def _extract_repo_records(payload: object) -> list[dict]:
        """Defensively pull repo-like records out of an uncertain response shape.

        Walks the known container keys (``repositories``/``records``/``data``/…)
        and keeps only dict entries that look like a repository (carry a
        name-ish field AND either a URL-ish or a stars-ish field). The connector
        *catalog* entries returned by the confirmed connectors endpoint lack
        these repo signals, so they are filtered out and discovery degrades to an
        empty list rather than misclassifying a connector definition as a repo.
        """
        if not isinstance(payload, dict):
            return []

        for key in _GH_RECORD_CONTAINER_KEYS:
            container = payload.get(key)
            if isinstance(container, list):
                repos = [
                    item
                    for item in container
                    if isinstance(item, dict) and GitHubSource._looks_like_repo(item)
                ]
                if repos:
                    return repos
        return []

    @staticmethod
    def _looks_like_repo(record: dict) -> bool:
        """Heuristic: a record is a repo if it has a name AND a URL or star count."""
        has_name = any(record.get(k) for k in _GH_NAME_KEYS)
        has_url = any(record.get(k) for k in _GH_URL_KEYS)
        has_stars = any(k in record for k in _GH_STARS_KEYS)
        return bool(has_name and (has_url or has_stars))

    # -- mapping + window helpers -------------------------------------------

    def _window_floor(self, since: datetime) -> datetime:
        """Return the effective lower time bound: max(since, now - 90 days)."""
        now = datetime.now(timezone.utc)
        ninety_days_ago = now - timedelta(days=_GH_WINDOW_DAYS)
        bound = since
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=timezone.utc)
        else:
            bound = bound.astimezone(timezone.utc)
        return max(bound, ninety_days_ago)

    def _to_candidate(self, record: dict, *, floor: datetime) -> Optional[Candidate]:
        """Map one repo-like record to a Candidate, or None to skip it.

        Skips records without a name, and records whose most recent activity
        falls before the 90-day floor (Requirement 4.1). Stars/commits/forks are
        coerced to non-negative ints for the ``signals`` payload.
        """
        if not isinstance(record, dict):
            return None

        name = self._first_str(record, _GH_NAME_KEYS)
        if not name:
            return None

        first_activity = self._parse_activity(record)
        if first_activity is not None and first_activity < floor:
            return None

        unique_id = self._first_str(record, _GH_ID_KEYS) or name
        url = self._first_str(record, _GH_URL_KEYS) or f"https://github.com/{name}"

        return Candidate(
            source=GitHubSource.name,
            source_unique_id=str(unique_id),
            name=str(name),
            url=url,
            signals={
                "stars": self._first_int(record, _GH_STARS_KEYS),
                "commits": self._first_int(record, _GH_COMMITS_KEYS),
                "forks": self._first_int(record, _GH_FORKS_KEYS),
                "language": record.get("language"),
                "description": record.get("description"),
            },
            first_activity=first_activity,
        )

    @staticmethod
    def _first_str(record: dict, keys: tuple[str, ...]) -> Optional[str]:
        """Return the first non-empty string value among ``keys``."""
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(value)
        return None

    @staticmethod
    def _first_int(record: dict, keys: tuple[str, ...]) -> int:
        """Return the first coercible non-negative int among ``keys`` (0 default)."""
        for key in keys:
            value = record.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return max(0, int(value))
            if isinstance(value, str) and value.strip().isdigit():
                return max(0, int(value.strip()))
        return 0

    @staticmethod
    def _parse_activity(record: dict) -> Optional[datetime]:
        """Extract the repo's most recent activity as a tz-aware UTC datetime.

        Probes ``pushed_at``/``updated_at``/``created_at`` (ISO8601, possibly
        ``Z``-suffixed) and unix-second numbers, returning ``None`` when none is
        parseable so the record is kept (the floor check then no-ops defensively).
        """
        for key in _GH_ACTIVITY_KEYS:
            value = record.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                try:
                    return datetime.fromtimestamp(int(value), tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    continue
            if isinstance(value, str) and value.strip():
                try:
                    normalized = value.strip().replace("Z", "+00:00")
                    parsed = datetime.fromisoformat(normalized)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(timezone.utc)
                except ValueError:
                    continue
        return None


# --- Hugging Face Hub source (stretch, feature-flagged) --------------------

# Public Hugging Face Hub models endpoint. No auth required; an optional bearer
# token (HUGGINGFACE_TOKEN) raises rate limits but is never mandatory. We sort
# by creation time descending so the most recently created AI models surface
# first and the 90-day window bounds the work (Requirement 4.6).
_HF_MODELS_URL = "https://huggingface.co/api/models"

# Per-source request budget (Requirement 4.6): the request completes within 30s.
_HF_TIMEOUT_SECONDS = 30

# Hard 90-day recency window (Requirement 4.6).
_HF_WINDOW_DAYS = 90

# How many models to request. Kept modest so a Tick stays fast; the Scanner
# orchestrator (5.4) handles dedup/upsert across Ticks.
_HF_LIMIT = 100


class HuggingFaceSource:
    """Hugging Face Hub signal source for newly-created AI models (Requirement 4.6).

    Implements :class:`SignalSource`. This is a **stretch source gated behind a
    feature flag**: it is disabled by default and returns an empty list unless
    explicitly enabled (``enabled=True``), so a deployment opts in deliberately
    and the Scanner only runs it when a Tick plan turns it on (task 5.4).

    When enabled, each :meth:`fetch` issues one bounded request for the most
    recently created models on the Hub (sorted by ``createdAt`` descending) and
    maps every model created within the last 90 days (and at or after ``since``)
    to a :class:`~angent.models.Candidate`:

    * ``source`` = ``"huggingface"``
    * ``source_unique_id`` = the model id (stable Hub natural key)
    * ``name`` = the model id / name
    * ``url`` = ``https://huggingface.co/{id}``
    * ``signals`` = ``{"downloads": ..., "likes": ..., "pipeline_tag": ..., ...}``
    * ``first_activity`` = the model's ``createdAt`` time

    Graceful degradation: when disabled, or on any timeout/transport/parse error
    or a non-list response, :meth:`fetch` returns an empty list rather than
    raising, so the Scanner can continue with the remaining sources and still
    complete the Tick. The HTTP session and config are injectable for testing.
    """

    name = "huggingface"

    def __init__(
        self,
        config: Optional["object"] = None,
        session: Optional[requests.Session] = None,
        timeout: int = _HF_TIMEOUT_SECONDS,
        limit: int = _HF_LIMIT,
        *,
        enabled: bool = False,
    ) -> None:
        # Lazy import keeps this module importable even if config wiring changes.
        if config is None:
            from angent.config import load_config

            config = load_config()
        self._config = config
        self._token = getattr(config, "huggingface_token", None)
        self._session = session or requests.Session()
        self._timeout = timeout
        self._limit = limit
        # The feature flag: this stretch source stays off unless explicitly
        # enabled, so it never runs by accident in the default configuration.
        self._enabled = enabled

    # -- public SignalSource interface --------------------------------------

    def fetch(self, plan: TickPlan, since: datetime) -> list[Candidate]:
        """Fetch newly-created Hugging Face model candidates in the 90-day window.

        Returns an empty list (never raises) when the source is disabled by its
        feature flag, when the Hub API is unreachable or times out, or when the
        response carries no usable model records — so the Scanner can continue
        with the remaining sources and still complete the Tick.
        """
        if not self._enabled:
            # Feature flag off: this stretch source contributes nothing.
            return []

        records = self._fetch_models()
        if not records:
            return []

        floor = self._window_floor(since)
        candidates: dict[str, Candidate] = {}
        for record in records:
            candidate = self._to_candidate(record, floor=floor)
            if candidate is not None:
                candidates.setdefault(candidate.source_unique_id, candidate)
        return list(candidates.values())

    # -- internals -----------------------------------------------------------

    def _fetch_models(self) -> list[dict]:
        """Issue one bounded Hub request for the newest models and return records.

        Sorts by ``createdAt`` descending so the freshest models lead, requests
        the ``createdAt``/``downloads``/``likes`` fields explicitly, and attaches
        the optional bearer token when present. A timeout, transport error,
        non-2xx status, or invalid JSON returns an empty list instead of raising.
        """
        headers = {"Accept": "application/json"}
        if isinstance(self._token, str) and self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        params = {
            "sort": "createdAt",
            "direction": -1,
            "limit": self._limit,
            "full": "true",
        }

        try:
            response = self._session.get(
                _HF_MODELS_URL,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return []

        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def _window_floor(self, since: datetime) -> datetime:
        """Return the effective lower time bound: max(since, now - 90 days)."""
        now = datetime.now(timezone.utc)
        ninety_days_ago = now - timedelta(days=_HF_WINDOW_DAYS)
        bound = since
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=timezone.utc)
        else:
            bound = bound.astimezone(timezone.utc)
        return max(bound, ninety_days_ago)

    def _to_candidate(self, record: dict, *, floor: datetime) -> Optional[Candidate]:
        """Map one Hub model record to a Candidate, or None to skip it.

        Skips records without a model id, and records created before the 90-day
        floor (Requirement 4.6). Downloads/likes are coerced to non-negative ints
        for the ``signals`` payload.
        """
        if not isinstance(record, dict):
            return None

        model_id = record.get("id") or record.get("modelId")
        if not model_id or not isinstance(model_id, str):
            return None

        first_activity = self._parse_created_at(record)
        if first_activity is not None and first_activity < floor:
            return None

        return Candidate(
            source=HuggingFaceSource.name,
            source_unique_id=model_id,
            name=model_id,
            url=f"https://huggingface.co/{model_id}",
            signals={
                "downloads": self._as_int(record.get("downloads")),
                "likes": self._as_int(record.get("likes")),
                "pipeline_tag": record.get("pipeline_tag"),
                "library_name": record.get("library_name"),
                "author": record.get("author"),
            },
            first_activity=first_activity,
        )

    @staticmethod
    def _parse_created_at(record: dict) -> Optional[datetime]:
        """Extract the model's ``createdAt`` as a tz-aware UTC datetime.

        Hugging Face returns ISO8601 strings such as
        ``"2024-01-02T03:04:05.000Z"``. Returns ``None`` when missing or
        unparseable so the record is kept (the floor check then no-ops).
        """
        created_at = record.get("createdAt") or record.get("created_at")
        if isinstance(created_at, str) and created_at:
            try:
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
        """Coerce a Hub numeric field to a non-negative int (0 on failure)."""
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str) and value.strip().isdigit():
            return max(0, int(value.strip()))
        return 0


# --- Scanner orchestrator (dedup/upsert + per-source retry) ----------------

# Hard 90-day recency window the Scanner derives for every Tick (Requirement 4).
_SCAN_WINDOW_DAYS = 90

# Per-source retry budget (Requirement 4.5): retry a failing source up to 3
# times before recording a failure entry and moving on.
_SOURCE_MAX_ATTEMPTS = 3

# fit_score sentinel for an unscored candidate (the Qualifier fills this in
# later). Matches the design's "Int32, default -1 = unscored".
_UNSCORED = -1

# Column order for the ``companies`` table, reused for every insert so writes
# stay in lock-step with the schema in persistence/clickhouse.py.
_COMPANIES_COLUMNS: tuple[str, ...] = (
    "company_id",
    "source",
    "source_unique_id",
    "name",
    "url",
    "signals",
    "first_activity",
    "fit_score",
    "fit_explanation",
    "created_at",
    "updated_at",
    "version",
)


@dataclass
class SourceFailure:
    """A signal source that exhausted its retries during a scan (Requirement 4.5).

    Recorded so the Tick can complete with the remaining sources while the
    failure remains observable in the :class:`ScanResult` and the logs.
    """

    source: str
    error: str
    attempts: int


@dataclass
class ScanResult:
    """Summary of one :meth:`Scanner.scan` run.

    ``candidates`` is every candidate the enabled sources returned this Tick.
    ``inserted`` counts brand-new ``companies`` rows, ``upserted`` counts matches
    on ``(source, source_unique_id)`` rewritten with a bumped version, and
    ``failures`` lists the sources that failed after 3 attempts. ``persist_errors``
    counts candidates whose ClickHouse write exhausted its own bounded retry.
    """

    candidates: list[Candidate] = field(default_factory=list)
    inserted: int = 0
    upserted: int = 0
    failures: list[SourceFailure] = field(default_factory=list)
    persist_errors: int = 0


def _to_utc_aware(dt: Optional[datetime]) -> datetime:
    """Normalize a datetime to timezone-aware UTC for ClickHouse ``DateTime`` writes.

    Using tz-aware UTC values makes the driver's conversion deterministic so a
    value round-trips stably (a naive write is interpreted in the local/session
    timezone, which silently shifts ``created_at`` across an upsert). A naive
    input is treated as already being UTC (that is how the driver returns the
    tz-less ``DateTime`` column on read), and ``None`` defaults to now
    (``first_activity`` is non-nullable in the schema).
    """
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Scanner:
    """Runs the enabled signal sources and writes candidates to ``companies``.

    The Scanner is the Tick's discovery stage (Requirement 4). It:

    * runs each enabled :class:`SignalSource` (respecting ``plan.sources`` when
      the Planner narrows the set), retrying a failing source up to 3 times and,
      on total failure, recording a :class:`SourceFailure`, continuing with the
      remaining sources, and still completing the Tick (Requirement 4.5);
    * inserts candidates not already present in ``companies`` with source
      attribution and a fresh ``company_id`` (Requirement 4.3); and
    * upserts matches on ``(source, source_unique_id)`` by preserving the
      original ``created_at``/``company_id`` and bumping ``version``/``updated_at``
      so the ``ReplacingMergeTree`` resolves to the latest row (Requirement 4.4).

    The sources can be injected; otherwise sensible defaults are built from the
    :class:`~angent.config.Config`: Hacker News always, GitHub when Airbyte
    credentials are present, and Hugging Face only when explicitly enabled.
    """

    def __init__(
        self,
        client: "ClickHouseClient",
        sources: Optional[list[SignalSource]] = None,
        *,
        config: Optional["object"] = None,
        enable_huggingface: bool = False,
    ) -> None:
        self._client = client
        if sources is None:
            sources = self._default_sources(config, enable_huggingface=enable_huggingface)
        self._sources: list[SignalSource] = list(sources)

    # -- defaults ------------------------------------------------------------

    @staticmethod
    def _default_sources(
        config: Optional["object"], *, enable_huggingface: bool
    ) -> list[SignalSource]:
        """Build the default source set: HN always, GitHub if Airbyte creds, HF if flagged."""
        if config is None:
            from angent.config import load_config

            config = load_config()

        sources: list[SignalSource] = [HackerNewsSource()]

        airbyte = getattr(config, "airbyte", None)
        if airbyte is not None and getattr(airbyte, "is_configured", False):
            sources.append(GitHubSource(config=config))

        if enable_huggingface:
            sources.append(HuggingFaceSource(config=config, enabled=True))

        return sources

    @property
    def sources(self) -> list[SignalSource]:
        return list(self._sources)

    # -- public API ----------------------------------------------------------

    def scan(self, plan: TickPlan) -> ScanResult:
        """Run the enabled sources for ``plan`` and dedup/upsert into ``companies``.

        Steps (Requirement 4.3, 4.4, 4.5):
          1. Derive ``since`` = now - 90 days (the hard recency window).
          2. For each enabled source (filtered by ``plan.sources`` when set),
             call ``fetch`` with up to 3 retry attempts; on total failure record
             a :class:`SourceFailure` and continue.
          3. Insert new candidates / upsert existing ones, preserving the
             original ``created_at`` and ``company_id`` on upsert.

        Returns a :class:`ScanResult` summarizing inserted/upserted counts, the
        per-source failures, and every candidate discovered this Tick.
        """
        since = datetime.now(timezone.utc) - timedelta(days=_SCAN_WINDOW_DAYS)
        result = ScanResult()

        for source in self._enabled_sources(plan):
            candidates = self._fetch_with_retry(source, plan, since, result)
            for candidate in candidates:
                result.candidates.append(candidate)
                self._persist_candidate(candidate, result)

        logger.info(
            "Scan complete: %d candidate(s), %d inserted, %d upserted, "
            "%d source failure(s), %d persist error(s)",
            len(result.candidates),
            result.inserted,
            result.upserted,
            len(result.failures),
            result.persist_errors,
        )
        return result

    # -- source orchestration ------------------------------------------------

    def _enabled_sources(self, plan: TickPlan) -> list[SignalSource]:
        """Return the sources to run this Tick, honoring ``plan.sources`` if set.

        When the Planner provides a non-empty ``plan.sources`` list, only sources
        whose ``name`` appears in it run; otherwise every configured source runs.
        """
        wanted = getattr(plan, "sources", None) or []
        if not wanted:
            return list(self._sources)
        wanted_set = {str(name) for name in wanted}
        return [s for s in self._sources if getattr(s, "name", None) in wanted_set]

    def _fetch_with_retry(
        self,
        source: SignalSource,
        plan: TickPlan,
        since: datetime,
        result: ScanResult,
    ) -> list[Candidate]:
        """Call ``source.fetch`` up to 3 times; record a failure on total failure.

        A source that raises is retried up to :data:`_SOURCE_MAX_ATTEMPTS` times.
        If every attempt raises, a :class:`SourceFailure` is appended to
        ``result.failures`` and an empty list is returned so the scan continues
        with the remaining sources and still completes the Tick (Requirement 4.5).
        """
        name = getattr(source, "name", source.__class__.__name__)
        last_error: Optional[str] = None
        for attempt in range(1, _SOURCE_MAX_ATTEMPTS + 1):
            try:
                return source.fetch(plan, since)
            except Exception as exc:  # noqa: BLE001 - retry any source error
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Signal source %r attempt %d/%d failed: %s",
                    name,
                    attempt,
                    _SOURCE_MAX_ATTEMPTS,
                    last_error,
                )

        result.failures.append(
            SourceFailure(source=name, error=last_error or "unknown error",
                          attempts=_SOURCE_MAX_ATTEMPTS)
        )
        logger.error(
            "Signal source %r failed after %d attempts; continuing with remaining sources",
            name,
            _SOURCE_MAX_ATTEMPTS,
        )
        return []

    # -- dedup / upsert ------------------------------------------------------

    def _persist_candidate(self, candidate: Candidate, result: ScanResult) -> None:
        """Insert a new candidate or upsert an existing one into ``companies``.

        Looks up the current row by ``(source, source_unique_id)``. If absent,
        inserts a new row (fresh ``company_id``, ``version=1``, unscored). If
        present, rewrites the row preserving the original ``company_id`` and
        ``created_at`` while bumping ``version``/``updated_at`` so the
        ``ReplacingMergeTree`` keeps the latest version (Requirement 4.3, 4.4).
        """
        existing = self._find_existing(candidate)
        now = datetime.now(timezone.utc)
        signals_json = json.dumps(candidate.signals or {}, default=str)
        first_activity = _to_utc_aware(candidate.first_activity)

        if existing is None:
            company_id = str(uuid.uuid4())
            row = [
                company_id,
                candidate.source,
                candidate.source_unique_id,
                candidate.name,
                candidate.url,
                signals_json,
                first_activity,
                _UNSCORED,
                "",            # fit_explanation — filled by the Qualifier later
                now,           # created_at
                now,           # updated_at
                1,             # version
            ]
            write = self._client.insert("companies", [row], list(_COMPANIES_COLUMNS))
            if write.ok:
                result.inserted += 1
            else:
                result.persist_errors += 1
            return

        company_id, created_at, fit_score, fit_explanation, version = existing
        row = [
            company_id,                       # preserve original id
            candidate.source,
            candidate.source_unique_id,
            candidate.name,                   # refresh mutable fields
            candidate.url,
            signals_json,
            first_activity,
            int(fit_score) if fit_score is not None else _UNSCORED,
            fit_explanation or "",            # preserve any existing explanation
            _to_utc_aware(created_at),         # preserve original created_at
            now,                              # bump updated_at
            int(version) + 1,                 # bump version -> latest wins
        ]
        write = self._client.insert("companies", [row], list(_COMPANIES_COLUMNS))
        if write.ok:
            result.upserted += 1
        else:
            result.persist_errors += 1

    def _find_existing(
        self, candidate: Candidate
    ) -> Optional[tuple[str, datetime, int, str, int]]:
        """Return the latest existing row for this candidate, or ``None``.

        Selects the highest-version row for ``(source, source_unique_id)`` so the
        upsert can preserve the original ``company_id``/``created_at`` and bump
        from the current ``version``. On a read failure returns ``None`` (the
        candidate is then treated as new — at worst a harmless extra version that
        the ``ReplacingMergeTree`` collapses on the natural key).
        """
        read = self._client.query(
            "SELECT company_id, created_at, fit_score, fit_explanation, version "
            "FROM companies "
            "WHERE source = {source:String} "
            "AND source_unique_id = {suid:String} "
            "ORDER BY version DESC LIMIT 1",
            parameters={
                "source": candidate.source,
                "suid": candidate.source_unique_id,
            },
        )
        if not read.ok or not read.rows:
            return None
        company_id, created_at, fit_score, fit_explanation, version = read.rows[0]
        return company_id, created_at, fit_score, fit_explanation, version
