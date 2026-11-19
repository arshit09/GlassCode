"""
speakable.py - turns Claude's written answer into something nice to HEAR.

Your glasses have no screen, so the answer is read aloud. Markdown symbols,
code blocks and long links sound terrible when spoken, so we clean them up
and keep the spoken part short. The full answer is always saved in
logs/history.md, so nothing is lost.
"""
from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.S)          # ```python ... ```
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")        # [text](url) -> text
_URL = re.compile(r"https?://\S+")                         # bare links
_INLINE_CODE = re.compile(r"`([^`]*)`")                    # `npm test` -> npm test
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)          # ## Title
_BULLET = re.compile(r"^\s*(?:[-*+•])\s+", re.M)      # - item / * item / • item
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+", re.M)            # 1. item
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")                    # **bold** / __bold__
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")  # *italic*
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}.*$", re.M)    # |---|---| lines
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def make_speakable(text: str, max_chars: int = 600) -> str:
    """Return a short, plain-spoken version of `text`.

    Example:
        "## Done\\n- Created `README.md`\\n- Ran **npm test**: all passed"
        -> "Done Created README.md Ran npm test: all passed"
    """
    if not text:
        return ""
    t = _CODE_BLOCK.sub("\nI left the code in the log.\n", text)
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub("a link", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _TABLE_RULE.sub(" ", t)
    t = _HEADING.sub("", t)
    t = _BULLET.sub("", t)
    t = _NUMBERED.sub("", t)
    t = _BOLD.sub(r"\2", t)
    t = _ITALIC.sub(r"\1", t)
    t = t.replace("|", " ").replace(">", " ").replace("#", " ")
    # Line breaks become sentence pauses, then squash extra spaces.
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\.\s*\.", ".", t)
    t = re.sub(r"\s+", " ", t).strip(" .") + "."
    t = t.replace(":.", ":").replace("..", ".")

    if len(t) <= max_chars:
        return t
    # Too long: cut at the last full sentence that fits, then add a pointer.
    cut = t[:max_chars]
    ends = [m.end() for m in _SENTENCE_END.finditer(cut)]
    if ends and ends[-1] > max_chars * 0.4:
        cut = cut[: ends[-1]]
    else:
        cut = cut.rsplit(" ", 1)[0] + "..."
    return cut.strip() + " The full answer is in the log."


def short_echo(text: str, max_words: int = 14) -> str:
    """First few words of what you said, used for "Got it: ..." replies."""
    words = (text or "").split()
    if len(words) <= max_words:
        return " ".join(words).rstrip(".!?")
    return " ".join(words[:max_words]) + "..."
