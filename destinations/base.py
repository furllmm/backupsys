"""
destinations.base
==================

Every remote/local backend (local folder, SFTP, WebDAV, S3-compatible)
implements this same narrow object-storage interface: put/get/exists/
stat/delete/list, keyed by a flat string key such as
``data/ab/cd/abcd1234...`` or ``snapshots/snapshot_20260701.json``.

Keeping the interface this narrow (rather than exposing e.g. "directories"
as a first-class concept) is deliberate: it's the same interface S3 itself
exposes, and every other backend (SFTP, WebDAV, local disk) can trivially
emulate a flat keyspace by creating parent directories on demand. This is
what makes :mod:`destinations.repository` backend-agnostic.

Concrete backends never see plaintext backup content directly -- the
:mod:`destinations.repository` layer applies compression/encryption before
calling :meth:`Destination.put_object`, so a backend only ever moves
opaque bytes around.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Optional


class DestinationError(RuntimeError):
    """Base class for all destination/backend errors."""


class DestinationConnectionError(DestinationError):
    """Raised when a backend can't be reached (network down, auth failure,
    host unreachable, ...). Callers (the repository / offline queue) use
    this specific type to decide whether to queue an operation for later
    retry rather than treating it as a permanent failure.
    """


class DestinationNotFoundError(DestinationError):
    """Raised by get_object()/stat() when the key doesn't exist."""


class Destination(ABC):
    """Common interface implemented by every destination backend."""

    #: Short machine-readable backend identifier, e.g. "local", "sftp".
    kind: str = "base"

    def connect(self) -> None:
        """Establish any persistent connection (SSH session, HTTP client,
        S3 client, ...). Backends that are inherently connectionless (e.g.
        local filesystem) can leave this a no-op. Must raise
        :class:`DestinationConnectionError` on failure, never a bare
        library-specific exception, so callers can handle all backends
        uniformly.
        """

    def disconnect(self) -> None:
        """Release any connection opened by :meth:`connect`. Safe to call
        even if never connected."""

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Return ``(ok, message)``. Never raises -- this is the method
        the GUI/CLI "Test" button calls, so failures must be reported as
        data, not exceptions.
        """

    @abstractmethod
    def put_object(self, key: str, data: bytes) -> None:
        """Write ``data`` under ``key``, creating any needed parent
        "directories" implicitly. Must be atomic where the backend
        supports it (write-temp-then-rename for local/SFTP; PUT is
        naturally atomic for S3/WebDAV at the object level).
        """

    @abstractmethod
    def get_object(self, key: str) -> bytes:
        """Raise :class:`DestinationNotFoundError` if ``key`` doesn't
        exist."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def stat(self, key: str) -> Optional[int]:
        """Return the object's size in bytes, or None if it doesn't
        exist. Used by the repository layer to implement resume-by-
        skipping-existing-objects without a full download.
        """

    @abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abstractmethod
    def list_objects(self, prefix: str = "") -> Iterable[str]:
        ...

    # ------------------------------------------------------------------
    def __enter__(self) -> "Destination":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()
