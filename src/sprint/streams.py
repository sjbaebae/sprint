"""Bounded binary file adapters for provider transports and checkpoint storage."""

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
