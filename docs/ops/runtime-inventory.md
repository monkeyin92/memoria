# Capture a runtime component inventory

The machine record is [`current-runtime-inventory.json`](current-runtime-inventory.json). Dated sections in `HANDOFF.md` stay receipts. Use this page to capture a host and compare it with that file.

The copy in git was `handoff_reconstruction`, assembled from dated HANDOFF receipts. On 2026-11-10 it was replaced with a `live_capture` read from `memoria-prod` (`/opt/memoria`, read-only). The firmware and miniprogram sections are not host-visible and remain carried from the 2026-11-04 handoff record; the file's `unknowns` says so.

## Choose an evidence class

| Class | Meaning |
| --- | --- |
| `live_capture` | `capture` read docker and `/opt/memoria` on a host. |
| `lab_fixture` | A test called the library with a stub backend and a fixture tree. |
| `handoff_reconstruction` | The JSON was assembled from dated HANDOFF prose. `unknowns` lists every gap. The file is not production verified. |

`capture` accepts only `live_capture`. Build the other two classes in process, or write the JSON and run `validate`.

Do not copy a commit out of an image name or tag. A missing OCI `org.opencontainers.image.revision` label is `revision_commit: unknown`, plus a line in `unknowns`. An image id of 8 to 63 hex digits stays a prefix, with `image_id_complete` false, and `unknowns` must contain the word `prefix`. A full id is `sha256:` plus 64 hex digits, or 64 hex digits alone. Do not invent a registry digest.

## Capture on the host

The command is read-only. It does not cut over, freeze, or rewrite overrides.

```bash
sudo python3 scripts/capture_runtime_inventory.py capture \
  --memoria-root /opt/memoria \
  --evidence-class live_capture \
  --carry-forward docs/ops/current-runtime-inventory.json \
  --output /tmp/runtime-inventory.json

python3 scripts/capture_runtime_inventory.py validate /tmp/runtime-inventory.json
```

Pass `--carry-forward` to keep the parts a host cannot see: the `firmware` and `miniprogram` sections, their placeholder component rows, and each component's `status` dates. Host observations win where both exist, and `unknowns` is derived from the merged values, so a field with a value never keeps its `unknown` line. `validate` rejects that contradiction.

Image, image id, OCI revision, OCI version, the compose chain label, and `MEMORIA_RELEASE_TAG` use the same `docker inspect` formats as `inspect_container` in `scripts/release_ops.sh`. The command also reads `StartedAt`, health, and restart count.

Env values are filtered inside the inspect format. Only these keys are kept:

- `TTS_PROVIDER`
- `STEPFUN_TTS_ENABLED`
- `STEPFUN_TTS_VOICE`
- `MEMORIA_SPEAKER_AUTHORITY_ENABLED`
- `MEMORIA_RELEASE_TAG`
- `MEMORIA_RELEASE_COMMIT`

With `--memoria-root`, the command reads `current`, `MEMORIA_RELEASE_TAG` and `MEMORIA_RELEASE_COMMIT` from that release's `.env`, `.cutover/prev-release`, and `state/component-overrides.tsv`. The compose label lists files in apply order, so `override_path` is the last entry, the override that actually shaped the container. A `*-component.override.yml` is paired with `*-rollback.override.yml` or `*-component.rollback.override.yml`, whichever exists. It does not call `derive_prev`.

## Compare with a running instance

```bash
python3 scripts/capture_runtime_inventory.py compare \
  docs/ops/current-runtime-inventory.json /tmp/runtime-inventory.json
```

Compare pairs rows by `role`. It reports `image_ref`, `image_id`, `revision_commit`, `override_path`, `override_sha256`, `health`, and `rollback.stack_prev` tag and commit. Exit 0 means those fields match. Exit 1 prints one line per difference. `unknown` against a concrete value is a difference.

`render-md` prints a markdown table on stdout. Pass `--output` only for a scratch path outside `docs/`. Do not commit a second rendered markdown file under `docs/`.

## Sync a local checkout

```bash
cd ~/projects/memoria && git fetch && git checkout <branch> && git pull
```

Replace `<branch>` with the branch you want. After the branch merges, check out `main` and pull that.

## Related commands

`scripts/create_release_manifest.py` records a publish. `scripts/deploy_*_component.sh` writes a pre-build `component-manifest.txt`. `scripts/release_ops.sh` cuts over, freezes, and rolls back. `scripts/production_layout_audit.sh` lists paths and container names. None of those writes this inventory.
