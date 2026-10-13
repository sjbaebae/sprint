"""Safe local Sprint transfers inside the operator-allocated CPU container."""

import os
from pathlib import Path
import secrets
import stat


def directory_fd(path, create=False):
    """Open an absolute directory through O_NOFOLLOW ancestors.

    Optionally create missing directories. Caller owns the returned FD and must
    close it. Raises on links, noncanonical components, or filesystem errors.
    """
    path = Path(path).absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if component in (".", ".."):
                raise ValueError("noncanonical transfer directory")
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def copy_file(source, target):
    """Copy a regular source through FDs and atomically replace the target entry.

    Rejects source/ancestor links and special files. A target symlink is replaced,
    never followed. Preserves an existing regular target's mode and owner when
    root; cleans temporary files on failure. Returns None.
    """
    source, target = Path(source).absolute(), Path(target).absolute()
    source_dir = directory_fd(source.parent)
    target_dir = directory_fd(target.parent, create=True)
    temporary = ".sprint-transfer-" + secrets.token_hex(16)
    try:
        source_fd = os.open(
            source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_dir
        )
        try:
            info = os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("transfer source must be regular")
            try:
                old = os.stat(target.name, dir_fd=target_dir, follow_symlinks=False)
            except FileNotFoundError:
                old = None
            target_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=target_dir,
            )
            with os.fdopen(target_fd, "wb") as stream:
                while block := os.read(source_fd, 1024 * 1024):
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
                mode = (
                    stat.S_IMODE(old.st_mode) & 0o777
                    if old and stat.S_ISREG(old.st_mode)
                    else stat.S_IMODE(info.st_mode) & 0o777
                )
                os.fchmod(stream.fileno(), mode)
                if old and stat.S_ISREG(old.st_mode) and os.geteuid() == 0:
                    os.fchown(stream.fileno(), old.st_uid, old.st_gid)
            os.replace(
                temporary, target.name, src_dir_fd=target_dir, dst_dir_fd=target_dir
            )
        finally:
            os.close(source_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=target_dir)
        except FileNotFoundError:
            pass
        os.close(source_dir)
        os.close(target_dir)


def copy_directory(source, target):
    """Recursively transfer a tree of regular files/directories without links.

    Creates the destination and delegates file replacement to copy_file. Raises
    on any symlink or special entry; an error can leave a partially copied tree.
    """
    source, target = Path(source).absolute(), Path(target).absolute()
    fd = directory_fd(source)
    os.close(fd)
    fd = directory_fd(target, create=True)
    os.close(fd)
    for path in source.iterdir():
        if path.is_symlink():
            raise ValueError("directory transfers reject symlinks")
        destination = target / path.name
        if path.is_dir():
            copy_directory(path, destination)
        elif path.is_file():
            copy_file(path, destination)
        else:
            raise ValueError("directory transfers reject special files")
