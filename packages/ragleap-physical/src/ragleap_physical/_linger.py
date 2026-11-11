"""Close a refused connection the way HTTP servers are meant to (the "lingering close" of the HTTP specification).

A server that answers a request and then closes a socket that still holds unread data makes the operating system send a reset.
On Windows that reset can destroy the answer before the client has read it, and the client reports WinError 10053. The remedy:
after the answer is sent, stop sending, then read and throw away what the client is still sending, for a short and bounded time.
"""
import socket
import time

LINGER_SECONDS = 0.5      # the longest a refused connection is kept open waiting for the client to finish
LINGER_MAX_BYTES = 1 << 20  # the most that is read and discarded; a client that sends more than this is cut off


def linger(connection, seconds: float = LINGER_SECONDS, max_bytes: int = LINGER_MAX_BYTES) -> int:
    """Half-close, then discard incoming data until the client closes, the time or the byte limit is reached. Returns the bytes discarded."""
    total = 0
    try:
        connection.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + seconds
        while total < max_bytes:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            connection.settimeout(left)
            chunk = connection.recv(65536)
            if not chunk:
                break
            total += len(chunk)
    except OSError:
        pass
    return total
