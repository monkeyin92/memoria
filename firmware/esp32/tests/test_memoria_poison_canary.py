from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

HEADER = (
    pathlib.Path(__file__).parents[1]
    / "overlay"
    / "files"
    / "main"
    / "memoria"
    / "memoria_poison_canary.h"
)

HARNESS = r"""
#include "memoria_poison_canary.h"

#include <cstdio>
#include <cstring>

int main() {
    alignas(8) unsigned char arena[4096];
    std::memset(arena, 0x5a, sizeof(arena));
    constexpr uint32_t kUser = 24;
    unsigned char* user = arena + 64;
    memoria::PoisonHead head{memoria::kPoisonHeadCanary, kUser};
    std::memcpy(user - sizeof(head), &head, sizeof(head));
    uint32_t tail = memoria::kPoisonTailCanary;
    std::memcpy(user + kUser, &tail, sizeof(tail));
    arena[0] = 0x11;
    arena[sizeof(arena) - 1] = 0x22;

    std::printf("%s\n", memoria::PoisonCanaryOk(user, kUser) ? "neighbor=ok" : "neighbor=bad");

    head.head_canary = 0;
    std::memcpy(user - sizeof(head), &head, sizeof(head));
    std::printf("%s\n", memoria::PoisonCanaryOk(user, kUser) ? "head=ok" : "head=bad");

    head.head_canary = memoria::kPoisonHeadCanary;
    std::memcpy(user - sizeof(head), &head, sizeof(head));
    tail = 0;
    std::memcpy(user + kUser, &tail, sizeof(tail));
    std::printf("%s\n", memoria::PoisonCanaryOk(user, kUser) ? "tail=ok" : "tail=bad");

    tail = memoria::kPoisonTailCanary;
    std::memcpy(user + kUser, &tail, sizeof(tail));
    head.alloc_size = 0xFFFFFFF0u;
    std::memcpy(user - sizeof(head), &head, sizeof(head));
    std::printf("%s\n", memoria::PoisonCanaryOk(user, kUser) ? "size=ok" : "size=bad");

    std::printf("bytes=%u\n", memoria::kPoisonCanaryBytes);
    return 0;
}
"""


def test_one_block_ignores_a_smashed_neighbor() -> None:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "canary"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(HEADER.parent),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        completed = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
    assert completed.stdout.splitlines() == [
        "neighbor=ok",
        "head=bad",
        "tail=bad",
        "size=bad",
        "bytes=12",
    ]
