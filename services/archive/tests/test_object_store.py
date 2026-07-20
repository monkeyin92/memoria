from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from services.archive.object_store import (
    EncryptedLocalObjectStore,
    EncryptedS3ObjectStore,
    ObjectOwnershipError,
)


def test_s3_factory_routes_explicit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def client(service: str, **kwargs: object) -> object:
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    EncryptedS3ObjectStore.from_boto3(
        bucket="archive",
        key=Fernet.generate_key().decode("ascii"),
        key_version="archive-v1",
        endpoint_url="http://minio:9000",
        region_name="us-east-1",
        access_key_id="archive-access",
        secret_access_key="archive-secret",
    )

    assert captured == {
        "service": "s3",
        "endpoint_url": "http://minio:9000",
        "region_name": "us-east-1",
        "aws_access_key_id": "archive-access",
        "aws_secret_access_key": "archive-secret",
    }


@pytest.mark.asyncio
async def test_local_object_store_encrypts_verifies_and_deletes_bytes(tmp_path: Path) -> None:
    store = EncryptedLocalObjectStore(
        root=tmp_path / "objects",
        key=Fernet.generate_key().decode("ascii"),
        key_version="test-v1",
    )
    plaintext = b"authorized voice sample bytes"

    reference = await store.put(
        account_id="account-001",
        purpose="voice-profile",
        data=plaintext,
        media_type="audio/wav",
    )

    stored_path = tmp_path / "objects" / reference.object_key
    assert stored_path.is_file()
    assert plaintext not in stored_path.read_bytes()
    assert await store.get(reference) == plaintext
    assert reference.byte_count == len(plaintext)
    assert reference.encryption_key_version == "test-v1"
    await store.delete(reference)
    assert not stored_path.exists()


@pytest.mark.asyncio
async def test_object_reference_cannot_cross_account_ownership_boundary(tmp_path: Path) -> None:
    store = EncryptedLocalObjectStore(
        root=tmp_path / "objects",
        key=Fernet.generate_key().decode("ascii"),
        key_version="test-v1",
    )
    reference = await store.put(
        account_id="account-b",
        purpose="source-audio",
        data=b"belongs to account b",
        media_type="audio/wav",
    )

    with pytest.raises(ObjectOwnershipError):
        await store.delete(replace(reference, account_id="account-a"))

    assert await store.get(reference) == b"belongs to account b"


@pytest.mark.asyncio
async def test_s3_deletion_removes_every_version_and_delete_marker() -> None:
    class VersionedS3Stub:
        def __init__(self) -> None:
            self.key = ""
            self.deleted: list[dict[str, str]] = []

        def put_object(self, **kwargs: object) -> None:
            self.key = str(kwargs["Key"])

        def list_object_versions(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["Prefix"] == self.key
            return {
                "Versions": [
                    {"Key": self.key, "VersionId": "current"},
                    {"Key": self.key, "VersionId": "older"},
                ],
                "DeleteMarkers": [{"Key": self.key, "VersionId": "marker"}],
                "IsTruncated": False,
            }

        def delete_objects(self, **kwargs: object) -> None:
            body = kwargs["Delete"]
            assert isinstance(body, dict)
            self.deleted.extend(body["Objects"])  # type: ignore[arg-type]

    client = VersionedS3Stub()
    store = EncryptedS3ObjectStore(
        client=client,
        bucket="archive",
        key=Fernet.generate_key().decode("ascii"),
        key_version="archive-v1",
    )
    reference = await store.put(
        account_id="account-a",
        purpose="source-audio",
        data=b"versioned",
        media_type="audio/wav",
    )

    await store.delete(reference)

    assert client.deleted == [
        {"Key": reference.object_key, "VersionId": "current"},
        {"Key": reference.object_key, "VersionId": "older"},
        {"Key": reference.object_key, "VersionId": "marker"},
    ]
