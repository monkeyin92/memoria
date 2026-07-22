from __future__ import annotations

import io
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken
from services.archive.object_store import (
    EncryptedLocalObjectStore,
    EncryptedS3ObjectStore,
    ObjectIntegrityError,
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
async def test_local_key_rotation_reads_old_reference_and_writes_active_key(tmp_path: Path) -> None:
    old_key = Fernet.generate_key().decode("ascii")
    active_key = Fernet.generate_key().decode("ascii")
    root = tmp_path / "objects"
    old_store = EncryptedLocalObjectStore(root=root, key=old_key, key_version="v1")
    old_reference = await old_store.put(
        account_id="account-001",
        purpose="source-audio",
        data=b"old archive audio",
        media_type="audio/wav",
    )
    rotated = EncryptedLocalObjectStore(
        root=root,
        key=active_key,
        key_version="v2",
        read_keys={"v1": old_key},
    )

    assert await rotated.get(old_reference) == b"old archive audio"
    active_reference = await rotated.put(
        account_id="account-001",
        purpose="source-audio",
        data=b"new archive audio",
        media_type="audio/wav",
    )
    assert active_reference.encryption_key_version == "v2"
    active_ciphertext = (root / active_reference.object_key).read_bytes()
    with pytest.raises(InvalidToken):
        Fernet(old_key).decrypt(active_ciphertext)
    with pytest.raises(ObjectIntegrityError, match="unknown encryption key version"):
        await rotated.get(replace(old_reference, encryption_key_version="retired"))


@pytest.mark.parametrize(
    "read_keys",
    [
        lambda active: {"v1": active},
        lambda _active: {
            "v1": (shared := Fernet.generate_key().decode("ascii")),
            "v0": shared,
        },
    ],
)
def test_keyring_rejects_duplicate_key_material(
    tmp_path: Path,
    read_keys: object,
) -> None:
    active = Fernet.generate_key().decode("ascii")

    with pytest.raises(ValueError, match="key material must be unique"):
        EncryptedLocalObjectStore(
            root=tmp_path / "objects",
            key=active,
            key_version="v2",
            read_keys=read_keys(active),  # type: ignore[operator]
        )


@pytest.mark.asyncio
async def test_s3_key_rotation_reads_old_reference_and_writes_active_key() -> None:
    class S3Stub:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put_object(self, **kwargs: object) -> None:
            self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])

        def get_object(self, **kwargs: object) -> dict[str, io.BytesIO]:
            return {"Body": io.BytesIO(self.objects[str(kwargs["Key"])])}

    old_key = Fernet.generate_key().decode("ascii")
    active_key = Fernet.generate_key().decode("ascii")
    client = S3Stub()
    old_store = EncryptedS3ObjectStore(
        client=client,
        bucket="archive",
        key=old_key,
        key_version="v1",
    )
    old_reference = await old_store.put(
        account_id="account-001",
        purpose="source-audio",
        data=b"old s3 archive audio",
        media_type="audio/wav",
    )
    rotated = EncryptedS3ObjectStore(
        client=client,
        bucket="archive",
        key=active_key,
        key_version="v2",
        read_keys={"v1": old_key},
    )

    assert await rotated.get(old_reference) == b"old s3 archive audio"
    active_reference = await rotated.put(
        account_id="account-001",
        purpose="source-audio",
        data=b"new s3 archive audio",
        media_type="audio/wav",
    )
    assert active_reference.encryption_key_version == "v2"
    with pytest.raises(ObjectIntegrityError, match="unknown encryption key version"):
        await rotated.get(replace(old_reference, encryption_key_version="retired"))


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
