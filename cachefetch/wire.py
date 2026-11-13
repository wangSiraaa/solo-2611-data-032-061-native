"""Minimal HTTP/1.1 wire handling built on ``socket`` (standard library).

Only what this task needs is implemented: a GET request/response exchange,
strict body framing by ``Content-Length`` or chunked transfer encoding, and
no redirects, compression negotiation or persistent connections.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

from .headers import ProtocolError


MAX_HEAD_BYTES = 64 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024
IO_TIMEOUT_SECONDS = 10.0


class WireError(Exception):
    """Transport-level failure (connect, reset, truncation, timeout)."""


class ShortResponse(WireError):
    """The peer closed or the body ended before the declared length."""

    def __init__(self, message: str, received: bytes):
        super().__init__(message)
        self.received = received


@dataclass
class HTTPRequest:
    method: str
    target: str
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class HTTPResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes

    def header_lines(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]

    def header(self, name: str) -> str | None:
        values = self.header_lines(name)
        return values[0] if values else None

    @property
    def etag(self) -> str | None:
        values = self.header_lines("ETag")
        if len(values) > 1:
            raise ProtocolError("multiple ETag headers")
        return values[0] if values else None


class _Reader:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = bytearray()
        self.total_received = 0

    def _fill(self) -> None:
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout as exc:
            raise WireError("timed out reading response") from exc
        except OSError as exc:
            raise WireError(f"read failed: {exc}") from exc
        if not chunk:
            raise WireError("connection closed before a complete response")
        self.total_received += len(chunk)
        self._buffer.extend(chunk)

    def read_line(self) -> bytes:
        while True:
            index = self._buffer.find(b"\r\n")
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[: index + 2]
                return line
            if len(self._buffer) > MAX_HEAD_BYTES:
                raise ProtocolError("header line too long")
            self._fill()

    def read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            try:
                self._fill()
            except WireError as exc:
                raise ShortResponse(
                    f"body ended early: wanted {count} bytes, got {len(self._buffer)}",
                    bytes(self._buffer),
                ) from exc
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def read_chunked(self) -> bytes:
        body = bytearray()
        while True:
            line = self.read_line()
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise ProtocolError("invalid chunk size") from exc
            if size < 0 or len(body) + size > MAX_BODY_BYTES:
                raise ProtocolError("chunk size out of range")
            if size == 0:
                while True:
                    trailer = self.read_line()
                    if trailer == b"":
                        return bytes(body)
            try:
                body.extend(self.read_exact(size))
            except ShortResponse as exc:
                raise ShortResponse("chunked body truncated", bytes(body)) from exc
            if self.read_line() != b"":
                raise ProtocolError("missing CRLF after chunk data")


def read_response(sock: socket.socket) -> HTTPResponse:
    reader = _Reader(sock)
    status_line = reader.read_line()
    parts = status_line.decode("iso-8859-1").split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1."):
        raise ProtocolError(f"invalid status line: {status_line!r}")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise ProtocolError(f"invalid status code: {parts[1]!r}") from exc
    reason = parts[2] if len(parts) > 2 else ""

    headers: list[tuple[str, str]] = []
    head_size = len(status_line) + 2
    while True:
        line = reader.read_line()
        if not line:
            break
        head_size += len(line) + 2
        if head_size > MAX_HEAD_BYTES:
            raise ProtocolError("response headers too large")
        if b":" not in line:
            raise ProtocolError(f"invalid header line: {line!r}")
        raw_name, raw_value = line.split(b":", 1)
        name = raw_name.decode("iso-8859-1").strip()
        value = raw_value.decode("iso-8859-1").strip(" \t")
        if not name:
            raise ProtocolError("empty header name")
        headers.append((name, value))

    response = HTTPResponse(status, reason, headers, b"")
    transfer = response.header_lines("Transfer-Encoding")
    content_lengths = response.header_lines("Content-Length")

    bodyless = status in (204, 304) or 100 <= status < 200
    if bodyless:
        # A 304 must not carry a body; any advertised length is metadata only.
        response.body = b""
        return response

    if transfer:
        tokens = [t.strip().lower() for t in ",".join(transfer).split(",") if t.strip()]
        if tokens and tokens[-1] == "chunked":
            response.body = reader.read_chunked()
            return response
        raise ProtocolError("unsupported Transfer-Encoding")

    if not content_lengths:
        raise ProtocolError("response has neither Content-Length nor chunked framing")
    if len(set(content_lengths)) > 1:
        raise ProtocolError("conflicting Content-Length headers")
    try:
        length = int(content_lengths[0])
    except ValueError as exc:
        raise ProtocolError("invalid Content-Length") from exc
    if length < 0 or length > MAX_BODY_BYTES:
        raise ProtocolError("Content-Length out of range")
    response.body = reader.read_exact(length)
    _check_no_extra_data(reader)
    return response


def _check_no_extra_data(reader: "_Reader") -> None:
    """Reject bytes after a body framed by Content-Length.

    Requests send ``Connection: close`` and the simulated server closes after
    each response, so draining to EOF is deterministic: a compliant response
    ends with the declared body and then EOF; any leftover buffered byte or
    any byte read before EOF is extra framing.  The socket timeout is only a
    transport safety net, not a test clock.
    """
    if reader._buffer:
        raise ProtocolError(
            f"{len(reader._buffer)} extra byte(s) after the declared Content-Length"
        )
    sock = reader._sock
    while True:
        try:
            extra = sock.recv(65536)
        except socket.timeout as exc:
            raise WireError("timed out draining the response") from exc
        except OSError:
            return  # peer reset: framed body was already complete
        if not extra:
            return
        raise ProtocolError(
            f"{len(extra)} extra byte(s) after the declared Content-Length"
        )


def send_request(sock: socket.socket, request: HTTPRequest) -> None:
    lines = [f"{request.method} {request.target} HTTP/1.1"]
    for name, value in request.headers:
        lines.append(f"{name}: {value}")
    data = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")
    try:
        sock.sendall(data)
    except OSError as exc:
        raise WireError(f"send failed: {exc}") from exc


def http_exchange(
    host: str,
    port: int,
    target: str,
    headers: list[tuple[str, str]],
    timeout: float = IO_TIMEOUT_SECONDS,
) -> HTTPResponse:
    """Perform one GET on a fresh connection and return the parsed response."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise WireError(f"connect failed: {exc}") from exc
    with sock:
        sock.settimeout(timeout)
        request = HTTPRequest("GET", target, headers)
        send_request(sock, request)
        return read_response(sock)
