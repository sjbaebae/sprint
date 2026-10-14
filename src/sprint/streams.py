"""Bounded binary file adapters for provider transports and checkpoint storage."""

import asyncio
import contextlib
import io
import os
from threading import Thread
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


def pipe_transfer(produce, consume):
    """Join a bounded local binary pipe, draining successful consumers fully.

    Closing the reader releases a failed producer from backpressure. Always join
    the producer before returning; errors must not leave background archive work.
    """
    read_fd, write_fd = os.pipe()
    errors = []

    def write():
        try:
            with os.fdopen(write_fd, "wb") as output:
                produce(output)
        except BaseException as error:
            errors.append(error)

    producer = Thread(target=write, name="sprint-archive-producer")
    try:
        producer.start()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    failure = None
    result = None
    try:
        with os.fdopen(read_fd, "rb") as source:
            result = consume(source)
            # Tar consumers may stop before the padded archive ends.
            while source.read(1024 * 1024):
                pass
    except BaseException as error:
        failure = error
    finally:
        producer.join()
    if errors and (failure is None or not isinstance(errors[0], BrokenPipeError)):
        raise errors[0]
    if failure is not None:
        raise failure
    return result


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
