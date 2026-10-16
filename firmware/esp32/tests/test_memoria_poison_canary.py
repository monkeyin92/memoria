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


WINDOW_HARNESS = r"""
#include "memoria_poison_canary.h"

#include <cstdio>
#include <cstdint>

int main() {
    memoria::CanaryUsWindow window;
    uint32_t n = 9;
    uint32_t min_us = 9;
    uint32_t max_us = 9;
    uint32_t avg_us = 9;
    window.Take(n, min_us, max_us, avg_us);
    std::printf("empty=%u,%u,%u,%u\n", n, min_us, max_us, avg_us);

    window.Note(10);
    window.Note(30);
    window.Note(20);
    window.Take(n, min_us, max_us, avg_us);
    std::printf("sample=%u,%u,%u,%u\n", n, min_us, max_us, avg_us);
    window.Take(n, min_us, max_us, avg_us);
    std::printf("reset=%u,%u,%u,%u\n", n, min_us, max_us, avg_us);

    window.Note(1);
    window.Note(2);
    window.Take(n, min_us, max_us, avg_us);
    std::printf("trunc=%u,%u,%u,%u\n", n, min_us, max_us, avg_us);

    std::printf("elapsed=%u\n", memoria::CanaryElapsedUs(10, 40));
    std::printf("backward=%u\n", memoria::CanaryElapsedUs(40, 10));
    std::printf("clamp=%u\n", memoria::CanaryElapsedUs(0, static_cast<int64_t>(UINT32_MAX) + 5));
    return 0;
}
"""

VERSION_HARNESS = r"""
#include "memoria_poison_canary.h"

int main() {
    return memoria::kPoisonCanaryBytes == 12 ? 0 : 1;
}
"""

IDF_VERSION_ERROR = "单块 canary 校验依赖 IDF v6.0.2 multi_heap_poisoning 私有布局，升级 IDF 前必须重核"


def _compile(source: pathlib.Path, binary: pathlib.Path, extra: list[str]) -> subprocess.CompletedProcess[str]:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    return subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            *extra,
            str(source),
            "-o",
            str(binary),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_timing_window_reports_min_max_avg_then_clears() -> None:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "window.cc"
        source.write_text(WINDOW_HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "window"
        completed = _compile(source, binary, ["-I", str(HEADER.parent)])
        assert completed.returncode == 0, completed.stderr
        ran = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
    assert ran.stdout.splitlines() == [
        "empty=0,0,0,0",
        "sample=3,10,30,20",
        "reset=0,0,0,0",
        "trunc=2,1,2,1",
        "elapsed=30",
        "backward=0",
        "clamp=4294967295",
    ]


def _idf_stub(directory: pathlib.Path, version: str) -> None:
    (directory / "esp_idf_version.h").write_text(
        "\n".join(
            [
                "#pragma once",
                "#define ESP_IDF_VERSION_VAL(major, minor, patch) (((major) << 16) | ((minor) << 8) | (patch))",
                f"#define ESP_IDF_VERSION ESP_IDF_VERSION_VAL({version})",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_poison_build_rejects_any_idf_other_than_6_0_2() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _idf_stub(root, "6, 0, 1")
        source = root / "version.cc"
        source.write_text(VERSION_HARNESS, encoding="utf-8")
        completed = _compile(
            source,
            root / "version",
            [
                "-DCONFIG_HEAP_POISONING_COMPREHENSIVE=1",
                "-I",
                str(root),
                "-I",
                str(HEADER.parent),
            ],
        )
    assert completed.returncode != 0
    assert IDF_VERSION_ERROR in completed.stderr


def test_poison_build_accepts_idf_6_0_2() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _idf_stub(root, "6, 0, 2")
        source = root / "version.cc"
        source.write_text(VERSION_HARNESS, encoding="utf-8")
        completed = _compile(
            source,
            root / "version",
            [
                "-DCONFIG_HEAP_POISONING_COMPREHENSIVE=1",
                "-I",
                str(root),
                "-I",
                str(HEADER.parent),
            ],
        )
    assert completed.returncode == 0, completed.stderr
