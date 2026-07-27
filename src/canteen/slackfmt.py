"""Standard markdown -> Slack mrkdwn.

Models write CommonMark. Slack does not read CommonMark: `*x*` is bold rather
than italic, links are `<url|text>`, and `**x**` renders as literal asterisks.
Telling the model to emit mrkdwn helps but is not reliable, so every reply is
converted here before it is posted.
"""

from __future__ import annotations

import re

# `*x*` is deliberately left alone. It means bold in Slack and italic in
# CommonMark, and the model emits both dialects, so the two readings cannot be
# told apart. Treating it as bold is correct for the Slack dialect and merely
# cosmetic when it was meant as italic — whereas rewriting it to `_x_` turned
# real bold into italic, which is the worse failure. `_x_` is italic in both
# dialects, so it needs no rule at all.
_BOLD = "\x00"

_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def _convert(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", rf"{_BOLD}\1{_BOLD}", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", rf"{_BOLD}\1{_BOLD}", text, flags=re.DOTALL)
    text = text.replace(_BOLD, "*")

    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    text = re.sub(r"\[([^\]]+)\]\((\S+?)\)", r"<\2|\1>", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.MULTILINE)
    return text


def to_mrkdwn(text: str) -> str:
    """Convert everything except code spans and fenced blocks, which are data."""
    return "".join(
        part if i % 2 else _convert(part)
        for i, part in enumerate(_CODE.split(text or ""))
    )
