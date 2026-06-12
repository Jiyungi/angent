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

The Deal_Memo is **always markdown** and is **never** OpenUI Lang — the two
output types are produced by separate code paths and remain separate
(Requirement 20.6, enforced by task 20.3). :meth:`Publisher.publish` is stubbed
here and fully implemented (Senso call + persistence + local fallback) in
task 20.2.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .models import Qualified

logger = logging.getLogger("angent.publisher")


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


# --- Publisher --------------------------------------------------------------


class Publisher:
    """Serializes qualified companies into a Deal_Memo and (20.2) publishes it.

    ``client`` (a ClickHouseClient) is optional and only needed when
    :meth:`serialize` is asked to read companies from the blackboard rather than
    being handed a list directly.
    """

    def __init__(self, client: Any = None, *, run_id: Optional[str] = None) -> None:
        self._client = client
        self._run_id = run_id

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

    # -- publish (stub; implemented in task 20.2) ----------------------------

    def publish(self, deal_memo: DealMemo) -> PublishResult:
        """Publish ``deal_memo`` to cited.md via Senso.

        Stubbed for task 20.1 — the Senso CLI call, ``publications`` persistence
        and local-file fallback are implemented in task 20.2. The Deal_Memo is
        kept as markdown here and never converted to OpenUI Lang.
        """
        raise NotImplementedError(
            "Publisher.publish is implemented in task 20.2 (Senso publish + "
            "persistence + local fallback)."
        )
