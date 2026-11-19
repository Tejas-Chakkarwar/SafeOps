"""Request-body reading for the testbed HTTP servers (decision 0009).

Stdlib-only, so SafeOps tests can drive it directly. The API-server service proxy
re-streams request bodies with `Transfer-Encoding: chunked`, so the harness's
PUT /control arrives without a Content-Length. Both framings are handled, with a size cap.
"""

from __future__ import annotations

from typing import BinaryIO

DEFAULT_LIMIT = 1 << 20  # 1 MiB; control bodies are a few dozen bytes


class BodyError(ValueError):
    pass


def read_body(headers, rfile: BinaryIO, limit: int = DEFAULT_LIMIT) -> bytes:
    """Read one request body. `headers` needs .get(name, default) (e.g. http.client.HTTPMessage)."""
    if "chunked" in (headers.get("Transfer-Encoding", "") or "").lower():
        return _read_chunked(rfile, limit)
    raw_length = headers.get("Content-Length")
    try:
        length = int(raw_length) if raw_length else 0
    except ValueError as exc:
        raise BodyError(f"invalid Content-Length {raw_length!r}") from exc
    if length < 0 or length > limit:
        raise BodyError(f"Content-Length {length} outside 0..{limit}")
    data = rfile.read(length) if length else b""
    if len(data) != length:
        raise BodyError("body shorter than Content-Length")
    return data


def _read_chunked(rfile: BinaryIO, limit: int) -> bytes:
    data = bytearray()
    while True:
        line = rfile.readline(1024)
        if not line:
            raise BodyError("connection closed inside a chunked body")
        size_field = line.split(b";", 1)[0].strip()  # drop chunk extensions
        try:
            size = int(size_field, 16)
        except ValueError as exc:
            raise BodyError(f"invalid chunk size {size_field!r}") from exc
        if size < 0:
            raise BodyError("negative chunk size")
        if size == 0:
            while True:  # trailers end with an empty line
                trailer = rfile.readline(1024)
                if trailer in (b"\r\n", b"\n", b""):
                    return bytes(data)
        if len(data) + size > limit:
            raise BodyError(f"chunked body exceeds {limit} bytes")
        chunk = rfile.read(size)
        if len(chunk) != size:
            raise BodyError("connection closed inside a chunk")
        data += chunk
        if rfile.readline(1024) not in (b"\r\n", b"\n"):
            raise BodyError("missing CRLF after chunk")
