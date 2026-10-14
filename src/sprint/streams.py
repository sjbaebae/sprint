"""Bounded binary file adapters for provider transports and checkpoint storage."""

import asyncio
import contextlib
import io
from typing import override


class ChunkReader(io.RawIOBase):
    """Adapt an iterator of byte blocks to a non-seeking binary file."""

    def __init__(self, chunks):
        super().__init__()
        self.chunks = iter(chunks)
        self.pending = memoryview(b"")

    @override
    def readable(self):
        return True

    @override
    def readinto(self, buffer):
        while not self.pending:
            chunk = next(self.chunks, None)
            if chunk is None:
                return 0
            self.pending = memoryview(chunk)
        count = min(len(buffer), len(self.pending))
        buffer[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count


class ChunkWriter(io.RawIOBase):
    """Adapt a blocking byte-block sink for buffered archive output."""

    def __init__(self, send):
        super().__init__()
        self.send = send

    @override
    def writable(self):
        return True

    @override
    def write(self, data):
        view = memoryview(data)
        for start in range(0, len(view), 1024 * 1024):
            self.send(view[start : start + 1024 * 1024])
        return len(data)


async def finish_transfer(operation):
    """Keep blocking transport cleanup joined even under repeated cancellation."""
    transfer = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(transfer)
    except asyncio.CancelledError:
        while not transfer.done():
            try:
                await asyncio.shield(transfer)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with contextlib.suppress(Exception):
            transfer.result()
        raise
