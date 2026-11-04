# Runtime component inventory

Single source for “what is running” across the full stack and overlays. Machine file: [`current-runtime-inventory.json`](current-runtime-inventory.json). Historical dated HANDOFF sections stay historical receipts. They are not a second current table.

## Evidence classes

| Class | Meaning |
| --- | --- |
| `live_capture` | Written by `scripts/capture_runtime_inventory.py capture` against docker + `/opt/memoria` on a host. |
| `lab_fixture` | Tests with an injectable backend / fixture host tree. |
| `handoff_reconstruction` | Assembled only from dated HANDOFF (or similar) prose. Explicit unknowns. Not production verified. |

Never promote reconstruction to `live_capture` without a fresh host capture. Never invent a commit from an image name or tag alone.

## Capture on a host

Read-only. Does not cut over, freeze, or rewrite overrides.

```bash
sudo python3 scripts/capture_runtime_inventory.py capture \
  --memoria-root /opt/memoria \
  --evidence-class live_capture \
  --output /tmp/runtime-inventory.json

python3 scripts/capture_runtime_inventory.py validate /tmp/runtime-inventory.json
python3 scripts/capture_runtime_inventory.py compare \
  docs/ops/current-runtime-inventory.json /tmp/runtime-inventory.json
```

To publish the capture into the repo (after review):

```bash
cp /tmp/runtime-inventory.json docs/ops/current-runtime-inventory.json
python3 scripts/capture_runtime_inventory.py render-md \
  docs/ops/current-runtime-inventory.json \
  --output docs/ops/current-runtime-inventory.rendered.md
```

Inspect fields match `scripts/release_ops.sh` `inspect_container`. Rollback is read from the frozen `current/.cutover/prev-release` file. The tool does not call `derive_prev`.

## Compare to a running instance

`compare` diffs by component role on `image_ref`, `image_id`, `image_revision`, `stack_commit`, `override_path`, and `health`, plus `rollback.stack_prev.{tag,commit}`. Exit 0 on match, 1 on diff. Unknown on either side still shows as a mismatch when the other side has a concrete value.

## Lab / isolation proof

This unit’s automated proof is `scripts/tests/test_capture_runtime_inventory.py` (`lab_fixture`): mixed stack+overlay, missing OCI revision → `unknown`, `prev-release` rollback anchor, format-string contract against `release_ops.sh`, and compare diffs. No Monkey production auth. No live production cutover.

## code / wired / enabled / verified

Each component row carries those four status fields plus optional `evidence_dates`. Fill them from real receipts. Leave `verified` as `not_live_captured` until a `live_capture` inventory exists.

## Local sync after cloud land

On the MacBook checkout:

```bash
cd ~/projects/memoria
git fetch origin
git checkout cursor/a12-runtime-component-inventory-cbd1
git pull --ff-only origin cursor/a12-runtime-component-inventory-cbd1
```

After merge to main, pull `main` instead.

## Related tools (not this inventory)

- `scripts/create_release_manifest.py` — publish-time whole-stack artifact set.
- `scripts/deploy_*_component.sh` `component-manifest.txt` — pre-build component bind.
- `scripts/release_ops.sh` — cutover / freeze / rollback.
- `scripts/production_layout_audit.sh` — path + container name listing only.
