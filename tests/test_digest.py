import re
from datetime import UTC, datetime

import pytest

from papertrail.dedup import deduplicate
from papertrail.digest import (
    DEFAULT_LIMIT,
    build,
    render_html,
    render_text,
    select,
    subject_line,
)
from papertrail.models import Item
from papertrail.pipeline import Story
from papertrail.provenance import classify
from papertrail.scoring import Score
from papertrail.substance import Flag, Substance

NOW = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def make_story(
    title: str = "Sparse autoencoders scale to frontier models",
    url: str = "https://arxiv.org/abs/2401.00001",
    score: int | None = 8,
    signal: float = 100.0,
    one_line: str = "Sparse autoencoders trained to 34M features.",
    hype: list[str] | None = None,
    flags: tuple[Flag, ...] = (),
    source: str = "hn",
    discussion: str | None = None,
    extra: list[Item] | None = None,
) -> Story:
    item = Item(
        title=title,
        url=url,
        source=source,
        published_at=NOW,
        raw_signal=signal,
        discussion_url=discussion,
    )
    (cluster,) = deduplicate([item, *(extra or [])])
    judgement = (
        None
        if score is None
        else Score.model_validate(
            {
                "id": cluster.cluster_id,
                "signal_score": score,
                "category": "paper",
                "one_line": one_line,
                "hype_flags": hype or [],
                "why": "Because.",
            }
        )
    )
    return Story(
        cluster=cluster,
        provenance=classify(url),
        substance=Substance(flags=flags),
        score=judgement,
    )


# --- selection --------------------------------------------------------------


def test_stories_are_ordered_by_score():
    chosen = select(
        [
            make_story(url="https://arxiv.org/abs/2401.00001", score=4, signal=900.0),
            make_story(url="https://arxiv.org/abs/2401.00002", score=9, signal=10.0),
        ]
    )
    assert [story.signal_score for story in chosen] == [9, 4]


def test_low_scoring_stories_are_left_out():
    chosen = select(
        [
            make_story(url="https://arxiv.org/abs/2401.00001", score=8),
            make_story(url="https://arxiv.org/abs/2401.00002", score=2),
        ],
        min_score=4,
    )
    assert [story.signal_score for story in chosen] == [8]


def test_the_digest_is_capped():
    stories = [make_story(url=f"https://arxiv.org/abs/2401.{i:05d}", score=9) for i in range(30)]
    assert len(select(stories, limit=DEFAULT_LIMIT)) == DEFAULT_LIMIT


def test_unscored_stories_are_kept_but_sort_last():
    """A scoring failure should degrade the digest, not empty it."""
    chosen = select(
        [
            make_story(url="https://arxiv.org/abs/2401.00001", score=None, signal=900.0),
            make_story(url="https://arxiv.org/abs/2401.00002", score=5, signal=10.0),
        ],
        min_score=4,
    )
    assert [story.signal_score for story in chosen] == [5, None]


def test_an_empty_input_selects_nothing():
    assert select([]) == []


# --- subject ----------------------------------------------------------------


def test_the_subject_names_the_lead_story_and_the_count():
    subject = subject_line([make_story(), make_story(url="https://github.com/a/b")], NOW)
    assert "01 Jun" in subject
    assert "Sparse autoencoders" in subject
    assert "+1 more" in subject


def test_a_single_story_subject_has_no_count():
    assert "more" not in subject_line([make_story()], NOW)


def test_a_long_title_is_trimmed_in_the_subject():
    subject = subject_line([make_story(title="x" * 200)], NOW)
    assert len(subject) < 100
    assert "…" in subject


def test_an_empty_day_says_so_in_the_subject():
    assert "nothing cleared the bar" in subject_line([], NOW)


# --- html -------------------------------------------------------------------


def test_the_html_carries_the_story_and_its_summary():
    body = render_html([make_story()], NOW)
    assert "Sparse autoencoders scale to frontier models" in body
    assert "34M features" in body


def test_the_html_uses_inline_styles_only():
    """Gmail strips style blocks and ignores stylesheet links."""
    body = render_html([make_story()], NOW)
    assert "<style" not in body
    assert "<link" not in body
    assert "style=" in body


def test_the_html_has_no_external_assets():
    body = render_html([make_story()], NOW)
    assert "<img" not in body
    assert "<script" not in body


