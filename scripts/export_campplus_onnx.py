#!/usr/bin/env python3
"""Export the pinned official CAM++ checkpoint to a dynamic-shape ONNX model."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speakerlab-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-onnx-sha256", default="")
    args = parser.parse_args()

    speakerlab_root = args.speakerlab_root.resolve()
    if not speakerlab_root.joinpath("speakerlab/models/campplus/DTDNN.py").is_file():
        raise ValueError("speakerlab root does not contain the CAM++ implementation")
    if not args.checkpoint.is_file():
        raise ValueError("CAM++ checkpoint does not exist")
    sys.path.insert(0, str(speakerlab_root))

    import numpy as np  # type: ignore[import-not-found]
    import onnx  # type: ignore[import-not-found]
    import onnxruntime  # type: ignore[import-not-found]
    import torch  # type: ignore[import-not-found]
    from speakerlab.models.campplus.DTDNN import CAMPPlus  # type: ignore[import-not-found]

    state = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    checkpoint_sha256 = hashlib.sha256(args.checkpoint.read_bytes()).hexdigest()
    if checkpoint_sha256 != "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8":
        raise ValueError("CAM++ checkpoint digest does not match the pinned official artifact")
    model = CAMPPlus(feat_dim=80, embedding_size=192)
    model.load_state_dict(state)
    model.eval()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dummy_feature = torch.randn(1, 345, 80)
    torch.onnx.export(
        model,
        dummy_feature,
        args.output,
        export_params=True,
        opset_version=11,
        do_constant_folding=True,
        input_names=["feature"],
        output_names=["embedding"],
        dynamic_axes={
            "feature": {0: "batch_size", 1: "frame_num"},
            "embedding": {0: "batch_size"},
        },
        dynamo=False,
    )
    exported = onnx.load(args.output)
    onnx.checker.check_model(exported)
    input_shape = exported.graph.input[0].type.tensor_type.shape.dim
    output_shape = exported.graph.output[0].type.tensor_type.shape.dim
    if len(input_shape) != 3 or input_shape[2].dim_value != 80:
        raise ValueError("exported CAM++ input contract is not [batch, frames, 80]")
    if len(output_shape) != 2 or output_shape[1].dim_value != 192:
        raise ValueError("exported CAM++ output contract is not [batch, 192]")
    pool_attributes = []
    for node in exported.graph.node:
        if node.op_type != "AveragePool":
            continue
        attributes = {
            attribute.name: onnx.helper.get_attribute_value(attribute)
            for attribute in node.attribute
        }
        pool_attributes.append(attributes.get("count_include_pad", 0))
    if len(pool_attributes) != 52 or set(pool_attributes) != {0}:
        raise ValueError("CAM++ ONNX must exclude padded values in all 52 AveragePool nodes")

    session = onnxruntime.InferenceSession(
        str(args.output),
        providers=["CPUExecutionProvider"],
    )
    worst_max_abs = 0.0
    worst_cosine = 1.0
    for frames in (100, 200, 300, 345, 370, 400, 489, 529, 1000):
        generator = torch.Generator().manual_seed(frames)
        features = torch.randn(1, frames, 80, generator=generator)
        with torch.no_grad():
            expected = model(features).cpu().numpy()
        actual = session.run(["embedding"], {"feature": features.numpy()})[0]
        max_abs = float(np.max(np.abs(expected - actual)))
        cosine = float(
            np.sum(expected * actual)
            / (np.linalg.norm(expected) * np.linalg.norm(actual))
        )
        worst_max_abs = max(worst_max_abs, max_abs)
        worst_cosine = min(worst_cosine, cosine)
    if worst_max_abs > 1e-4 or worst_cosine < 0.99999:
        raise ValueError(
            "CAM++ PyTorch/ONNX parity failed: "
            f"max_abs={worst_max_abs} cosine={worst_cosine}"
        )

    onnx_sha256 = hashlib.sha256(args.output.read_bytes()).hexdigest()
    if args.expected_onnx_sha256 and onnx_sha256 != args.expected_onnx_sha256:
        raise ValueError("CAM++ ONNX digest is not reproducible in the pinned builder")
    sidecar = args.output.with_suffix(f"{args.output.suffix}.sha256")
    sidecar.write_text(f"{onnx_sha256}\n", encoding="ascii")
    manifest = {
        "model_id": "iic/speech_campplus_sv_zh-cn_16k-common",
        "model_revision": "v1.0.0",
        "model_revision_commit": "7e452fc19c2b0c761d591a4241c332e02dfe10d3",
        "source_commit": "065629c313eaf1a01c65c640c46d77e61e9607b4",
        "checkpoint_sha256": checkpoint_sha256,
        "onnx_sha256": onnx_sha256,
        "preprocessing": "fbank-80-cmn-v1",
        "average_pool_count_include_pad": 0,
        "parity_worst_max_abs": worst_max_abs,
        "parity_worst_cosine": worst_cosine,
        "torch_version": torch.__version__,
        "onnx_version": onnx.__version__,
        "onnxruntime_version": onnxruntime.__version__,
    }
    if args.manifest:
        args.manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
