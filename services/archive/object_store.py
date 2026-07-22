"""Client-encrypted object adapters for audio, images, documents, and exports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

_PURPOSE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class ObjectIntegrityError(RuntimeError):
    pass


class ObjectOwnershipError(ObjectIntegrityError):
    pass


@dataclass(frozen=True, slots=True)
class ObjectRef:
    account_id: str
    object_key: str
    media_type: str
    byte_count: int
    content_sha256: str
    encryption_key_version: str
    backend: str


class ObjectStore(Protocol):
    async def put(
        self,
        *,
        account_id: str,
        purpose: str,
        data: bytes,
        media_type: str,
    ) -> ObjectRef: ...

    async def get(self, reference: ObjectRef) -> bytes: ...

    async def delete(self, reference: ObjectRef) -> None: ...


def _object_key(account_id: str, purpose: str, prefix: str = "") -> str:
    if not account_id.strip():
        raise ValueError("account_id must not be blank")
    if not _PURPOSE.fullmatch(purpose):
        raise ValueError("purpose must be a lowercase slug")
    account_hash = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:24]
    name = f"{account_hash}/{purpose}/{uuid.uuid4()}.fernet"
    return f"{prefix.strip('/')}/{name}" if prefix.strip("/") else name


def _validate_owner(reference: ObjectRef) -> None:
    account_hash = hashlib.sha256(reference.account_id.encode("utf-8")).hexdigest()[:24]
    if not reference.account_id.strip() or account_hash not in reference.object_key.split("/"):
        raise ObjectOwnershipError("object reference does not belong to its account")


def _decrypt_and_verify(fernet: Fernet, encrypted: bytes, reference: ObjectRef) -> bytes:
    try:
        plaintext = fernet.decrypt(encrypted)
    except InvalidToken as exc:
        raise ObjectIntegrityError("object ciphertext failed authentication") from exc
    digest = hashlib.sha256(plaintext).hexdigest()
    if len(plaintext) != reference.byte_count or digest != reference.content_sha256:
        raise ObjectIntegrityError("object plaintext does not match its manifest")
    return plaintext


def _keyring(
    *,
    key: str,
    key_version: str,
    read_keys: Mapping[str, str] | None,
) -> tuple[Fernet, dict[str, Fernet]]:
    if not key_version.strip():
        raise ValueError("key_version must not be blank")
    raw_keys = dict(read_keys or {})
    if key_version in raw_keys:
        raise ValueError("read_keys must not include active key version")
    if any(not isinstance(version, str) or not version.strip() for version in raw_keys):
        raise ValueError("read key versions must not be blank")
    try:
        encoded_keys = [
            key.encode("ascii"),
            *(value.encode("ascii") for value in raw_keys.values()),
        ]
        fernets = [Fernet(encoded) for encoded in encoded_keys]
        materials = [base64.urlsafe_b64decode(encoded) for encoded in encoded_keys]
    except (AttributeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("object encryption keys must be valid Fernet keys") from exc
    if len(materials) != len(set(materials)):
        raise ValueError("active and read-only key material must be unique")
    active, *read_fernets = fernets
    return active, dict(zip(raw_keys, read_fernets, strict=True))


def _fernet_for_version(
    *,
    active: Fernet,
    active_version: str,
    read_keys: Mapping[str, Fernet],
    version: str,
) -> Fernet:
    if version == active_version:
        return active
    try:
        return read_keys[version]
    except KeyError as exc:
        raise ObjectIntegrityError("unknown encryption key version") from exc


class EncryptedLocalObjectStore:
    def __init__(
        self,
        *,
        root: Path,
        key: str,
        key_version: str,
        read_keys: Mapping[str, str] | None = None,
    ) -> None:
        self._root = root.expanduser().resolve()
        self._key_version = key_version
        self._fernet, self._read_fernets = _keyring(
            key=key,
            key_version=key_version,
            read_keys=read_keys,
        )

    async def put(
        self,
        *,
        account_id: str,
        purpose: str,
        data: bytes,
        media_type: str,
    ) -> ObjectRef:
        if not media_type.strip():
            raise ValueError("media_type must not be blank")
        object_key = _object_key(account_id, purpose)
        reference = ObjectRef(
            account_id=account_id,
            object_key=object_key,
            media_type=media_type,
            byte_count=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(),
            encryption_key_version=self._key_version,
            backend="local",
        )
        destination = self._path(reference)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        encrypted = self._fernet.encrypt(data)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, encrypted)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        return reference

    async def get(self, reference: ObjectRef) -> bytes:
        _validate_owner(reference)
        return _decrypt_and_verify(
            _fernet_for_version(
                active=self._fernet,
                active_version=self._key_version,
                read_keys=self._read_fernets,
                version=reference.encryption_key_version,
            ),
            self._path(reference).read_bytes(),
            reference,
        )

    async def delete(self, reference: ObjectRef) -> None:
        _validate_owner(reference)
        self._path(reference).unlink(missing_ok=True)

    def _path(self, reference: ObjectRef) -> Path:
        path = (self._root / reference.object_key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError("object key escapes configured root")
        return path


class EncryptedS3ObjectStore:
    """S3-compatible adapter; works with AWS S3 or an OSS S3 endpoint."""

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        key: str,
        key_version: str,
        read_keys: Mapping[str, str] | None = None,
        prefix: str = "memoria",
    ) -> None:
        if not bucket.strip():
            raise ValueError("bucket and key_version must not be blank")
        self._client = client
        self._bucket = bucket
        self._key_version = key_version
        self._fernet, self._read_fernets = _keyring(
            key=key,
            key_version=key_version,
            read_keys=read_keys,
        )
        self._prefix = prefix

    @classmethod
    def from_boto3(
        cls,
        *,
        bucket: str,
        key: str,
        key_version: str,
        read_keys: Mapping[str, str] | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        prefix: str = "memoria",
    ) -> EncryptedS3ObjectStore:
        import boto3

        client_kwargs: dict[str, str] = {}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if region_name:
            client_kwargs["region_name"] = region_name
        if access_key_id:
            client_kwargs["aws_access_key_id"] = access_key_id
        if secret_access_key:
            client_kwargs["aws_secret_access_key"] = secret_access_key
        client = boto3.client("s3", **client_kwargs)
        return cls(
            client=client,
            bucket=bucket,
            key=key,
            key_version=key_version,
            read_keys=read_keys,
            prefix=prefix,
        )

    async def put(
        self,
        *,
        account_id: str,
        purpose: str,
        data: bytes,
        media_type: str,
    ) -> ObjectRef:
        object_key = _object_key(account_id, purpose, self._prefix)
        reference = ObjectRef(
            account_id=account_id,
            object_key=object_key,
            media_type=media_type,
            byte_count=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(),
            encryption_key_version=self._key_version,
            backend="s3",
        )
        encrypted = self._fernet.encrypt(data)
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=object_key,
            Body=encrypted,
            ContentType="application/octet-stream",
            Metadata={
                "plain-sha256": reference.content_sha256,
                "plain-bytes": str(reference.byte_count),
                "media-type": media_type,
                "key-version": self._key_version,
            },
        )
        return reference

    async def get(self, reference: ObjectRef) -> bytes:
        _validate_owner(reference)
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket,
            Key=reference.object_key,
        )
        encrypted = await asyncio.to_thread(response["Body"].read)
        return _decrypt_and_verify(
            _fernet_for_version(
                active=self._fernet,
                active_version=self._key_version,
                read_keys=self._read_fernets,
                version=reference.encryption_key_version,
            ),
            encrypted,
            reference,
        )

    async def delete(self, reference: ObjectRef) -> None:
        _validate_owner(reference)
        await asyncio.to_thread(self._delete_all_versions, reference.object_key)

    def _delete_all_versions(self, object_key: str) -> None:
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": object_key,
        }
        versions: list[dict[str, str]] = []
        while True:
            response = self._client.list_object_versions(**request)
            for group in ("Versions", "DeleteMarkers"):
                for item in response.get(group, []):
                    if item.get("Key") == object_key and item.get("VersionId") is not None:
                        versions.append({"Key": object_key, "VersionId": str(item["VersionId"])})
            if not response.get("IsTruncated"):
                break
            request["KeyMarker"] = response["NextKeyMarker"]
            request["VersionIdMarker"] = response["NextVersionIdMarker"]
        if versions:
            self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": versions, "Quiet": True},
            )
        else:
            self._client.delete_object(Bucket=self._bucket, Key=object_key)
