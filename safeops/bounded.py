"""Bounded reads of external input (decision 0025, M8B-2 STOP-resolution amendment).

A neutral leaf: standard library only, no SafeOps import. Used by the executor's direct
Kubernetes PATCH, where an HTTP response body is the external input. The tools' text `cap()`
(safeops.tools.reader) is a different, character-oriented bound and stays where it is.
"""

from __future__ import annotations


def read_bounded_bytes(stream, limit: int) -> tuple[bytes, bool]:
    """Read at most `limit + 1` bytes from `stream` (anything with `read(size)`), and return
    `(the first limit bytes, truncated)`: truncated is True exactly when more than `limit`
    bytes were there. Every `read` call is given a finite size, never more than is still
    wanted, so nothing larger than `limit + 1` bytes is ever requested in total; nothing is
    sized from what the stream claims (a Content-Length, say)."""
    if type(limit) is not int or limit < 0:
        raise ValueError(f"the byte limit must be a non-negative int, not {limit!r}")
    want = limit + 1
    chunks: list[bytes] = []
    got = 0
    while got < want:
        chunk = stream.read(want - got)
        if not chunk:
            break
        if type(chunk) is not bytes:
            raise TypeError(f"the stream returned {type(chunk).__name__}, not bytes")
        if len(chunk) > want - got:
            raise ValueError("the stream returned more bytes than were asked for")
        chunks.append(chunk)
        got += len(chunk)
    data = b"".join(chunks)
    return data[:limit], len(data) > limit
