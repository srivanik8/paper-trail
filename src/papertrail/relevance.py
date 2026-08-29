"""Cheap topical filter for firehose sources.

Hacker News is a general firehose, so an AI digest has to narrow it before
anything expensive happens. This is a keyword pass over title and URL only --
deliberately crude and deliberately cheap. It is tuned for recall: a false
positive costs one row in a table, a false negative loses a story forever.

Precision is somebody else's job. Day 3 drops anything without a resolvable
primary source, which removes most of what slips through here.
"""

from __future__ import annotations

import re

# Grouped only for readability; they are matched as one alternation.
_TERMS: tuple[str, ...] = (
    # umbrella
    r"a\.?i\.?",
    r"artificial intelligence",
    r"agi\b",
    r"superintelligence",
    r"machine learning",
    r"deep learning",
    r"neural net(work)?s?",
    # model families and the labs that ship them
    r"llms?",
    r"language model",
    r"foundation model",
    r"gpt",
    r"claude",
    r"gemini",
    r"llama",
    r"mistral",
    r"qwen",
    r"deepseek",
    r"openai",
    r"anthropic",
    r"deepmind",
    r"hugging ?face",
    # techniques and artifacts
    r"transformers?",
    r"diffusion",
    r"embeddings?",
    r"fine[- ]?tun\w*",
    r"quantiz\w*",
    r"inference",
    r"prompt\w*",
    r"rag\b",
    r"retrieval[- ]augmented",
    r"agentic",
    r"agents?",
    r"benchmark\w*",
    r"tokeniz\w*",
    r"multimodal",
    r"chatbot",
    r"distillation",
    r"mixture of experts",
    r"moe\b",
    r"context window",
    r"chain[- ]of[- ]thought",
    r"scaling laws?",
    r"pre[- ]?train\w*",
    r"post[- ]?train\w*",
    r"reinforcement learning",
    r"rlhf",
    r"hallucinat\w*",
    r"open[- ]weights?",
    # research vocabulary: these headlines carry no vendor or model name at all
    r"interpretability",
    r"autoencoders?",
    r"alignment",
    r"reasoning model",
    r"frontier model",
    # where the primary sources live
    r"arxiv",
)

_PATTERN = re.compile(rf"(?<![\w-])(?:{'|'.join(_TERMS)})(?![\w-])", re.IGNORECASE)


def matched_terms(*texts: str | None) -> list[str]:
    """Return the distinct topical terms found across ``texts``, lowercased."""
    seen: dict[str, None] = {}
    for text in texts:
        if not text:
            continue
        for match in _PATTERN.finditer(text):
            seen.setdefault(match.group(0).lower(), None)
    return list(seen)


def is_relevant(*texts: str | None) -> bool:
    """True if any topical term appears in any of ``texts``."""
    return any(text and _PATTERN.search(text) for text in texts)
