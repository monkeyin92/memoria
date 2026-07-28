"""Import EchoLife WeChat identities and profile fields without fabricating conversations."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

from services.control_api.app.database import ExternalIdentityConflictError, MemoryStore

_HASH_RE = re.compile(r"^[0-9a-f]{24}$")
_SENSITIVE_QUERY_NAMES = {
    "access_token",
    "apikey",
    "api_key",
    "key",
    "secret",
    "sig",
    "signature",
    "token",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_avatar_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or candidate.startswith(("wxfile:", "file:", "data:")):
        return ""
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return ""
    query_names = {name.casefold() for name, _ in parse_qsl(parsed.query)}
    return "" if query_names & _SENSITIVE_QUERY_NAMES else candidate


def _avatar_content(
    value: object,
    *,
    avatar_root: Path | None,
) -> tuple[str, bytes] | None:
    if avatar_root is None or not isinstance(value, str):
        return None
    parsed = urlsplit(value.strip())
    filename = Path(parsed.path or parsed.netloc).name
    if not filename:
        return None
    path = avatar_root / filename
    try:
        content = path.read_bytes()
    except OSError:
        return None
    if not content or len(content) > 2 * 1024 * 1024:
        return None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", content
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", content
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", content
    return None


def _legacy_content_present(payload: dict[str, Any]) -> bool:
    return bool(payload.get("storyFragments") or payload.get("timeline"))


def _identity_from_session(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    auth = payload.get("auth")
    if not isinstance(auth, dict) or auth.get("status") != "authenticated":
        return None
    user = auth.get("user")
    if not isinstance(user, dict):
        return None
    subject_hash = str(auth.get("wechatOpenidHash") or "").strip()
    source_user_id = str(user.get("id") or "").strip()
    if not subject_hash and source_user_id.startswith("wx_"):
        subject_hash = source_user_id[3:]
    if not _HASH_RE.fullmatch(subject_hash):
        return None
    expected_user_id = f"wx_{subject_hash}"
    if source_user_id and source_user_id != expected_user_id:
        raise ValueError("EchoLife user id does not match wechatOpenidHash")
    return subject_hash, expected_user_id, user


def _sqlite_payloads(path: Path) -> tuple[list[dict[str, Any]], bool]:
    payloads: list[dict[str, Any]] = []
    legacy_content = False
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "app_state" in tables:
            for row in connection.execute("SELECT value FROM app_state"):
                try:
                    payload = json.loads(str(row["value"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    payloads.append(payload)
        if "users" in tables:
            columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(users)")
            }
            if {"id", "nickname", "avatar_url"} <= columns:
                open_id = "open_id" if "open_id" in columns else "''"
                for row in connection.execute(
                    f"SELECT id, {open_id} AS open_id, nickname, avatar_url FROM users"
                ):
                    source_user_id = str(row["id"] or "").strip()
                    raw_openid = str(row["open_id"] or "").strip()
                    subject_hash = (
                        hashlib.sha256(raw_openid.encode("utf-8")).hexdigest()[:24]
                        if raw_openid
                        else source_user_id.removeprefix("wx_")
                        if source_user_id.startswith("wx_")
                        else ""
                    )
                    if not _HASH_RE.fullmatch(subject_hash):
                        continue
                    payloads.append(
                        {
                            "auth": {
                                "status": "authenticated",
                                "wechatOpenidHash": subject_hash,
                                "user": {
                                    "id": f"wx_{subject_hash}",
                                    "nickname": str(row["nickname"] or ""),
                                    "avatarUrl": str(row["avatar_url"] or ""),
                                },
                            }
                        }
                    )
        legacy_tables = ("story_fragments", "timeline_events", "interview_messages")
        legacy_content = any(
            table in tables
            and int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) > 0
            for table in legacy_tables
        )
    return payloads, legacy_content


def _source_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    patterns = (
        "*.json.backup-*",
        "*.json",
        "sessions/*.json",
        "*.sqlite",
        "*.sqlite3",
        "*.db",
    )
    return sorted(
        {path.resolve() for pattern in patterns for path in source.glob(pattern)},
        key=lambda path: (
            path.suffix.lower() in {".sqlite", ".sqlite3", ".db"},
            path.name,
        ),
    )


def _collect_candidates(
    source: Path,
) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, int]]:
    candidates: dict[str, tuple[str, dict[str, Any]]] = {}
    metrics = {
        "files_scanned": 0,
        "conflicts": 0,
        "legacy_content_deferred": 0,
    }
    for path in _source_files(source):
        metrics["files_scanned"] += 1
        try:
            if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                payloads, sqlite_legacy = _sqlite_payloads(path)
                metrics["legacy_content_deferred"] += int(sqlite_legacy)
            else:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payloads = [payload] if isinstance(payload, dict) else []
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
            metrics["conflicts"] += 1
            continue
        for payload in payloads:
            metrics["legacy_content_deferred"] += int(_legacy_content_present(payload))
            try:
                identity = _identity_from_session(payload)
            except ValueError:
                metrics["conflicts"] += 1
                continue
            if identity is None:
                continue
            subject_hash, expected_user_id, user = identity
            previous = candidates.get(subject_hash)
            merged_user = dict(previous[1]) if previous is not None else {}
            merged_user.update(
                {
                    key: value
                    for key, value in user.items()
                    if value is not None and str(value).strip()
                }
            )
            candidates[subject_hash] = expected_user_id, merged_user
    return candidates, metrics


def _migrate_into(
    source: Path,
    target_db: Path,
    *,
    avatar_root: Path | None,
    public_base_url: str | None,
) -> dict[str, int]:
    store = MemoryStore(str(target_db))
    store.initialize()
    candidates, source_metrics = _collect_candidates(source)
    result = {
        **source_metrics,
        "users_discovered": len(candidates),
        "users_imported": 0,
        "users_unchanged": 0,
        "avatars_imported": 0,
        "avatars_deferred": 0,
    }
    for subject_hash, (expected_user_id, user) in sorted(candidates.items()):
        try:
            existing_user_id = store.external_identity_user(
                provider="wechat_openid",
                subject_hash=subject_hash,
            )
            if existing_user_id is not None and existing_user_id != expected_user_id:
                result["conflicts"] += 1
                continue
            if existing_user_id is None and store.is_account_unavailable(
                user_id=expected_user_id
            ):
                result["conflicts"] += 1
                continue
            user_id, identity_changed = store.bind_external_identities(
                preferred_user_id=expected_user_id,
                identities={"wechat_openid": subject_hash},
                now=_now(),
            )
            before = store.get_profile(user_id=user_id, now=_now())
            display_name = str(
                user.get("nickname")
                or user.get("wechatName")
                or user.get("displayName")
                or ""
            ).strip()
            phone_masked = str(user.get("phoneNumberMasked") or "").strip()
            profile = store.update_external_profile(
                user_id=user_id,
                display_name=display_name or None,
                phone_number_masked=phone_masked or None,
                now=_now(),
            )
            avatar_changed = False
            source_avatar = user.get("avatarUrl")
            local_avatar = _avatar_content(source_avatar, avatar_root=avatar_root)
            if local_avatar is not None and public_base_url:
                content_type, content = local_avatar
                digest = hashlib.sha256(content).hexdigest()
                public_id = hashlib.sha256(
                    f"echolife-avatar-v1:{subject_hash}:{digest}".encode()
                ).hexdigest()[:40]
                avatar_url = (
                    f"{public_base_url.rstrip('/')}/v1/auth/wechat-avatars/{public_id}"
                )
                existing_avatar = store.get_profile_avatar(public_id=public_id)
                if (
                    existing_avatar is None
                    or str(existing_avatar["sha256"]) != digest
                    or profile["avatar_url"] != avatar_url
                ):
                    store.save_profile_avatar(
                        user_id=user_id,
                        public_id=public_id,
                        content_type=content_type,
                        content=content,
                        sha256=digest,
                        avatar_url=avatar_url,
                        now=_now(),
                    )
                    profile = store.get_profile(user_id=user_id, now=_now())
                    avatar_changed = True
                    result["avatars_imported"] += 1
            else:
                avatar_url = _safe_avatar_url(source_avatar)
                if avatar_url and avatar_url != profile["avatar_url"]:
                    profile = store.update_profile(
                        user_id=user_id,
                        values={"avatar_url": avatar_url},
                        now=_now(),
                    )
                    avatar_changed = True
                elif source_avatar and not avatar_url:
                    result["avatars_deferred"] += 1
            if not avatar_changed and local_avatar is not None and not public_base_url:
                result["avatars_deferred"] += 1
            changed = identity_changed or avatar_changed or any(
                before.get(key) != profile.get(key)
                for key in ("display_name", "phone_number_masked")
            )
            result["users_imported" if changed else "users_unchanged"] += 1
        except (ExternalIdentityConflictError, OSError, ValueError, sqlite3.Error):
            result["conflicts"] += 1
    return result


def _validated_public_base_url(value: str | None) -> str | None:
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public base URL must be an absolute HTTP(S) URL")
    return candidate


def _resolved_avatar_root(value: Path | None) -> Path | None:
    if value is None:
        return None
    root = value.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"EchoLife avatar directory not found: {root}")
    return root


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection, sqlite3.connect(
        target
    ) as target_connection:
        source_connection.backup(target_connection)
    target.chmod(0o600)


def migrate_echolife_users(
    *,
    source: Path,
    target_db: Path,
    dry_run: bool,
    avatar_root: Path | None = None,
    public_base_url: str | None = None,
) -> dict[str, int]:
    resolved_source = source.expanduser().resolve()
    target = target_db.expanduser().resolve()
    if not resolved_source.exists():
        raise ValueError(f"EchoLife source not found: {resolved_source}")
    resolved_avatar_root = _resolved_avatar_root(avatar_root)
    resolved_public_base_url = _validated_public_base_url(public_base_url)
    if resolved_avatar_root is not None and resolved_public_base_url is None:
        raise ValueError("--public-base-url is required with --avatar-root")
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="memoria-echolife-dry-run-") as temp:
            candidate = Path(temp) / target.name
            if target.is_file():
                _backup_sqlite(target, candidate)
            return _migrate_into(
                resolved_source,
                candidate,
                avatar_root=resolved_avatar_root,
                public_base_url=resolved_public_base_url,
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        _backup_sqlite(
            target,
            target.with_name(f"{target.name}.pre-echolife-{timestamp}.bak"),
        )
    return _migrate_into(
        resolved_source,
        target,
        avatar_root=resolved_avatar_root,
        public_base_url=resolved_public_base_url,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", "--source-dir", dest="source", type=Path, required=True)
    parser.add_argument("--target-db", type=Path, required=True)
    parser.add_argument("--avatar-root", type=Path)
    parser.add_argument("--public-base-url")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = migrate_echolife_users(
            source=args.source,
            target_db=args.target_db,
            dry_run=args.dry_run,
            avatar_root=args.avatar_root,
            public_base_url=args.public_base_url,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
