"""
destinations.webdav
====================

Generic WebDAV destination. Nextcloud, ownCloud, and Seafile all expose a
standard WebDAV endpoint (e.g. ``https://host/remote.php/dav/files/user/``
for Nextcloud, ``https://host/remote.php/webdav/`` for ownCloud,
``https://host/seafdav/`` for Seafile) -- the user supplies that full base
URL, and this backend speaks plain WebDAV (PUT/GET/HEAD/DELETE/MKCOL/
PROPFIND) against it via ``requests``. This is deliberately product-agnostic:
we never special-case any one vendor's API, only the standard protocol
every one of them supports.

Requires the ``requests`` package.
"""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

try:
    import requests

    _REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REQUESTS_AVAILABLE = False

from .base import (
    Destination,
    DestinationConnectionError,
    DestinationNotFoundError,
)

_DAV_NS = "{DAV:}"


class WebDAVDestination(Destination):
    kind = "webdav"

    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_tls: bool = True,
        remote_root: str = "backupsys",
    ):
        if not _REQUESTS_AVAILABLE:
            raise ImportError(
                "The 'requests' package is required for WebDAV destinations. "
                "Install it with: pip install requests"
            )
        self.base_url = base_url.rstrip("/") + "/"
        self.remote_root = remote_root.strip("/")
        self.verify_tls = verify_tls
        self._session: Optional["requests.Session"] = None
        self._auth = (username, password) if username else None

    # ------------------------------------------------------------------
    def connect(self) -> None:
        if self._session is not None:
            return
        self._session = requests.Session()
        if self._auth:
            self._session.auth = self._auth
        try:
            self._mkcol_recursive(self.remote_root)
        except requests.RequestException as exc:
            raise DestinationConnectionError(f"WebDAV connect to {self.base_url} failed: {exc}") from exc

    def disconnect(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            resp = self._session.request("PROPFIND", self._url(""), headers={"Depth": "0"}, verify=self.verify_tls, timeout=15)
            if resp.status_code in (207, 200):
                return True, f"OK: {self.base_url}{self.remote_root}/ is reachable"
            return False, f"Unexpected status {resp.status_code} from {self.base_url}"
        except (DestinationConnectionError, requests.RequestException) as exc:
            return False, str(exc)
        finally:
            self.disconnect()

    # ------------------------------------------------------------------
    def _url(self, key: str) -> str:
        parts = [p for p in key.split("/") if p not in ("", ".", "..")]
        path = "/".join([self.remote_root, *parts]) if parts else self.remote_root
        return urljoin(self.base_url, path)

    def _mkcol_recursive(self, remote_dir: str) -> None:
        parts = [p for p in remote_dir.strip("/").split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            url = urljoin(self.base_url, current)
            resp = self._session.request("MKCOL", url, verify=self.verify_tls, timeout=15)
            # 201 = created, 405 = already exists -- both are fine.
            if resp.status_code not in (201, 405, 301):
                raise DestinationConnectionError(
                    f"Could not create WebDAV collection {current} (status {resp.status_code})"
                )

    def _ensure_parent(self, key: str) -> None:
        parent = posixpath.dirname(key)
        if parent:
            self._mkcol_recursive(f"{self.remote_root}/{parent}")

    def put_object(self, key: str, data: bytes) -> None:
        self.connect()
        self._ensure_parent(key)
        try:
            resp = self._session.put(self._url(key), data=data, verify=self.verify_tls, timeout=60)
            if resp.status_code not in (200, 201, 204):
                raise DestinationConnectionError(f"PUT {key} failed (status {resp.status_code})")
        except requests.RequestException as exc:
            raise DestinationConnectionError(f"WebDAV write failed for {key}: {exc}") from exc

    def get_object(self, key: str) -> bytes:
        self.connect()
        try:
            resp = self._session.get(self._url(key), verify=self.verify_tls, timeout=60)
        except requests.RequestException as exc:
            raise DestinationConnectionError(f"WebDAV read failed for {key}: {exc}") from exc
        if resp.status_code == 404:
            raise DestinationNotFoundError(key)
        if resp.status_code != 200:
            raise DestinationConnectionError(f"GET {key} failed (status {resp.status_code})")
        return resp.content

    def exists(self, key: str) -> bool:
        return self.stat(key) is not None

    def stat(self, key: str) -> Optional[int]:
        self.connect()
        try:
            resp = self._session.head(self._url(key), verify=self.verify_tls, timeout=15)
        except requests.RequestException as exc:
            raise DestinationConnectionError(f"WebDAV stat failed for {key}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise DestinationConnectionError(f"HEAD {key} failed (status {resp.status_code})")
        length = resp.headers.get("Content-Length")
        return int(length) if length is not None else 0

    def delete(self, key: str) -> None:
        self.connect()
        resp = self._session.delete(self._url(key), verify=self.verify_tls, timeout=30)
        if resp.status_code not in (200, 204, 404):
            raise DestinationConnectionError(f"DELETE {key} failed (status {resp.status_code})")

    def list_objects(self, prefix: str = "") -> Iterable[str]:
        self.connect()
        url = self._url(prefix)
        try:
            resp = self._session.request(
                "PROPFIND", url, headers={"Depth": "infinity"}, verify=self.verify_tls, timeout=60
            )
        except requests.RequestException as exc:
            raise DestinationConnectionError(f"WebDAV list failed for {prefix}: {exc}") from exc
        if resp.status_code != 207:
            return
        root = ET.fromstring(resp.content)
        base_path = urlparse(self.base_url).path.rstrip("/")
        root_prefix = f"{base_path}/{self.remote_root}"
        for response_el in root.findall(f"{_DAV_NS}response"):
            href_el = response_el.find(f"{_DAV_NS}href")
            is_collection = response_el.find(f".//{_DAV_NS}collection") is not None
            if href_el is None or is_collection:
                continue
            href = href_el.text or ""
            if href.startswith(root_prefix):
                yield href[len(root_prefix):].lstrip("/")
