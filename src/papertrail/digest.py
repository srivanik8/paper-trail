"""Rendering the digest.

Email clients are permanently stuck in 2003, so this is table-based layout with
inline styles and no external assets. That is not laziness -- Gmail strips
``<style>`` blocks, Outlook renders through Word, and a stylesheet link is
simply ignored.

The shape of the page follows the argument the pipeline makes. Each story leads
with what happened in one factual line, then names what you can check it
against, and only then offers the links. A story whose artifact looked thin
carries that on its face rather than in a footnote.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .provenance import Evidence
from .timeutil import utcnow

#: Stories per digest. More than this and it stops being a digest.
DEFAULT_LIMIT = 10

#: Below this score a story is not worth anyone's morning, however it ranked.
DEFAULT_MIN_SCORE = 4

_EVIDENCE_LABEL: dict[Evidence, str] = {
    Evidence.PAPER: "paper",
    Evidence.REPO: "repository",
    Evidence.MODEL_WEIGHTS: "weights",
    Evidence.OFFICIAL_BLOG: "official post",
    Evidence.NONE: "unverified",
}

_INK = "#151a18"
_MUTED = "#67746f"
_RULE = "#d6ddd8"
_ACCENT = "#14624e"
_FLAG = "#a6402f"
_PAPER = "#ffffff"


@dataclass(frozen=True, slots=True)
class Digest:
    """One rendered digest, ready to send."""

    subject: str
    html: str
    text: str
    stories: list[Any]

    @property
    def empty(self) -> bool:
        """True if nothing cleared the bar today."""
        return not self.stories


def select(
    stories: list[Any], limit: int = DEFAULT_LIMIT, min_score: int = DEFAULT_MIN_SCORE
) -> list[Any]:
    """Choose what goes in the digest.

    Unscored stories are kept -- a scoring failure should degrade the digest,
    not empty it -- but they sort below everything the model actually judged.
    """
    eligible = [
        story for story in stories if story.signal_score is None or story.signal_score >= min_score
    ]
    eligible.sort(key=lambda story: story.rank_key, reverse=True)
    return eligible[:limit]


def subject_line(stories: list[Any], now: datetime) -> str:
    """Subject naming the day and the lead story, so the inbox preview is useful."""
    date = now.strftime("%d %b")
    if not stories:
        return f"paper-trail {date}: nothing cleared the bar"

    lead = stories[0].canonical.title
    if len(lead) > 60:
        lead = lead[:59].rstrip() + "…"
    more = f" +{len(stories) - 1} more" if len(stories) > 1 else ""
    return f"paper-trail {date}: {lead}{more}"


def _esc(value: str) -> str:
    """Escape for HTML. Every string in the digest came from the open web."""
    return html.escape(value or "", quote=True)


def _story_html(story: Any, position: int) -> str:
    """Render one story as a table row."""
    item = story.canonical
    score = story.signal_score
    badge = _EVIDENCE_LABEL[story.evidence]

    links = [f'<a href="{_esc(item.url)}" style="color:{_ACCENT};">source</a>']
    if story.provenance.url and story.provenance.url != item.url:
        links.append(f'<a href="{_esc(story.provenance.url)}" style="color:{_ACCENT};">{badge}</a>')
    if item.discussion_url and item.discussion_url != item.url:
        links.append(
            f'<a href="{_esc(item.discussion_url)}" style="color:{_ACCENT};">discussion</a>'
        )

    notes = []
    if story.thin:
        notes.append("the repository behind this looks thin")
    if story.score is not None and story.score.hype_flags:
        notes.append(", ".join(flag.value.replace("_", " ") for flag in story.score.hype_flags))
    if story.cluster.also_seen:
        notes.append("also on " + ", ".join(story.cluster.also_seen))

    one_line = (
        _esc(story.score.one_line) if story.score is not None else "<em>not scored this run</em>"
    )
    rating = f"{score}" if score is not None else "&ndash;"

    note_html = (
        f'<div style="margin:6px 0 0;font-size:13px;color:{_FLAG};">'
        f"{_esc(' &middot; '.join(notes))}</div>"
        if notes
        else ""
    )

    return f"""
      <tr>
        <td style="padding:18px 0 0;vertical-align:top;width:34px;">
          <div style="font:600 15px/1.2 Georgia,serif;color:{_ACCENT};">{rating}</div>
        </td>
        <td style="padding:18px 0 0;vertical-align:top;">
          <div style="font:600 17px/1.35 Georgia,serif;color:{_INK};">
            {_esc(item.title)}
          </div>
          <div style="margin:6px 0 0;font:15px/1.5 -apple-system,sans-serif;color:{_INK};">
            {one_line}
          </div>
          {note_html}
          <div style="margin:8px 0 0;font:13px/1.4 -apple-system,sans-serif;color:{_MUTED};">
            {badge} &middot; {" &middot; ".join(links)}
          </div>
        </td>
      </tr>
      <tr><td colspan="2" style="padding-top:18px;border-bottom:1px solid {_RULE};"></td></tr>
    """.replace("\n      ", "\n").strip()


def render_html(stories: list[Any], now: datetime, dropped: int = 0) -> str:
    """Render the digest as an email-safe HTML document."""
    date = now.strftime("%A %d %B %Y")

    if stories:
        body = "\n".join(_story_html(story, i) for i, story in enumerate(stories))
    else:
        body = (
            f'<tr><td colspan="2" style="padding:24px 0;font:15px/1.5 '
            f'-apple-system,sans-serif;color:{_MUTED};">Nothing today traced back to a '
            f"primary source and cleared the bar. That is a normal outcome.</td></tr>"
        )

    footer = f"{len(stories)} stor{'y' if len(stories) == 1 else 'ies'}"
    if dropped:
        footer += f" &middot; {dropped} dropped for having no primary source"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>paper-trail</title></head>
<body style="margin:0;padding:0;background:#f3f6f4;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#f3f6f4;padding:24px 12px;">
 <tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:620px;background:{_PAPER};padding:28px 26px 24px;">
   <tr><td colspan="2" style="padding-bottom:4px;">
     <div style="font:600 20px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;color:{_INK};">
       paper<span style="color:{_ACCENT};">-</span>trail
     </div>
     <div style="margin:4px 0 0;font:13px/1.4 -apple-system,sans-serif;color:{_MUTED};">
       {_esc(date)} &middot; only what traces back to a primary source
     </div>
   </td></tr>
   <tr><td colspan="2" style="padding-top:14px;border-bottom:2px solid {_INK};"></td></tr>
   {body}
   <tr><td colspan="2" style="padding:16px 0 0;font:12px/1.5 -apple-system,
              sans-serif;color:{_MUTED};">{footer}</td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""


def render_text(stories: list[Any], now: datetime) -> str:
    """Plain-text alternative, for clients that refuse HTML."""
    lines = [f"paper-trail — {now.strftime('%A %d %B %Y')}", ""]

    if not stories:
        lines.append("Nothing today traced back to a primary source and cleared the bar.")
        return "\n".join(lines)

    for story in stories:
        score = story.signal_score if story.signal_score is not None else "-"
        lines.append(f"[{score}] {story.canonical.title}")
        if story.score is not None:
            lines.append(f"    {story.score.one_line}")
        if story.thin:
            lines.append("    (the repository behind this looks thin)")
        lines.append(f"    {story.canonical.url}")
        if story.provenance.url and story.provenance.url != story.canonical.url:
            lines.append(f"    {_EVIDENCE_LABEL[story.evidence]}: {story.provenance.url}")
        lines.append("")

    return "\n".join(lines)


def build(
    stories: list[Any],
    now: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    min_score: int = DEFAULT_MIN_SCORE,
    dropped: int = 0,
) -> Digest:
    """Select, render and package a digest."""
    moment = now or utcnow()
    chosen = select(stories, limit=limit, min_score=min_score)
    return Digest(
        subject=subject_line(chosen, moment),
        html=render_html(chosen, moment, dropped=dropped),
        text=render_text(chosen, moment),
        stories=chosen,
    )
