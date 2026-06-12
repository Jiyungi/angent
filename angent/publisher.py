"""Publisher: serialize a run's qualified companies into a Deal_Memo (markdown).

The Publisher turns a run's qualified companies into a citable open-web artifact.
It reads the **same** ClickHouse company data the OpenUI deep-dive consumes
(name, URL, source, fit_score, fit_explanation, signals), serializes it into a
**Deal_Memo markdown** document with a provenance citation per company linking
to that company's real source URL (its GitHub repo or Hacker News post), and
(in task 20.2) publishes it to cited.md via Senso.

This module (task 20.1) implements the markdown serializer only:

  * :class:`DealMemo`        — a small holder for the markdown text + metadata.
  * :func:`read_qualified_companies` — reads scored companies from the
    ``companies`` table (latest version via ``argMax``), optionally filtered by
    a minimum fit score, returning :class:`~angent.models.Qualified` objects.
  * :meth:`Publisher.serialize` — accepts a list of ``Qualified`` (or reads
    from ClickHouse) and emits the Deal_Memo markdown: a title/summary plus one
    ``##`` section per company carrying name, URL, source, fit_score,
    fit_explanation, signals, AND a provenance citation line linking to the
    real source URL (Requirements 20.1, 20.2).

The Deal_Memo is **always markdown** and is **never** OpenUI Lang. It is
produced by THIS code path (the Publisher's markdown serializer), which is
entirely separate from the OpenUI deep-dive surface
(``genui-chat-app/src/components/openui/CompanyDeepDive.tsx``, Requirement 14).
Both code paths read the **same** ClickHouse company fields
(``_COMPANY_FIELDS``), but they emit two distinct output types that remain
separate (Requirement 20.6):

  * the Publisher → **markdown** Deal_Memo published to cited.md, and
  * the OpenUI deep-dive → **OpenUI Lang** rendered by ``<Renderer/>`` for the
    in-app human surface.

:func:`assert_markdown_not_openui` makes this separation explicit and
verifiable: it guards a serialized Deal_Memo against any OpenUI Lang markers so
the two output types can never converge (Requirement 20.6, enforced by task
20.3). :meth:`Publisher.publish` is stubbed here and fully implemented (Senso
call + persistence + local fallback) in task 20.2.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Config, load_config
from .models import Qualified

logger = logging.getLogger("angent.publisher")

# How long to wait on the Senso HTTP call before treating it as unreachable.
SENSO_TIMEOUT_SECONDS = 15
# Path appended to ``SENSO_BASE_URL`` for the publish/content-ingest call.
SENSO_PUBLISH_PATH = "/content/raw"
# Directory for the local Deal_Memo fallback (gitignored — see .gitignore).
LOCAL_FALLBACK_DIR = "deal_memos"
# Columns of the ``publications`` table (kept aligned with the ClickHouse DDL).
PUBLICATIONS_COLUMNS: tuple[str, ...] = (
    "publication_id",
    "run_id",
    "cited_md_url",
    "slug",
    "handle",
    "local_path",
    "published_ok",
    "published_at",
    "updated_at",
    "version",
)


# --- Result + memo holders --------------------------------------------------


@dataclass
class PublishResult:
    """Outcome of publishing a Deal_Memo (see design → Publisher).

    On success ``ok`` is True and ``url``/``slug`` carry the cited.md location.
    On failure the local-file fallback sets ``local_path`` and ``error``
    (Requirement 20.5). Fully populated by task 20.2.
    """

    ok: bool
    url: Optional[str] = None        # cited.md/<handle>/<slug> on success
    slug: Optional[str] = None
    local_path: Optional[str] = None  # set when the local-file fallback is used
    error: Optional[str] = None


@dataclass
class DealMemo:
    """The published Deal_Memo artifact: markdown text + metadata.

    ``markdown`` is the full Deal_Memo body destined for cited.md. ``title`` and
    ``summary`` map to Senso's ``seo_title`` / ``summary`` fields, and
    ``company_count`` is carried for convenience/logging.
    """

    markdown: str
    title: str
    summary: str
    company_count: int = 0
    run_id: Optional[str] = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# --- ClickHouse read --------------------------------------------------------

# Columns read from the ``companies`` table for the Deal_Memo. We use ``argMax``
# on ``version`` so the latest upserted row wins on the ReplacingMergeTree
# (mirrors loop_state's latest-version-wins read in the persistence layer).
_COMPANY_FIELDS = ("name", "url", "source", "fit_score", "fit_explanation", "signals")


def read_qualified_companies(
    client: Any,
    run_id: Optional[str] = None,
    *,
    min_score: int = 0,
    limit: int = 200,
) -> list[Qualified]:
    """Read scored companies from the ``companies`` table as ``Qualified``.

    Latest version per ``(source, source_unique_id)`` is resolved with
    ``argMax(col, version)``. Only companies with ``fit_score >= min_score`` are
    returned (default 0 keeps every scored company; ``fit_score = -1`` means
    unscored and is always excluded). Ordered by fit score descending so the
    Deal_Memo leads with the strongest fits.

    ``client`` is a :class:`~angent.persistence.clickhouse.ClickHouseClient`.
    On a read failure an empty list is returned (publishing is non-blocking).
    """
    # ``source`` is part of the natural key (GROUP BY), so it is selected
    # directly; the version-mutable columns use ``argMax(col, version)`` to pick
    # the value from the row with the greatest version (latest upserted state).
    # Output column order is kept aligned with ``_COMPANY_FIELDS`` below.
    statement = (
        "SELECT name, url, source, fit_score, fit_explanation, signals FROM ("
        "SELECT "
        "argMax(name, version) AS name, "
        "argMax(url, version) AS url, "
        "source, "
        "argMax(fit_score, version) AS fit_score, "
        "argMax(fit_explanation, version) AS fit_explanation, "
        "argMax(signals, version) AS signals "
        "FROM companies "
        "GROUP BY source, source_unique_id"
        ") "
        "WHERE fit_score >= {min_score:Int32} "
        "ORDER BY fit_score DESC "
        "LIMIT {limit:Int32}"
    )
    result = client.query(
        statement,
        parameters={"min_score": int(min_score), "limit": int(limit)},
    )
    if not getattr(result, "ok", False) or not result.rows:
        if not getattr(result, "ok", False):
            logger.warning(
                "read_qualified_companies: ClickHouse read failed: %s",
                getattr(result, "error", "unknown error"),
            )
        return []

    companies: list[Qualified] = []
    for row in result.rows:
        name, url, source, fit_score, fit_explanation, signals = row
        companies.append(
            Qualified(
                source=source,
                source_unique_id=url or name,
                name=name,
                url=url,
                signals=_parse_signals(signals),
                fit_score=int(fit_score) if fit_score is not None else 0,
                fit_explanation=fit_explanation or "",
            )
        )
    return companies


def _parse_signals(raw: Any) -> dict:
    """Best-effort parse of the JSON ``signals`` column into a dict."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, ValueError):
        return {"raw": str(raw)}


