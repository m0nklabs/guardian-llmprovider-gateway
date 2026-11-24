"""SSE framing — the one rule for "is this an ordinary ``data:`` chunk?".

The rule used to live in three places: ``app.cloud_inference.forwarding``
(``_sse_data_payload``), its delegating ``_reported_model_from_line`` and an
inline copy inside ``app.capture.stream_assembler``.  That copy existed only
because ``app.capture`` may not import ``app.gateway`` (``capture_dispatch``
imports the assembler, so the direction is circular) — and the copies drifted:
a substring test on the ``[DONE]`` marker silently dropped chunks whose
*content* spelled it, costing the served-model capture (PR #128 review) and
letting a repetition loop whose unit spells the marker run to the end of the
upstream stream (issue #129).

This module is the neutral home both layers may import.  It deliberately
imports **nothing** from ``app.*``, so using it can never reintroduce the
cycle; ``tests/unit/test_sse.py`` guards that.
"""

from __future__ import annotations

#: Prefix every ordinary SSE data line carries.
SSE_DATA_PREFIX = "data: "

#: The end-of-stream sentinel.  Compared against the *whole* payload, never as
#: a substring: a chunk whose content merely spells it is ordinary data.
SSE_DONE_SENTINEL = "[DONE]"


def sse_data_payload(line: str) -> str | None:
    """Return the payload of one ordinary SSE data line, ``None`` for the rest.

    ``None`` means "there is no chunk to parse here": no ``data: `` prefix, an
    empty payload, or the real ``data: [DONE]`` sentinel.  The sentinel is
    compared **exactly** against the whole stripped payload, so a JSON chunk
    whose content spells ``[DONE]`` stays an ordinary data line.

    Only framing is decided here.  A returned payload is neither validated as
    JSON nor required to be an object — the consumer owns that judgement, and
    is expected to reject a malformed or non-object payload itself.
    """
    if not line.startswith(SSE_DATA_PREFIX):
        return None
    payload = line[len(SSE_DATA_PREFIX) :].strip()
    if not payload or payload == SSE_DONE_SENTINEL:
        return None
    return payload
