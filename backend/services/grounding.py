"""Deterministic grounding check of a final answer against this run's tool records.

It verifies two things a language model cannot be trusted to self-report: every
evidence ID the answer cites was really returned by a tool in this run, and the
numbers the answer states appear in the tool results (or the user's request).
It cannot judge reasoning quality; it only refuses fabricated references.
"""

import json
import re

EVIDENCE_ID = re.compile(r"\bev_[0-9a-f]{20}\b")
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
MAX_REPORTED = 10


def _numbers(text):
    return {
        token.replace(",", "").rstrip(".")
        for token in NUMBER.findall(EVIDENCE_ID.sub(" ", text))
    }


def _is_trivial(token):
    # List markers and tiny counts are too common to be meaningful claims.
    return "." not in token and token.isdigit() and int(token) < 10


def tool_evidence(messages):
    """(evidence_ids, pooled_text) from tool messages of this run."""
    ids, pool = set(), []
    for message in messages:
        if message.role != "tool" or message.is_error:
            continue
        content = message.content or ""
        pool.append(content)
        try:
            body = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and isinstance(body.get("evidence"), dict):
            evidence_id = body["evidence"].get("evidence_id")
            if isinstance(evidence_id, str):
                ids.add(evidence_id)
    return ids, "\n".join(pool)


def check_answer(answer, messages, user_message=""):
    known, pool = tool_evidence(messages)
    cited = set(EVIDENCE_ID.findall(answer or ""))
    unknown = sorted(cited - known)
    if not known and not cited:
        return {"status": "not_applicable", "cited": [], "unknown_ids": [], "unsupported_numbers": []}
    supported = _numbers(pool) | _numbers(user_message)
    unsupported = sorted(
        {n for n in _numbers(answer or "") if not _is_trivial(n) and n not in supported}
    )[:MAX_REPORTED]
    if unknown:
        status = "invalid_citation"
    elif known and not cited:
        status = "uncited"
    elif unsupported:
        status = "unsupported_numbers"
    else:
        status = "grounded"
    return {
        "status": status,
        "cited": sorted(cited & known),
        "unknown_ids": unknown[:MAX_REPORTED],
        "unsupported_numbers": unsupported,
    }