# --- Output-type separation guard (Requirement 20.6) ------------------------

# OpenUI Lang / OpenUI-surface markers that must NEVER appear in a markdown
# Deal_Memo. These are structural DSL/identifier tokens specific to the
# OpenUI-generated deep-dive surface (Requirement 14,
# ``genui-chat-app/src/components/openui``) — they have no place in the
# Publisher's markdown output. Kept narrow so legitimate company prose (fit
# explanations, signals) never trips the guard.
_OPENUI_LANG_MARKERS: tuple[str, ...] = (
    "<Renderer",        # OpenUI Lang is rendered by the <Renderer/> element
    "openuiLibrary",    # OpenUI Lang prompt/library handle
    "openuiPromptOptions",
    "componentLibrary",
    "streamProtocol",
    "@openuidev",        # OpenUI React package namespace
    "processMessage",
)


def assert_markdown_not_openui(text: str) -> None:
    """Guard that ``text`` is a markdown Deal_Memo and not OpenUI Lang.

    The Deal_Memo is markdown ONLY and is produced by this Publisher code path,
    which is separate from the OpenUI deep-dive surface (Requirement 14). That
    deep-dive renders the **same** ClickHouse company data as the in-app human
    surface, but as OpenUI Lang via ``<Renderer/>`` — a distinct output type.
    This guard affirms the two output types remain separate (Requirement 20.6)
    by ensuring no OpenUI Lang markers leak into the published Deal_Memo.

    Raises :class:`ValueError` if any OpenUI Lang marker is found.
    """
    found = [marker for marker in _OPENUI_LANG_MARKERS if marker in text]
    if found:
        raise ValueError(
            "Deal_Memo must be markdown, not OpenUI Lang (Requirement 20.6); "
            f"found OpenUI Lang marker(s): {', '.join(found)}"
        )


