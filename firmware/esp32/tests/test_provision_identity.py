from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "provision_identity.py"
PARTITION_SIZE = 0x10000


def _idf_python() -> Path:
    candidates = []
    configured = os.environ.get("MEMORIA_PYTHON_BIN")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        Path.home() / ".espressif" / "python_env" / f"idf6.0_py{version}_env" / "bin" / "python"
        for version in ("3.12", "3.10")
    )
    candidates.append(Path(sys.executable))
    for candidate in candidates:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import esp_idf_nvs_partition_gen"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    pytest.skip("ESP-IDF esp_idf_nvs_partition_gen is not available")


def _inputs(tmp_path: Path) -> tuple[Path, Path, bytes]:
    seed = bytes(range(32))
    seed_path = tmp_path / "seed.bin"
    seed_path.write_bytes(seed)
    seed_path.chmod(0o600)
    activation_path = tmp_path / "activation-pubkey.bin"
    activation_path.write_bytes(bytes(range(32, 64)))
    activation_path.chmod(0o600)
    return seed_path, activation_path, seed


def _command(
    seed_path: Path,
    activation_path: Path,
    output_path: Path,
    *,
    device_id: str = "dev_atk_fixture0001",
    certificate_id: str = "cert_atk_fixture0001",
    client_id: str = "client_atk_fixture0001",
    control_api_url: str = "https://memoria.example.test:8443/api",
    idf_python: Path | None = None,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--seed-in",
        str(seed_path),
        "--activation-pubkey-in",
        str(activation_path),
        "--device-id",
        device_id,
        "--certificate-id",
        certificate_id,
        "--client-id",
        client_id,
        "--control-api-url",
        control_api_url,
        "--output",
        str(output_path),
        *(["--idf-python", str(idf_python)] if idf_python else []),
    ]


def test_real_idf_generator_writes_64k_private_image_without_printing_seed(tmp_path: Path) -> None:
    idf_python = _idf_python()
    seed_path, activation_path, seed = _inputs(tmp_path)
    output_path = tmp_path / "nested" / "identity.bin"

    result = subprocess.run(
        _command(
            seed_path,
            activation_path,
            output_path,
            idf_python=idf_python,
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output_path.stat().st_size == PARTITION_SIZE
    assert output_path.stat().st_mode & 0o777 == 0o600
    combined_output = result.stdout + result.stderr
    assert seed.hex() not in combined_output
    assert not list(output_path.parent.glob("memoria-identity-*"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device_id", 'dev"quoted'),
        ("certificate_id", "cert/slash"),
        ("client_id", "client with space"),
        ("device_id", "-leading-hyphen"),
        ("certificate_id", "a" * 129),
    ],
)
def test_rejects_unsafe_protocol_ids(
    tmp_path: Path, field: str, value: str
) -> None:
    seed_path, activation_path, _ = _inputs(tmp_path)
    output_path = tmp_path / "identity.bin"
    values = {"device_id": "dev_ok", "certificate_id": "cert_ok", "client_id": "client_ok"}
    values[field] = value

    result = subprocess.run(
        _command(
            seed_path,
            activation_path,
            output_path,
            device_id=values["device_id"],
            certificate_id=values["certificate_id"],
            client_id=values["client_id"],
            idf_python=tmp_path / "missing-python",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert field.replace("_", "-") in result.stderr
    assert not output_path.exists()
    assert not list(tmp_path.glob("memoria-identity-*"))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://memoria.example.test/api",
        "https://user:password@memoria.example.test/api",
        "https://memoria.example.test/api?token=secret",
        "https://memoria.example.test/api?",
        "https://memoria.example.test/api#fragment",
        "https://memoria.example.test/api#",
        "https:///api",
        "https://:8443/api",
    ],
)
def test_rejects_unsafe_control_api_urls(tmp_path: Path, url: str) -> None:
    seed_path, activation_path, _ = _inputs(tmp_path)
    output_path = tmp_path / "identity.bin"

    result = subprocess.run(
        _command(
            seed_path,
            activation_path,
            output_path,
            control_api_url=url,
            idf_python=tmp_path / "missing-python",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "control-api-url" in result.stderr
    assert not output_path.exists()
    assert not list(tmp_path.glob("memoria-identity-*"))


def test_rejects_seed_without0600_mode(tmp_path: Path) -> None:
    seed_path, activation_path, _ = _inputs(tmp_path)
    seed_path.chmod(0o644)
    output_path = tmp_path / "identity.bin"

    result = subprocess.run(
        _command(
            seed_path,
            activation_path,
            output_path,
            idf_python=tmp_path / "missing-python",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "mode 0600" in result.stderr
    assert not output_path.exists()
    assert not list(tmp_path.glob("memoria-identity-*"))
