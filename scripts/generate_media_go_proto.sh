#!/usr/bin/env bash
set -euo pipefail

# The Go bindings are generated from Memoria's own media-v1 contract.  This
# script intentionally does not vendor or copy a media framework; protoc and
# the two small protobuf generators are build-time tools only.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$root/services/media_edge/gen"
mkdir -p "$out"

command -v protoc >/dev/null || { echo "protoc is required" >&2; exit 1; }
command -v protoc-gen-go >/dev/null || { echo "protoc-gen-go is required" >&2; exit 1; }
command -v protoc-gen-go-grpc >/dev/null || { echo "protoc-gen-go-grpc is required" >&2; exit 1; }

protoc \
  -I "$root/packages/proto" \
  --go_out=paths=source_relative:"$out" \
  --go-grpc_out=paths=source_relative:"$out" \
  "$root/packages/proto/memoria/media/v1/media.proto" \
  "$root/packages/proto/memoria/media/v1/device.proto" \
  "$root/packages/proto/memoria/media/v1/events.proto"

gofmt -w "$out/memoria/media/v1"/*.go