# --- Publisher --------------------------------------------------------------


class Publisher:
    """Serializes qualified companies into a Deal_Memo and (20.2) publishes it.

    ``client`` (a ClickHouseClient) is optional and only needed when
    :meth:`serialize` is asked to read companies from the blackboard rather than
    being handed a list directly.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        run_id: Optional[str] = None,
        config: Optional[Config] = None,
    ) -> None:
        self._client = client
        self._run_id = run_id
        self._config = config

    # -- serialize -----------------------------------------------------------

    def serialize(
        self,
        companies: Optional[list[Qualified]] = None,
        *,
        min_score: int = 0,
        title: Optional[str] = None,
    ) -> DealMemo:
        """Emit the Deal_Memo markdown for ``companies``.

        Accepts either an explicit list of :class:`Qualified` companies or, when
        ``companies`` is ``None``, reads the qualified companies from ClickHouse
        via :func:`read_qualified_companies` (requires a ``client``).

        The returned :class:`DealMemo` holds markdown with a title/summary and
        one ``##`` section per company containing the company's name, URL,
        source, fit_score, fit_explanation and signals, plus a **provenance
        citation** line that links to the company's real source URL — its
        GitHub repo or Hacker News post (Requirements 20.1, 20.2).
        """
        if companies is None:
            if self._client is None:
                raise ValueError(
                    "serialize() needs either a companies list or a ClickHouse "
                    "client to read from"
                )
            companies = read_qualified_companies(
                self._client, self._run_id, min_score=min_score
            )

        generated_at = datetime.now(timezone.utc)
        memo_title = title or "Angent Deal Memo \u2014 Qualified Companies"
        count = len(companies)
        if count == 0:
            summary = "No qualified companies were discovered for this run."
        else:
            avg = sum(c.fit_score for c in companies) / count
            summary = (
                f"{count} qualified compan{'y' if count == 1 else 'ies'} "
                f"discovered from public signals (GitHub, Hacker News), "
                f"average fit score {avg:.0f}/100. Each entry below cites its "
                f"real source for provenance."
            )

        lines: list[str] = []
        lines.append(f"# {memo_title}")
        lines.append("")
        lines.append(
            f"_Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')}"
            + (f" for run `{self._run_id}`" if self._run_id else "")
            + "._"
        )
        lines.append("")
        lines.append(summary)
        lines.append("")

        if count == 0:
            lines.append(
                "_No companies met the qualification threshold for this run._"
            )
        else:
            for idx, company in enumerate(companies, start=1):
                lines.extend(self._render_company(idx, company))
                lines.append("")

        markdown = "\n".join(lines).rstrip() + "\n"
        return DealMemo(
            markdown=markdown,
            title=memo_title,
            summary=summary,
            company_count=count,
            run_id=self._run_id,
            generated_at=generated_at,
        )

    def _render_company(self, idx: int, company: Qualified) -> list[str]:
        """Render one ``##`` section for ``company`` including its citation."""
        name = company.name or "(unnamed company)"
        section: list[str] = [f"## {idx}. {name}"]

        # Core fields.
        if company.url:
            section.append(f"- **URL:** [{company.url}]({company.url})")
        section.append(f"- **Source:** {self._source_label(company.source)}")
        section.append(f"- **Fit score:** {company.fit_score}/100")

        explanation = (company.fit_explanation or "").strip()
        if explanation:
            section.append(f"- **Fit explanation:** {explanation}")

        signals_md = self._render_signals(company.signals)
        if signals_md:
            section.append(f"- **Signals:** {signals_md}")

        # Provenance citation linking to the real source URL (GitHub repo / HN post).
        citation = self._provenance_citation(company)
        section.append("")
        section.append(f"> {citation}")
        return section

    @staticmethod
    def _source_label(source: str) -> str:
        return {
            "github": "GitHub",
            "hackernews": "Hacker News",
            "huggingface": "Hugging Face Hub",
        }.get(source, source or "unknown")

    @staticmethod
    def _render_signals(signals: dict) -> str:
        """Render the signals dict as a compact inline list, or '' if empty."""
        if not signals:
            return ""
        parts = [f"{k}: {v}" for k, v in signals.items()]
        return ", ".join(parts)

    def _provenance_citation(self, company: Qualified) -> str:
        """Build the provenance citation line for ``company``.

        Links to the company's real source URL — its GitHub repository or the
        Hacker News post it was discovered from — so the Deal_Memo is citable
        with provenance (Requirement 20.2).
        """
        label = self._source_label(company.source)
        if company.url:
            kind = {
                "github": "GitHub repository",
                "hackernews": "Hacker News post",
                "huggingface": "Hugging Face page",
            }.get(company.source, f"{label} source")
            return (
                f"Provenance: discovered via {label}. "
                f"Source: [{kind}]({company.url})"
            )
        return f"Provenance: discovered via {label}. Source URL unavailable."

    # -- publish -------------------------------------------------------------

    def publish(
        self,
        deal_memo: DealMemo,
        *,
        geo_question_id: Optional[str] = None,
    ) -> PublishResult:
        """Publish ``deal_memo`` to cited.md via Senso, with a local fallback.

        Posts the Deal_Memo markdown to Senso (``SENSO_BASE_URL`` with the
        ``X-API-Key`` header, payload ``{geo_question_id?, raw_markdown,
        seo_title, summary}``). On success the returned cited.md URL / slug /
        handle are persisted to the ``publications`` table with
        ``published_ok=1`` and a :class:`PublishResult` ``ok=True`` is returned
        (Requirements 20.3, 20.4).

        If Senso is unconfigured, unreachable, times out, or returns an error,
        the Deal_Memo markdown is written to a local file (``deal_memos/<run_id>.md``,
        gitignored), a ``publications`` row is persisted with ``published_ok=0``
        and the ``local_path`` set, the error is surfaced on the result, and the
        method **never raises** so the Control_Loop is never aborted
        (Requirements 20.5, 18.11, 18.13).
        """
        config = self._config or load_config()
        senso = config.senso

        if not senso.is_configured:
            error = "Senso is not configured (SENSO_API_KEY missing)"
            logger.warning("Publisher.publish: %s; using local fallback", error)
            return self._local_fallback(deal_memo, error)

        try:
            url, slug, handle = self._post_to_senso(
                deal_memo, senso.api_key, senso.base_url, geo_question_id
            )
        except Exception as exc:  # noqa: BLE001 - any failure -> non-blocking fallback
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Publisher.publish: Senso publish failed (%s); using local fallback",
                error,
            )
            return self._local_fallback(deal_memo, error)

        # Success: persist the cited.md location with published_ok=1.
        self._persist_publication(
            cited_md_url=url or "",
            slug=slug or "",
            handle=handle or "",
            local_path=None,
            published_ok=1,
        )
        logger.info("Publisher.publish: published Deal_Memo to %s", url)
        return PublishResult(ok=True, url=url, slug=slug)

    # -- Senso HTTP call -----------------------------------------------------

    def _post_to_senso(
        self,
        deal_memo: DealMemo,
        api_key: str,
        base_url: str,
        geo_question_id: Optional[str],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """POST the Deal_Memo to Senso and return ``(url, slug, handle)``.

        Raises on any transport error or non-2xx response so the caller can fall
        back to the local file path.
        """
        import requests  # imported lazily so the module imports without requests

        endpoint = base_url.rstrip("/") + SENSO_PUBLISH_PATH
        payload: dict[str, Any] = {
            "raw_markdown": deal_memo.markdown,
            "seo_title": deal_memo.title,
            "summary": deal_memo.summary,
            # Senso's content-ingest API also accepts these field names; sending
            # both keeps us robust to the documented schema (title/summary/text).
            "title": deal_memo.title,
            "text": deal_memo.markdown,
        }
        if geo_question_id:
            payload["geo_question_id"] = geo_question_id

        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        response = requests.post(
            endpoint, json=payload, headers=headers, timeout=SENSO_TIMEOUT_SECONDS
        )
        response.raise_for_status()

        try:
            body = response.json()
        except ValueError:
            body = {}
        return self._parse_senso_response(body)

    @staticmethod
    def _parse_senso_response(
        body: Any,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Extract ``(url, slug, handle)`` from a Senso response body.

        Tolerant of nesting and alternate key names: a cited.md entry carries a
        ``handle`` and ``slug`` and is served at ``cited.md/<handle>/<slug>``.
        """
        if not isinstance(body, dict):
            return None, None, None

        # Unwrap a common single-level envelope (e.g. {"data": {...}}).
        data = body
        for key in ("data", "result", "content", "publication"):
            inner = body.get(key)
            if isinstance(inner, dict):
                data = inner
                break

        def pick(d: dict, *names: str) -> Optional[str]:
            for n in names:
                v = d.get(n)
                if isinstance(v, str) and v:
                    return v
            return None

        url = pick(data, "url", "cited_md_url", "public_url", "permalink")
        slug = pick(data, "slug")
        handle = pick(data, "handle", "org_handle", "namespace")

        if not url and handle and slug:
            url = f"https://cited.md/{handle}/{slug}"
        elif not url and slug:
            url = f"https://cited.md/{slug}"
        return url, slug, handle

    # -- local fallback ------------------------------------------------------

    def _local_fallback(self, deal_memo: DealMemo, error: str) -> PublishResult:
        """Write the Deal_Memo to a local file and record a failed publication.

        Never raises: a fallback write failure is logged and still yields a
        non-OK :class:`PublishResult` so the Control_Loop continues.
        """
        local_path: Optional[str] = None
        try:
            os.makedirs(LOCAL_FALLBACK_DIR, exist_ok=True)
            run_id = self._run_id or deal_memo.run_id or datetime.now(
                timezone.utc
            ).strftime("%Y%m%dT%H%M%SZ")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(run_id))
            local_path = os.path.join(LOCAL_FALLBACK_DIR, f"{safe}.md")
            with open(local_path, "w", encoding="utf-8") as fh:
                fh.write(deal_memo.markdown)
            local_path = os.path.abspath(local_path)
            logger.info("Publisher: wrote local Deal_Memo fallback to %s", local_path)
        except Exception as write_exc:  # noqa: BLE001 - fallback must never raise
            logger.error(
                "Publisher: local fallback write failed: %s", write_exc, exc_info=True
            )
            error = f"{error}; local write failed: {write_exc}"

        self._persist_publication(
            cited_md_url="",
            slug="",
            handle="",
            local_path=local_path,
            published_ok=0,
        )
        return PublishResult(ok=False, local_path=local_path, error=error)

    # -- persistence ---------------------------------------------------------

    def _persist_publication(
        self,
        *,
        cited_md_url: str,
        slug: str,
        handle: str,
        local_path: Optional[str],
        published_ok: int,
    ) -> None:
        """Insert a row into the ``publications`` table (best-effort, never raises).

        Uses tz-aware UTC timestamps and an incremented ``version`` so the
        ``ReplacingMergeTree(version)`` (ORDER BY run_id) keeps the latest
        publication per run as the winner.
        """
        if self._client is None:
            logger.debug(
                "Publisher: no ClickHouse client; skipping publications persistence"
            )
            return

        run_id = self._run_id or ""
        now = datetime.now(timezone.utc)
        version = self._next_publication_version(run_id)
        row = [
            str(uuid.uuid4()),   # publication_id
            run_id,              # run_id
            cited_md_url,        # cited_md_url
            slug,                # slug
            handle,              # handle
            local_path,          # local_path (Nullable)
            int(published_ok),   # published_ok
            now,                 # published_at
            now,                 # updated_at
            version,             # version
        ]
        try:
            result = self._client.insert(
                "publications", [row], list(PUBLICATIONS_COLUMNS)
            )
            if not getattr(result, "ok", False):
                logger.warning(
                    "Publisher: publications insert failed: %s",
                    getattr(result, "error", "unknown error"),
                )
        except Exception as exc:  # noqa: BLE001 - persistence must never abort the loop
            logger.error(
                "Publisher: publications insert raised: %s", exc, exc_info=True
            )

    def _next_publication_version(self, run_id: str) -> int:
        """Return ``max(version)+1`` for ``run_id`` (1 if none / on read failure)."""
        try:
            result = self._client.query(
                "SELECT max(version) FROM publications WHERE run_id = {run_id:String}",
                parameters={"run_id": run_id},
            )
            if getattr(result, "ok", False) and result.rows:
                current = result.rows[0][0]
                if current is not None:
                    return int(current) + 1
        except Exception:  # noqa: BLE001 - fall back to a time-based version
            logger.debug("Publisher: version read failed", exc_info=True)
            import time

            return int(time.time())
        return 1
