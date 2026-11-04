#!/usr/bin/env python3
"""Capture, validate, render, and compare a Memoria runtime component inventory."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
EVIDENCE_CLASSES = frozenset(
    {"live_capture", "lab_fixture", "handoff_reconstruction"}
)
UNKNOWN = "unknown"

INSPECT_FORMATS: dict[str, str] = {
    "chain": '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
    "version": '{{index .Config.Labels "org.opencontainers.image.version"}}',
    "revision": '{{index .Config.Labels "org.opencontainers.image.revision"}}',
    "image": "{{.Config.Image}}",
    "image-id": "{{.Image}}",
    "release-tag": (
        '{{range .Config.Env}}{{if and (ge (len .) 20) '
        '(eq (slice . 0 20) "MEMORIA_RELEASE_TAG=")}}{{slice . 20}}{{end}}{{end}}'
    ),
    "running": "{{.State.Running}}",
}

# Allowlisted env keys only. Captured via per-key templates, never a full Env dump.
FLAG_ALLOWLIST: tuple[str, ...] = (
    "MEMORIA_RELEASE_TAG",
    "MEMORIA_RELEASE_COMMIT",
    "TTS_PROVIDER",
    "STEPFUN_TTS_ENABLED",
    "STEPFUN_TTS_VOICE",
    "MEMORIA_SPEAKER_AUTHORITY_ENABLED",
)

ROLE_BY_IMAGE_PREFIX: tuple[tuple[str, str], ...] = (
    ("memoria-control-api", "control-api"),
    ("memoria-agent", "voice-core-media-bridge"),
    ("memoria-media-edge", "media-edge"),
)

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# Docker .Image is sha256:<64 hex>. Short HANDOFF prefixes are not image ids.
_IMAGE_ID = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")

def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def inspect_format(field: str) -> str:
    if field not in INSPECT_FORMATS:
        raise KeyError(f"unknown inspect field: {field}")
    return INSPECT_FORMATS[field]


def flag_env_format(key: str) -> str:
    if key not in FLAG_ALLOWLIST:
        raise KeyError(f"env key not allowlisted: {key}")
    prefix = f"{key}="
    n = len(prefix)
    return (
        f'{{{{range .Config.Env}}}}'
        f'{{{{if and (ge (len .) {n}) (eq (slice . 0 {n}) "{prefix}")}}}}'
        f'{{{{slice . {n}}}}}'
        f'{{{{end}}}}{{{{end}}}}'
    )


def role_for_image(image: str) -> str:
    name = image.rsplit("/", 1)[-1]
    repo = name.split(":", 1)[0]
    for prefix, role in ROLE_BY_IMAGE_PREFIX:
        if repo == prefix:
            return role
    return "other"


class DockerBackend:
    def list_memoria_containers(self) -> list[str]:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}"],
            text=True,
        )
        return sorted(n for n in out.splitlines() if n.startswith("memoria"))

    def inspect_field(self, container: str, field: str) -> str:
        fmt = inspect_format(field)
        return subprocess.check_output(
            ["docker", "inspect", container, "--format", fmt],
            text=True,
        ).rstrip("\n")

    def inspect_started_at(self, container: str) -> str:
        return subprocess.check_output(
            ["docker", "inspect", container, "--format", "{{.State.StartedAt}}"],
            text=True,
        ).rstrip("\n")

    def inspect_health(self, container: str) -> str:
        fmt = (
            "{{if .State.Health}}{{.State.Health.Status}}"
            "{{else}}no-healthcheck{{end}}"
        )
        return subprocess.check_output(
            ["docker", "inspect", container, "--format", fmt],
            text=True,
        ).rstrip("\n")

    def inspect_restarts(self, container: str) -> int:
        raw = subprocess.check_output(
            ["docker", "inspect", container, "--format", "{{.RestartCount}}"],
            text=True,
        ).rstrip("\n")
        return int(raw)

    def inspect_repo_digest(self, container: str) -> str:
        fmt = '{{index .RepoDigests 0}}'
        try:
            raw = subprocess.check_output(
                ["docker", "inspect", container, "--format", fmt],
                text=True,
                stderr=subprocess.DEVNULL,
            ).rstrip("\n")
        except subprocess.CalledProcessError:
            return UNKNOWN
        return raw if raw and raw != "<no value>" else UNKNOWN

    def inspect_flags(self, container: str) -> dict[str, str]:
        flags: dict[str, str] = {}
        for key in FLAG_ALLOWLIST:
            fmt = flag_env_format(key)
            raw = subprocess.check_output(
                ["docker", "inspect", container, "--format", fmt],
                text=True,
            ).rstrip("\n")
            flags[key] = raw if raw else UNKNOWN
        return flags


Backend = Any


def parse_prev_release(text: str) -> dict[str, Any]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    result: dict[str, Any] = {
        "tag": UNKNOWN,
        "commit": UNKNOWN,
        "services": [],
        "overrides": [],
    }
    if not lines:
        return result
    for line in lines:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "tag":
            result["tag"] = value or UNKNOWN
        elif key == "commit":
            result["commit"] = value if _HEX40.match(value or "") else UNKNOWN
        elif key == "services":
            result["services"] = [s for s in value.split() if s]
        elif key == "overrides":
            parts = value.split()
            groups = []
            i = 0
            while i + 3 < len(parts):
                service, path, sha256, image_id = parts[i : i + 4]
                groups.append(
                    {
                        "service": service,
                        "path": path,
                        "sha256": sha256 if _HEX64.match(sha256) else UNKNOWN,
                        "image_id": image_id,
                    }
                )
                i += 4
            result["overrides"] = groups
    return result


def read_release_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in {"MEMORIA_RELEASE_TAG", "MEMORIA_RELEASE_COMMIT"}:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _or_unknown(value: str | None) -> str:
    if value is None or value == "" or value == "<no value>":
        return UNKNOWN
    return value


def build_inventory(
    *,
    evidence_class: str,
    observations: Sequence[Mapping[str, Any]],
    stack: Mapping[str, Any] | None = None,
    rollback: Mapping[str, Any] | None = None,
    firmware: Mapping[str, Any] | None = None,
    miniprogram: Mapping[str, Any] | None = None,
    captured_at: str | None = None,
    memoria_root: str | None = None,
    extra_unknowns: Sequence[str] | None = None,
) -> dict[str, Any]:
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError(f"invalid evidence_class: {evidence_class}")

    unknowns: list[str] = list(extra_unknowns or [])
    components: list[dict[str, Any]] = []

    for obs in observations:
        role = str(obs.get("role") or role_for_image(str(obs.get("image") or "")))
        image_ref = _or_unknown(obs.get("image") if isinstance(obs.get("image"), str) else None)
        image_id = _or_unknown(
            obs.get("image_id") if isinstance(obs.get("image_id"), str) else None
        )
        registry_digest = _or_unknown(
            obs.get("registry_digest")
            if isinstance(obs.get("registry_digest"), str)
            else None
        )
        revision = obs.get("revision")
        if isinstance(revision, str) and _HEX40.match(revision):
            image_revision = revision
        else:
            image_revision = UNKNOWN
            if revision not in (None, "", UNKNOWN):
                unknowns.append(
                    f"{role}: OCI revision not a 40-hex commit (got {revision!r}); "
                    "not inferred from image name"
                )
            elif image_ref != UNKNOWN:
                unknowns.append(
                    f"{role}: image_revision unknown; not inferred from image name {image_ref}"
                )

        stack_commit = obs.get("stack_commit")
        if isinstance(stack_commit, str) and _HEX40.match(stack_commit):
            stack_commit_v = stack_commit
        else:
            stack_commit_v = UNKNOWN
            if role in {"control-api", "voice-core-media-bridge", "media-edge"}:
                unknowns.append(f"{role}: stack_commit unknown")

        flags_raw = obs.get("effective_flags") or {}
        flags: dict[str, str] = {}
        if isinstance(flags_raw, Mapping):
            for key in FLAG_ALLOWLIST:
                val = flags_raw.get(key, UNKNOWN)
                flags[key] = _or_unknown(val if isinstance(val, str) else None)
                if flags[key] == UNKNOWN:
                    unknowns.append(f"{role}: flag {key} unknown")

        status = obs.get("status") if isinstance(obs.get("status"), Mapping) else {}
        component = {
            "role": role,
            "container": _or_unknown(
                obs.get("container") if isinstance(obs.get("container"), str) else None
            ),
            "image_ref": image_ref,
            "image_id": image_id,
            "registry_digest": registry_digest,
            "image_revision": image_revision,
            "stack_commit": stack_commit_v,
            "compose_chain": _or_unknown(
                obs.get("compose_chain")
                if isinstance(obs.get("compose_chain"), str)
                else None
            ),
            "override_path": _or_unknown(
                obs.get("override_path")
                if isinstance(obs.get("override_path"), str)
                else None
            ),
            "started_at": _or_unknown(
                obs.get("started_at") if isinstance(obs.get("started_at"), str) else None
            ),
            "health": _or_unknown(
                obs.get("health") if isinstance(obs.get("health"), str) else None
            ),
            "restarts": obs.get("restarts")
            if isinstance(obs.get("restarts"), int)
            else UNKNOWN,
            "effective_flags": flags,
            "status": {
                "code": status.get("code", UNKNOWN),
                "wired": status.get("wired", UNKNOWN),
                "enabled": status.get("enabled", UNKNOWN),
                "verified": status.get("verified", UNKNOWN),
                "evidence_dates": status.get("evidence_dates")
                if isinstance(status.get("evidence_dates"), Mapping)
                else {},
            },
        }
        if image_id == UNKNOWN:
            unknowns.append(f"{role}: image_id unknown")
        if registry_digest == UNKNOWN:
            unknowns.append(f"{role}: registry_digest unknown")
        components.append(component)

    stack_out: dict[str, Any] = {
        "current_tag": UNKNOWN,
        "current_commit": UNKNOWN,
        "current_path": UNKNOWN,
    }
    if stack:
        for key in stack_out:
            val = stack.get(key)
            stack_out[key] = _or_unknown(val if isinstance(val, str) else None)
            if stack_out[key] == UNKNOWN:
                unknowns.append(f"stack.{key} unknown")
        if stack_out["current_commit"] != UNKNOWN and not _HEX40.match(
            stack_out["current_commit"]
        ):
            unknowns.append("stack.current_commit not 40-hex; cleared to unknown")
            stack_out["current_commit"] = UNKNOWN

    rollback_out: dict[str, Any] = {
        "stack_prev": {
            "tag": UNKNOWN,
            "commit": UNKNOWN,
            "services": [],
            "overrides": [],
        },
        "per_component": [],
    }
    if rollback:
        sp = rollback.get("stack_prev")
        if isinstance(sp, Mapping):
            rollback_out["stack_prev"] = {
                "tag": _or_unknown(sp.get("tag") if isinstance(sp.get("tag"), str) else None),
                "commit": sp.get("commit")
                if isinstance(sp.get("commit"), str) and _HEX40.match(sp["commit"])
                else UNKNOWN,
                "services": list(sp.get("services") or [])
                if isinstance(sp.get("services"), list)
                else [],
                "overrides": list(sp.get("overrides") or [])
                if isinstance(sp.get("overrides"), list)
                else [],
            }
            if rollback_out["stack_prev"]["tag"] == UNKNOWN:
                unknowns.append("rollback.stack_prev.tag unknown")
            if rollback_out["stack_prev"]["commit"] == UNKNOWN:
                unknowns.append("rollback.stack_prev.commit unknown")
        pc = rollback.get("per_component")
        if isinstance(pc, list):
            rollback_out["per_component"] = pc

    firmware_out = {
        "build": UNKNOWN,
        "app_commit": UNKNOWN,
        "assets_hash": UNKNOWN,
        "evidence": UNKNOWN,
    }
    if firmware:
        for key in firmware_out:
            val = firmware.get(key)
            firmware_out[key] = _or_unknown(val if isinstance(val, str) else None)
    for key, val in firmware_out.items():
        if val == UNKNOWN:
            unknowns.append(f"firmware.{key} unknown")

    mini_out = {"version": UNKNOWN, "evidence": UNKNOWN}
    if miniprogram:
        for key in mini_out:
            val = miniprogram.get(key)
            mini_out[key] = _or_unknown(val if isinstance(val, str) else None)
    for key, val in mini_out.items():
        if val == UNKNOWN:
            unknowns.append(f"miniprogram.{key} unknown")

    seen: set[str] = set()
    uniq_unknowns: list[str] = []
    for item in unknowns:
        if item not in seen:
            seen.add(item)
            uniq_unknowns.append(item)

    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_class": evidence_class,
        "captured_at": captured_at or UNKNOWN,
        "memoria_root": memoria_root or UNKNOWN,
        "stack": stack_out,
        "components": components,
        "firmware": firmware_out,
        "miniprogram": mini_out,
        "rollback": rollback_out,
        "unknowns": uniq_unknowns,
    }


def capture_from_backend(
    backend: Backend,
    *,
    evidence_class: str,
    memoria_root: Path | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for name in backend.list_memoria_containers():
        image = backend.inspect_field(name, "image")
        role = role_for_image(image)
        if role == "other":
            continue
        chain = backend.inspect_field(name, "chain")
        override_path = UNKNOWN
        if "," in chain:
            parts = chain.split(",")
            if len(parts) >= 2:
                override_path = parts[1]
        flags = backend.inspect_flags(name)
        stack_commit = flags.get("MEMORIA_RELEASE_COMMIT", UNKNOWN)
        observations.append(
            {
                "role": role,
                "container": name,
                "image": image,
                "image_id": backend.inspect_field(name, "image-id"),
                "registry_digest": backend.inspect_repo_digest(name),
                "revision": backend.inspect_field(name, "revision"),
                "stack_commit": stack_commit,
                "compose_chain": chain,
                "override_path": override_path,
                "started_at": backend.inspect_started_at(name),
                "health": backend.inspect_health(name),
                "restarts": backend.inspect_restarts(name),
                "effective_flags": flags,
            }
        )

    stack: dict[str, Any] = {}
    rollback: dict[str, Any] = {}
    root_s = str(memoria_root) if memoria_root else None
    if memoria_root is not None:
        current = memoria_root / "current"
        if current.is_symlink() or current.is_dir():
            resolved = current.resolve()
            stack["current_path"] = str(resolved)
            stack["current_tag"] = resolved.name
            env = read_release_env(resolved / ".env")
            stack["current_commit"] = env.get("MEMORIA_RELEASE_COMMIT", UNKNOWN)
            prev_path = resolved / ".cutover" / "prev-release"
            if prev_path.is_file():
                rollback["stack_prev"] = parse_prev_release(
                    prev_path.read_text(encoding="utf-8")
                )
            else:
                rollback["stack_prev"] = parse_prev_release("")

    return build_inventory(
        evidence_class=evidence_class,
        observations=observations,
        stack=stack or None,
        rollback=rollback or None,
        captured_at=captured_at or _utc_now(),
        memoria_root=root_s,
    )


def validate_inventory(doc: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if doc.get("evidence_class") not in EVIDENCE_CLASSES:
        errors.append("evidence_class invalid")
    if "components" not in doc or not isinstance(doc["components"], list):
        errors.append("components must be a list")
        return errors
    if "unknowns" not in doc or not isinstance(doc["unknowns"], list):
        errors.append("unknowns must be a list")
    if "rollback" not in doc or not isinstance(doc["rollback"], Mapping):
        errors.append("rollback missing")
    for comp in doc["components"]:
        if not isinstance(comp, Mapping):
            errors.append("component entry must be an object")
            continue
        for key in (
            "role",
            "image_ref",
            "image_id",
            "registry_digest",
            "image_revision",
            "stack_commit",
        ):
            if key not in comp:
                errors.append(f"component missing {key}")
        rev = comp.get("image_revision")
        if rev not in (None, UNKNOWN) and not (
            isinstance(rev, str) and _HEX40.match(rev)
        ):
            errors.append(
                f"{comp.get('role')}: image_revision must be 40-hex or unknown"
            )
        image_id = comp.get("image_id")
        if image_id not in (None, UNKNOWN) and not (
            isinstance(image_id, str) and _IMAGE_ID.match(image_id)
        ):
            errors.append(
                f"{comp.get('role')}: image_id must be sha256:64-hex, bare 64-hex, "
                "or unknown (short HANDOFF prefixes are not ids)"
            )
        image_ref = comp.get("image_ref")
        if (
            isinstance(image_ref, str)
            and ":" in image_ref
            and isinstance(rev, str)
            and rev != UNKNOWN
            and rev == image_ref.rsplit(":", 1)[-1]
        ):
            errors.append(
                f"{comp.get('role')}: image_revision must not equal image tag"
            )
    return errors


def render_markdown(doc: Mapping[str, Any]) -> str:
    lines = [
        f"# Runtime component inventory",
        "",
        f"- schema_version: `{doc.get('schema_version')}`",
        f"- evidence_class: `{doc.get('evidence_class')}`",
        f"- captured_at: `{doc.get('captured_at')}`",
        f"- memoria_root: `{doc.get('memoria_root')}`",
        "",
        "## Stack",
        "",
    ]
    stack = doc.get("stack") if isinstance(doc.get("stack"), Mapping) else {}
    lines.append(
        f"- current_tag: `{stack.get('current_tag', UNKNOWN)}`  \n"
        f"- current_commit: `{stack.get('current_commit', UNKNOWN)}`  \n"
        f"- current_path: `{stack.get('current_path', UNKNOWN)}`"
    )
    lines.extend(["", "## Components", ""])
    lines.append(
        "| role | image_ref | image_id | image_revision | stack_commit | "
        "override | health | started_at |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for comp in doc.get("components") or []:
        if not isinstance(comp, Mapping):
            continue
        lines.append(
            "| {role} | `{image_ref}` | `{image_id}` | `{image_revision}` | "
            "`{stack_commit}` | `{override_path}` | {health} | `{started_at}` |".format(
                role=comp.get("role", UNKNOWN),
                image_ref=comp.get("image_ref", UNKNOWN),
                image_id=comp.get("image_id", UNKNOWN),
                image_revision=comp.get("image_revision", UNKNOWN),
                stack_commit=comp.get("stack_commit", UNKNOWN),
                override_path=comp.get("override_path", UNKNOWN),
                health=comp.get("health", UNKNOWN),
                started_at=comp.get("started_at", UNKNOWN),
            )
        )
    rb = doc.get("rollback") if isinstance(doc.get("rollback"), Mapping) else {}
    sp = rb.get("stack_prev") if isinstance(rb.get("stack_prev"), Mapping) else {}
    lines.extend(
        [
            "",
            "## Rollback",
            "",
            f"- stack_prev.tag: `{sp.get('tag', UNKNOWN)}`",
            f"- stack_prev.commit: `{sp.get('commit', UNKNOWN)}`",
            f"- stack_prev.services: `{sp.get('services', [])}`",
            "",
            "## Unknowns",
            "",
        ]
    )
    unknowns = doc.get("unknowns") or []
    if not unknowns:
        lines.append("(none)")
    else:
        for item in unknowns:
            lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def compare_inventories(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> list[str]:
    diffs: list[str] = []
    left_map = {
        c["role"]: c
        for c in left.get("components") or []
        if isinstance(c, Mapping) and "role" in c
    }
    right_map = {
        c["role"]: c
        for c in right.get("components") or []
        if isinstance(c, Mapping) and "role" in c
    }
    roles = sorted(set(left_map) | set(right_map))
    keys = (
        "image_ref",
        "image_id",
        "image_revision",
        "stack_commit",
        "override_path",
        "health",
    )
    for role in roles:
        if role not in left_map:
            diffs.append(f"{role}: missing on left")
            continue
        if role not in right_map:
            diffs.append(f"{role}: missing on right")
            continue
        for key in keys:
            lv = left_map[role].get(key, UNKNOWN)
            rv = right_map[role].get(key, UNKNOWN)
            if lv != rv:
                diffs.append(f"{role}.{key}: {lv!r} != {rv!r}")
    lsp = (left.get("rollback") or {}).get("stack_prev") or {}
    rsp = (right.get("rollback") or {}).get("stack_prev") or {}
    if isinstance(lsp, Mapping) and isinstance(rsp, Mapping):
        for key in ("tag", "commit"):
            if lsp.get(key) != rsp.get(key):
                diffs.append(
                    f"rollback.stack_prev.{key}: {lsp.get(key)!r} != {rsp.get(key)!r}"
                )
    return diffs


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="observe host/docker and write inventory JSON")
    cap.add_argument("--output", type=Path, required=True)
    cap.add_argument("--memoria-root", type=Path, default=None)
    cap.add_argument(
        "--evidence-class",
        choices=sorted(EVIDENCE_CLASSES),
        default="live_capture",
    )

    val = sub.add_parser("validate", help="validate inventory schema")
    val.add_argument("inventory", type=Path)

    rend = sub.add_parser("render-md", help="render markdown table")
    rend.add_argument("inventory", type=Path)
    rend.add_argument("--output", type=Path, default=None)

    cmp_ = sub.add_parser("compare", help="diff two inventories by component")
    cmp_.add_argument("left", type=Path)
    cmp_.add_argument("right", type=Path)

    args = parser.parse_args(argv)

    if args.cmd == "capture":
        if args.evidence_class == "live_capture":
            backend: Backend = DockerBackend()
        else:
            print(
                "capture with evidence_class!=live_capture requires a lab harness; "
                "use build_inventory in tests",
                file=sys.stderr,
            )
            return 2
        doc = capture_from_backend(
            backend,
            evidence_class=args.evidence_class,
            memoria_root=args.memoria_root,
        )
        errors = validate_inventory(doc)
        if errors:
            print("inventory invalid after capture:", file=sys.stderr)
            for err in errors:
                print(f"  {err}", file=sys.stderr)
            return 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(_canonical(doc) + "\n", encoding="utf-8")
        print(f"runtime_inventory_ok {args.output}")
        return 0

    if args.cmd == "validate":
        doc = _load_json(args.inventory)
        errors = validate_inventory(doc)
        if errors:
            for err in errors:
                print(err, file=sys.stderr)
            return 1
        print("runtime_inventory_valid")
        return 0

    if args.cmd == "render-md":
        doc = _load_json(args.inventory)
        text = render_markdown(doc)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    if args.cmd == "compare":
        left = _load_json(args.left)
        right = _load_json(args.right)
        diffs = compare_inventories(left, right)
        if not diffs:
            print("runtime_inventory_compare=MATCH")
            return 0
        print("runtime_inventory_compare=DIFF")
        for line in diffs:
            print(line)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
