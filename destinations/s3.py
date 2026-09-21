"""
destinations.s3
================

Generic S3-compatible object storage destination. Because ``boto3``
accepts an arbitrary ``endpoint_url``, this single backend covers AWS S3
itself as well as any self-hosted S3-compatible service -- MinIO being the
most common self-hosted target, but also TrueNAS's built-in S3 service,
Ceph RGW, Garage, SeaweedFS, etc. We never hardcode an AWS-specific
endpoint or behavior.

Requires the ``boto3`` package.
"""

from __future__ import annotations

from typing import Iterable, Optional

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover
    _BOTO3_AVAILABLE = False

from .base import (
    Destination,
    DestinationConnectionError,
    DestinationNotFoundError,
)


class S3Destination(Destination):
    kind = "s3"

    def __init__(
        self,
        bucket: str,
        access_key: str,
        secret_key: str,
        endpoint_url: Optional[str] = None,
        region: str = "us-east-1",
        prefix: str = "backupsys",
        use_ssl: bool = True,
    ):
        if not _BOTO3_AVAILABLE:
            raise ImportError(
                "The 'boto3' package is required for S3-compatible destinations. "
                "Install it with: pip install boto3"
            )
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint_url = endpoint_url  # None => real AWS S3
        self.region = region
        self.prefix = prefix.strip("/")
        self.use_ssl = use_ssl
        self._client = None

    # ------------------------------------------------------------------
    def connect(self) -> None:
        if self._client is not None:
            return
        try:
            self._client = boto3.client(
                "s3",
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                endpoint_url=self.endpoint_url,
                region_name=self.region,
                use_ssl=self.use_ssl,
            )
            # Bucket creation is best-effort: many self-hosted setups
            # pre-provision buckets with specific policies, so a failure
            # here (e.g. AccessDenied on CreateBucket but fine on
            # PutObject) is not fatal -- we only surface it if the bucket
            # genuinely doesn't exist and can't be created.
            try:
                self._client.head_bucket(Bucket=self.bucket)
            except ClientError:
                try:
                    self._client.create_bucket(Bucket=self.bucket)
                except ClientError as exc:
                    raise DestinationConnectionError(
                        f"Bucket {self.bucket!r} doesn't exist and couldn't be created: {exc}"
                    ) from exc
        except (BotoCoreError, ClientError) as exc:
            raise DestinationConnectionError(f"S3 connect failed: {exc}") from exc

    def disconnect(self) -> None:
        self._client = None  # boto3 clients don't need explicit closing

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            return True, f"OK: bucket {self.bucket!r} reachable" + (
                f" via {self.endpoint_url}" if self.endpoint_url else " (AWS S3)"
            )
        except DestinationConnectionError as exc:
            return False, str(exc)

    # ------------------------------------------------------------------
    def _key_for(self, key: str) -> str:
        parts = [p for p in key.split("/") if p not in ("", ".", "..")]
        return "/".join([self.prefix, *parts]) if parts else self.prefix

    def put_object(self, key: str, data: bytes) -> None:
        self.connect()
        try:
            self._client.put_object(Bucket=self.bucket, Key=self._key_for(key), Body=data)
        except (BotoCoreError, ClientError) as exc:
            raise DestinationConnectionError(f"S3 PUT failed for {key}: {exc}") from exc

    def get_object(self, key: str) -> bytes:
        self.connect()
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._key_for(key))
            return resp["Body"].read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise DestinationNotFoundError(key) from exc
            raise DestinationConnectionError(f"S3 GET failed for {key}: {exc}") from exc
        except BotoCoreError as exc:
            raise DestinationConnectionError(f"S3 GET failed for {key}: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self.stat(key) is not None

    def stat(self, key: str) -> Optional[int]:
        self.connect()
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=self._key_for(key))
            return resp["ContentLength"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey"):
                return None
            raise DestinationConnectionError(f"S3 HEAD failed for {key}: {exc}") from exc
        except BotoCoreError as exc:
            raise DestinationConnectionError(f"S3 HEAD failed for {key}: {exc}") from exc

    def delete(self, key: str) -> None:
        self.connect()
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._key_for(key))
        except (BotoCoreError, ClientError) as exc:
            raise DestinationConnectionError(f"S3 DELETE failed for {key}: {exc}") from exc

    def list_objects(self, prefix: str = "") -> Iterable[str]:
        self.connect()
        full_prefix = self._key_for(prefix) if prefix else self.prefix
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
                for obj in page.get("Contents", []):
                    yield obj["Key"][len(self.prefix):].lstrip("/")
        except (BotoCoreError, ClientError) as exc:
            raise DestinationConnectionError(f"S3 list failed: {exc}") from exc