def test_the_score_is_shown():
    assert ">8<" in render_html([make_story(score=8)], NOW).replace("\n", "").replace(" ", "")


def test_an_unscored_story_renders_without_a_score():
    body = render_html([make_story(score=None)], NOW)
    assert "not scored this run" in body


def test_the_primary_source_is_linked_separately_from_the_story():
    story = make_story(url="https://news.example/post")
    story = Story(
        cluster=story.cluster,
        provenance=classify("https://arxiv.org/abs/2401.00001"),
        substance=story.substance,
        score=story.score,
    )
    body = render_html([story], NOW)
    assert "https://news.example/post" in body
    assert "https://arxiv.org/abs/2401.00001" in body


def test_a_discussion_link_is_offered_when_it_differs():
    body = render_html(
        [
            make_story(
                url="https://arxiv.org/abs/1", discussion="https://news.ycombinator.com/item?id=1"
            )
        ],
        NOW,
    )
    assert "discussion" in body


def test_a_thin_artifact_is_said_on_the_face_of_the_story():
    body = render_html([make_story(flags=(Flag.README_ONLY, Flag.WAITLIST))], NOW)
    assert "looks thin" in body


def test_hype_flags_are_shown_in_words():
    body = render_html([make_story(hype=["vendor_benchmark"])], NOW)
    assert "vendor benchmark" in body


def test_other_feeds_that_carried_the_story_are_named():
    echo = Item(
        title="Sparse autoencoders scale to frontier models today",
        url="https://reddit.example/x",
        source="reddit",
        published_at=NOW,
        raw_signal=5.0,
    )
    assert "also on reddit" in render_html([make_story(extra=[echo])], NOW)


def test_an_empty_digest_explains_itself_rather_than_being_blank():
    body = render_html([], NOW)
    assert "Nothing today" in body
    assert "normal outcome" in body


def test_the_footer_counts_what_was_dropped():
    assert "12 dropped" in render_html([make_story()], NOW, dropped=12)


@pytest.mark.parametrize(
    "hostile",
    [
        '<script>alert("x")</script>',
        '"><img src=x onerror=alert(1)>',
        "Ampersands & angle < brackets >",
    ],
)
def test_titles_from_the_open_web_are_escaped(hostile):
    """Escaping neutralizes the markup; the characters survive as inert text."""
    body = render_html([make_story(title=hostile)], NOW)
    assert "<script>" not in body
    assert "<img" not in body
    assert "&lt;" in body or "&amp;" in body


def test_a_hostile_url_cannot_break_out_of_its_attribute():
    body = render_html([make_story(url='https://example.com/"><script>x</script>')], NOW)
    assert "<script>" not in body


def test_the_document_is_well_formed_enough_to_parse():
    body = render_html([make_story(), make_story(url="https://github.com/a/b")], NOW)
    assert body.startswith("<!DOCTYPE html>")
    assert body.count("<table") == body.count("</table>")
    assert len(re.findall(r"<tr[ >]", body)) == body.count("</tr>")


# --- text -------------------------------------------------------------------


def test_the_text_version_carries_the_same_stories():
    text = render_text([make_story()], NOW)
    assert "[8] Sparse autoencoders scale to frontier models" in text
    assert "34M features" in text
    assert "https://arxiv.org/abs/2401.00001" in text


def test_the_text_version_has_no_markup():
    text = render_text([make_story(title="A <b>bold</b> claim")], NOW)
    assert "<div" not in text and "<td" not in text


def test_the_text_version_notes_a_thin_artifact():
    assert "looks thin" in render_text([make_story(flags=(Flag.README_ONLY,))], NOW)


def test_an_empty_text_digest_explains_itself():
    assert "Nothing today" in render_text([], NOW)


# --- build ------------------------------------------------------------------


def test_build_packages_subject_html_and_text():
    digest = build([make_story()], now=NOW)
    assert "Sparse autoencoders" in digest.subject
    assert digest.html.startswith("<!DOCTYPE html>")
    assert "[8]" in digest.text
    assert digest.empty is False


def test_a_day_with_nothing_worth_reading_builds_an_empty_digest():
    digest = build([make_story(score=1)], now=NOW, min_score=4)
    assert digest.empty is True
    assert "nothing cleared the bar" in digest.subject
