"""Host-side checks for the on-device local stop keyword.

`memoria_stop_keyword.h` has no ESP-IDF dependency, so the exact phrase table,
per-phrase thresholds and debounce compile on the host. The wiring through
MultiNet, Application and the protocol is checked as source text, and the
keyword.detected message the firmware renders is pinned to the shared
fixture that the Go media-edge test feeds through its strict decoder.
"""

from __future__ import annotations

import json
import math
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest
from jsonschema import Draft202012Validator

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
REPO_ROOT = pathlib.Path(__file__).parents[3]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
HEADER = MEMORIA_DIR / "memoria_stop_keyword.h"
PROTOCOL_SOURCE = (MEMORIA_DIR / "memoria_protocol.cc").read_text(encoding="utf-8")
PROTOCOL_HEADER = (MEMORIA_DIR / "memoria_protocol.h").read_text(encoding="utf-8")
PATCH_0031 = (
    FIRMWARE_ROOT / "overlay" / "patches" / "0031-memoria-local-stop-keyword.patch"
).read_text(encoding="utf-8")
RELEASE_HEADER = (MEMORIA_DIR / "memoria_firmware_release.h").read_text(encoding="utf-8")
CONTRACT = json.loads(
    (REPO_ROOT / "packages" / "contracts" / "device-media-v2.json").read_text(encoding="utf-8")
)
FIXTURE_PATH = REPO_ROOT / "packages" / "contracts" / "device-keyword-stop-v2.example.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

HARNESS = r"""
#include "memoria_stop_keyword.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

int main() {
    memoria::LocalStopKeywordGate gate;
    char line[256];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        char command[16] = {};
        char id[64] = {};
        char score_text[32] = {};
        int armed = 0;
        long long now_ms = 0;
        if (sscanf(line, "%15s", command) != 1) {
            continue;
        }
        if (strcmp(command, "reset") == 0) {
            gate.Reset();
        } else if (strcmp(command, "eval") == 0) {
            if (sscanf(line, "%*s %63s %31s %d %lld", id, score_text, &armed, &now_ms) != 4) {
                return 2;
            }
            const float score = strtof(score_text, nullptr);
            printf("%s\n", memoria::LocalStopVerdictName(
                               gate.Evaluate(id, score, armed != 0, now_ms)));
        } else if (strcmp(command, "conf") == 0) {
            if (sscanf(line, "%*s %31s", score_text) != 1) {
                return 2;
            }
            printf("%.6f\n", static_cast<double>(
                                 memoria::LocalStopReportedConfidence(strtof(score_text, nullptr))));
        } else if (strcmp(command, "table") == 0) {
            for (const auto& phrase : memoria::kLocalStopPhrases) {
                printf("%s|%s|%s|%.4f|%u\n", phrase.id, phrase.command, phrase.display,
                       static_cast<double>(phrase.min_score), phrase.nominal_duration_ms);
            }
            printf("debounce %lld\n", static_cast<long long>(memoria::kLocalStopDebounceMs));
            printf("floor %.4f\n",
                   static_cast<double>(memoria::kLocalStopHardStopConfidenceFloor));
            printf("detect %.4f\n", static_cast<double>(memoria::kMultiNetDetectThreshold));
            printf("wake %.4f\n", static_cast<double>(memoria::kWakeWordMinScore));
            printf("enabled %d\n", memoria::kLocalStopKeywordEnabled ? 1 : 0);
            printf("registered %d\n",
                   static_cast<int>(memoria::LocalStopPhrasesToRegister().end() -
                                    memoria::LocalStopPhrasesToRegister().begin()));
        } else if (strcmp(command, "wake") == 0) {
            int transcript_matches = 0;
            if (sscanf(line, "%*s %31s %d", score_text, &transcript_matches) != 2) {
                return 2;
            }
            printf("%s\n", memoria::WakeWordAccepted(strtof(score_text, nullptr), transcript_matches != 0) ? "wake" : "reject");
        } else {
            return 2;
        }
        fflush(stdout);
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def gate_tool() -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "stop_gate_tool"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O0",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(MEMORIA_DIR),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        yield binary


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True
    )
    return completed.stdout.splitlines()


def _table(tool: pathlib.Path) -> tuple[dict[str, dict[str, object]], dict[str, float]]:
    phrases: dict[str, dict[str, object]] = {}
    constants: dict[str, float] = {}
    for line in _run(tool, "table"):
        name, _, value = line.partition(" ")
        if name in {"debounce", "floor", "detect", "wake", "enabled", "registered"}:
            constants[name] = float(value)
        else:
            phrase_id, command, display, min_score, duration = line.split("|")
            phrases[phrase_id] = {
                "command": command,
                "display": display,
                "min_score": float(min_score),
                "duration_ms": int(duration),
            }
    return phrases, constants


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "freertos", "ESP_LOG", "cJSON"):
        assert forbidden not in source


def test_phrase_table_is_tunable_above_the_detection_floor(gate_tool: pathlib.Path) -> None:
    phrases, constants = _table(gate_tool)
    assert {"ting_yi_xia", "bie_shuo_le", "ting_ting", "ting"} <= set(phrases)
    assert phrases["ting_yi_xia"]["command"] == "ting yi xia"
    assert phrases["bie_shuo_le"]["command"] == "bie shuo le"
    assert phrases["ting_ting"]["command"] == "ting ting"
    assert phrases["ting"]["command"] == "ting"
    identifier = re.compile(CONTRACT["$defs"]["identifier"]["pattern"])
    commands = [str(phrase["command"]) for phrase in phrases.values()]
    assert len(set(commands)) == len(commands)
    # MultiNet reports everything from the 0.10 detection floor so weak hits
    # reach the log; acceptance is per command, never below that floor.
    assert constants["detect"] == pytest.approx(0.10)
    for phrase_id, phrase in phrases.items():
        assert identifier.fullmatch(phrase_id), phrase_id
        assert re.fullmatch(r"[a-z]+( [a-z]+)*", str(phrase["command"]))
        assert constants["detect"] < float(phrase["min_score"]) < 1.0
        assert 0 < int(phrase["duration_ms"]) <= 60_000
    # Build 12 thresholds from the first device run (see the header comment).
    assert float(phrases["ting_yi_xia"]["min_score"]) == pytest.approx(0.20)
    assert float(phrases["bie_shuo_le"]["min_score"]) == pytest.approx(0.20)
    assert float(phrases["ting_ting"]["min_score"]) == pytest.approx(0.20)
    assert float(phrases["ting"]["min_score"]) == pytest.approx(0.30)
    # A single syllable false-triggers most easily: it is the strictest row.
    assert float(phrases["ting"]["min_score"]) == max(
        float(phrase["min_score"]) for phrase in phrases.values()
    )
    assert constants["debounce"] >= 1000
    # Edge/Core KWSHardStopMinConfidence.
    assert constants["floor"] == pytest.approx(0.8)


def test_wake_word_acceptance_requires_consistent_text_for_weak_hits(gate_tool: pathlib.Path) -> None:
    phrases, constants = _table(gate_tool)
    # Keep the measured recall floor, but require transcript consistency for
    # weak hits so a low-score echo such as "mo mo li" cannot wake the device.
    assert constants["wake"] == pytest.approx(0.12)
    assert constants["wake"] > constants["detect"]
    assert _run(
        gate_tool,
        "wake 0.10 0",
        "wake 0.119 0",
        "wake 0.12 0",
        "wake 0.15 0",
        "wake 0.27 0",
        "wake nan 0",
    ) == [
        "reject",
        "reject",
        "reject",
        "reject",
        "wake",
        "reject",
    ]
    assert _run(
        gate_tool,
        "wake 0.12 1",
        "wake 0.15 1",
        "wake 0.199 1",
        "wake 0.20 0",
        "wake 0.27 0",
    ) == ["wake", "wake", "wake", "wake", "wake"]


def test_detection_floor_matches_the_board_sdkconfig(gate_tool: pathlib.Path) -> None:
    _, constants = _table(gate_tool)
    board = json.loads(
        (
            FIRMWARE_ROOT
            / "overlay"
            / "files"
            / "main"
            / "boards"
            / "memoria"
            / "esp-vocat"
            / "config.json"
        ).read_text(encoding="utf-8")
    )
    sdkconfig = board["builds"][0]["sdkconfig_append"]
    thresholds = [
        item for item in sdkconfig if item.startswith("CONFIG_CUSTOM_WAKE_WORD_THRESHOLD=")
    ]
    assert thresholds == ["CONFIG_CUSTOM_WAKE_WORD_THRESHOLD=10"]
    assert int(thresholds[0].split("=")[1]) / 100 == pytest.approx(constants["detect"])


def test_gate_requires_stop_only_mode_and_the_phrase_threshold(gate_tool: pathlib.Path) -> None:
    assert _run(
        gate_tool,
        "eval ting_yi_xia 0.90 0 1000",
        "eval not_a_phrase 0.90 1 1000",
        "eval ting_yi_xia 0.19 1 1000",
        "eval ting 0.29 1 1000",
        "eval ting_yi_xia 0.20 1 1000",
    ) == ["not_armed", "unknown_phrase", "below_threshold", "below_threshold", "accepted"]


def test_one_utterance_sends_one_stop(gate_tool: pathlib.Path) -> None:
    # MultiNet cleans after a hit, so the tail of 「停停」 (or a repeated 「停」)
    # can hit again immediately; only the first one may stop playback.
    assert _run(
        gate_tool,
        "eval ting_ting 0.40 1 10000",
        "eval ting 0.50 1 10200",
        "eval ting_ting 0.40 1 11499",
        "eval ting_ting 0.40 1 11500",
    ) == ["accepted", "debounced", "debounced", "accepted"]


def test_rejected_hits_do_not_start_the_debounce_window(gate_tool: pathlib.Path) -> None:
    assert _run(
        gate_tool,
        "eval ting_yi_xia 0.10 1 5000",
        "eval ting_yi_xia 0.90 0 5100",
        "eval ting_yi_xia 0.90 1 5200",
        "reset",
        "eval ting_yi_xia 0.90 1 5300",
    ) == ["below_threshold", "not_armed", "accepted", "accepted"]


def test_wire_confidence_is_lifted_to_the_hard_stop_floor(gate_tool: pathlib.Path) -> None:
    values = [
        float(line) for line in _run(gate_tool, "conf 0.26", "conf 0.93", "conf 1.5", "conf nan")
    ]
    assert values[0] == pytest.approx(0.8, abs=1e-6)
    assert values[1] == pytest.approx(0.93, abs=1e-6)
    assert values[2] == pytest.approx(1.0)
    assert values[3] == pytest.approx(0.8, abs=1e-6)
    assert all(not math.isnan(value) for value in values)


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    body_start = source.index("{", start)
    depth = 0
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[body_start : index + 1]
    raise AssertionError(f"unterminated function {signature}")


def _emitted_shape(body: str) -> dict[str, list[tuple[str, str]]]:
    objects: dict[str, list[tuple[str, str]]] = {
        "root.value": [],
        "evidence": [],
        "expected_fence": [],
    }
    pattern = re.compile(
        r'cJSON_Add(String|Number|Bool|Item)ToObject\(\s*([\w.]+),\s*"(\w+)",\s*([^;]+)\);'
    )
    for kind, target, key, value in pattern.findall(body):
        objects[target].append((key, value.strip() if kind != "Item" else "object"))
    return objects


def test_keyword_stop_message_matches_the_shared_fixture_exactly() -> None:
    body = _function_body(PROTOCOL_SOURCE, "void MemoriaProtocol::SendKeywordStop(")
    shape = _emitted_shape(body)
    # Edge decodes with DisallowUnknownFields and closes the socket on any
    # unknown or missing field, so the key sets and order must be identical.
    assert [key for key, _ in shape["root.value"]] == list(FIXTURE)
    assert [key for key, _ in shape["evidence"]] == list(FIXTURE["evidence"])
    assert [key for key, _ in shape["expected_fence"]] == list(FIXTURE["expected_fence"])

    top = dict(shape["root.value"])
    evidence = dict(shape["evidence"])
    assert top["type"] == '"keyword.detected"' == json.dumps(FIXTURE["type"])
    assert top["version"] == "2" == str(FIXTURE["version"])
    assert top["hard_stop"] == "true" and FIXTURE["hard_stop"] is True
    assert top["keyword_id"] == "phrase->id"
    assert "LocalStopReportedConfidence(hit.score)" in body
    assert evidence["source"] == '"local_kws"' == json.dumps(FIXTURE["evidence"]["source"])
    assert evidence["aec_mode"] == '"fd_low_cost"' == json.dumps(FIXTURE["evidence"]["aec_mode"])
    assert evidence["aec_verified"] == "false" and FIXTURE["evidence"]["aec_verified"] is False
    assert evidence["far_end_rms"] == "0" == str(FIXTURE["evidence"]["far_end_rms"])
    assert (
        evidence["speaker_class"] == '"unknown"' == json.dumps(FIXTURE["evidence"]["speaker_class"])
    )
    # The hit is estimated to span the phrase before the current uplink
    # sample, on the 16 kHz capture clock Edge multiplies duration_ms by.
    assert "phrase->nominal_duration_ms) * kUplinkSampleRate / 1000" in body
    assert "uplink_sample_start_ - duration_samples" in body
    assert "QueueTransportText(RenderJson(root.value))" in body


def test_shared_fixture_is_a_valid_device_media_v2_hard_stop() -> None:
    Draft202012Validator(CONTRACT).validate(FIXTURE)
    assert FIXTURE["type"] == "keyword.detected"
    assert FIXTURE["hard_stop"] is True
    assert FIXTURE["confidence"] >= 0.8
    assert FIXTURE["evidence"]["source"] == "local_kws"
    assert FIXTURE["keyword_id"] == "ting_yi_xia"


def test_keyword_barge_in_is_parsed_from_signed_settings_and_reset() -> None:
    assert "bool KeywordBargeInAllowed() const;" in PROTOCOL_HEADER
    assert "bool LocalStopKeywordArmed();" in PROTOCOL_HEADER
    assert "bool keyword_barge_in_allowed_ = false;" in PROTOCOL_HEADER
    accepted = _function_body(PROTOCOL_SOURCE, "bool MemoriaProtocol::HandleSessionAccepted(")
    assert 'if (entry == "keyword") {' in accepted
    assert "keyword_barge_in_allowed = true;" in accepted
    # "none" disables every source, matching Edge's bargeInSourceAllowed.
    assert accepted.index("if (barge_in_none) {") < accepted.index(
        "keyword_barge_in_allowed_ = keyword_barge_in_allowed;"
    )
    reset = _function_body(PROTOCOL_SOURCE, "void MemoriaProtocol::ResetSessionState(")
    assert "keyword_barge_in_allowed_ = false;" in reset

    allowed = _function_body(PROTOCOL_SOURCE, "bool MemoriaProtocol::KeywordBargeInAllowed(")
    assert "protocol_version_ == kProtocolVersionV2" in allowed
    assert "keyword_barge_in_allowed_" in allowed
    armed = _function_body(PROTOCOL_SOURCE, "bool MemoriaProtocol::LocalStopKeywordArmed(")
    assert "KeywordBargeInAllowed() && HasActivePlaybackGeneration()" in armed


def test_keyword_stop_reuses_the_button_local_flush_and_fence() -> None:
    notify = _function_body(PROTOCOL_SOURCE, "bool MemoriaProtocol::NotifyLocalKeywordStop(")
    # Re-checked under the playback lock: the hit crossed tasks, and a closed
    # window must neither flush nor report.
    assert notify.index("std::lock_guard<std::recursive_mutex>") < notify.index(
        "if (!LocalStopKeywordArmed())"
    )
    assert notify.index("if (!LocalStopKeywordArmed())") < notify.index("LocalHardStop(&hit);")
    button = _function_body(PROTOCOL_SOURCE, "void MemoriaProtocol::NotifyLocalFlush(")
    assert "LocalHardStop(nullptr);" in button

    hard_stop = _function_body(PROTOCOL_SOURCE, "void MemoriaProtocol::LocalHardStop(")
    flush = hard_stop.index("on_local_flush_requested_(0);")
    capture = hard_stop.index("const GenerationFence fence = fence_;")
    keyword = hard_stop.index("SendKeywordStop(fence, *keyword);")
    button_stop = hard_stop.index("SendButtonStop(fence, local_flush_sample_end);")
    # Fence captured before the gate closes; the report follows the flush and
    # is the terminal receipt for the stopped generation either way.
    assert capture < flush < keyword
    assert flush < button_stop
    assert hard_stop.index("playback_terminal_receipted_ = true;") < keyword


def test_local_stop_keyword_is_off_and_registers_no_phrase(gate_tool: pathlib.Path) -> None:
    # 2026-09-30: MultiNet during playback starved the idle task, so playback
    # is back to build 10's load; the cloud path stops the reply.
    _, constants = _table(gate_tool)
    assert constants["enabled"] == 0
    assert constants["registered"] == 0


def test_hello_declares_the_local_stop_keyword() -> None:
    hello = _function_body(PROTOCOL_SOURCE, "std::string MemoriaProtocol::DeviceHelloV2(")
    assert (
        'cJSON_AddBoolToObject(capabilities, "local_stop_keyword", kLocalStopKeywordEnabled)'
        in hello
    )


def _added_lines(patch: str, path: str) -> str:
    section = patch[patch.index(f"diff --git a/{path} b/{path}") :]
    following = section.find("\ndiff --git ", 1)
    if following != -1:
        section = section[:following]
    return "\n".join(
        line[1:]
        for line in section.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def test_multinet_registers_stop_phrases_and_ignores_wake_in_stop_only_mode() -> None:
    wake = _added_lines(PATCH_0031, "main/audio/wake_words/custom_wake_word.cc")
    assert "for (const auto& phrase : memoria::LocalStopPhrasesToRegister())" in wake
    assert "commands_.push_back({phrase.command, phrase.id, memoria::kLocalStopAction});" in wake
    assert "stop_gate_.Evaluate(command.text.c_str(), score, stop_only_.load(), now_ms)" in wake
    # Stop-only mode logs every stop hit that reaches the 0.10 floor at INFO;
    # idle/listening hits act on nothing and stay at DEBUG.
    handler = wake[wake.index("void CustomWakeWord::HandleStopCommand") :]
    handler = handler[: handler.index("stop_keyword_detected_callback_(command.text, score);")]
    not_armed = handler[handler.index("if (verdict == memoria::LocalStopVerdict::kNotArmed) {") :]
    assert (
        not_armed.index("ESP_LOGD(")
        < not_armed.index("} else {")
        < not_armed.index('ESP_LOGI(TAG, "Local stop keyword %s: id=%s prob=%.3f min=%.2f",')
    )
    # Misses are visible: detection windows and timeouts log their candidates
    # while stop-only mode is armed.
    assert 'LogStopModeCandidates("detected", mn_result);' in wake
    assert 'LogStopModeCandidates("timeout", multinet_->get_results(multinet_model_data_));' in wake
    assert wake.count("if (stop_only_.load()) {") == 2
    # The MultiNet floor comes from the header, not a second hand-kept value.
    assert "threshold_ = memoria::kMultiNetDetectThreshold;" in wake
    assert "MultiNet rejected command" in wake
    # In stop-only mode a wake hit neither starts a conversation nor ends
    # detection: it is skipped before the existing running_ = false branch.
    stop_only_wake = wake[wake.index('if (command.action == "wake" && stop_only_.load()) {') :]
    stop_only_wake = stop_only_wake[: stop_only_wake.index("continue;")]
    assert "running_ = false" not in stop_only_wake
    assert "wake_word_detected_callback_" not in stop_only_wake
    # Weak wake hits must match the configured transcript before the unchanged
    # detection log and wake-up.
    wake_gate = wake.index('if (command.action == "wake") {')
    gate_body = wake[wake_gate : wake.index("continue;", wake_gate)]
    assert "WakeWordTextMatchesCommand" in gate_body
    assert "WakeWordAccepted(mn_result->prob[i], transcript_matches)" in gate_body
    assert "Wake word rejected:" in gate_body
    assert wake_gate < wake.index(
        'ESP_LOGI(TAG, "Custom wake word detected: command_id=%d, string=%s, prob=%f",'
    )
    header = _added_lines(PATCH_0031, "main/audio/wake_words/custom_wake_word.h")
    assert '#include "memoria/memoria_stop_keyword.h"' in header
    assert "std::atomic<bool> stop_only_ = false;" in header


def test_detection_leaves_stop_only_mode_whenever_wake_detection_is_reconfigured() -> None:
    engine = _added_lines(PATCH_0031, "main/audio/engines/afe_audio_engine.cc")
    assert "bool AfeAudioEngine::EnableStopKeywordDetection()" in engine
    assert "custom_wake_word_->SetStopOnly(true);" in engine
    assert engine.count("custom_wake_word_->SetStopOnly(false);") == 2
    service = _added_lines(PATCH_0031, "main/audio/audio_service.cc")
    assert "bool AudioService::EnableStopKeywordDetection()" in service
    # Stop hits must not clear AS_EVENT_WAKE_WORD_RUNNING like a wake word.
    stop_callback = service[service.index("audio_engine_->OnStopKeywordDetected") :]
    stop_callback = stop_callback[
        : stop_callback.index("callbacks_.on_stop_keyword_detected(phrase_id, score);")
    ]
    assert "xEventGroupClearBits" not in stop_callback


def test_application_arms_stop_keyword_only_for_playback_with_keyword_barge_in() -> None:
    app = _added_lines(PATCH_0031, "main/application.cc")
    configure = app[app.index("void Application::ConfigureStopKeywordForSpeaking()") :]
    configure = configure[: configure.index("void Application::HandleLocalStopKeyword")]
    assert "memoria::kLocalStopKeywordEnabled && memoria_protocol != nullptr" in configure
    assert "memoria_protocol->KeywordBargeInAllowed()" in configure
    assert "audio_service_.EnableStopKeywordDetection()" in configure
    assert "audio_service_.EnableWakeWordDetection(false);" in configure
    assert app.count("ConfigureStopKeywordForSpeaking();") == 2
    # Leaving Speaking disarms stop-only mode before the next state's config.
    assert (
        "new_state != kDeviceStateSpeaking && audio_service_.IsStopKeywordDetectionEnabled()" in app
    )
    handler = app[app.index("void Application::HandleLocalStopKeyword") :]
    handler = handler[: handler.index("#endif")]
    assert "state != kDeviceStateSpeaking" in handler
    assert "memoria_protocol->NotifyLocalKeywordStop(hit)" in handler
    assert "AbortSpeaking(" not in handler
    assert "SendButtonStop" not in handler
    # The hit is handed from the AFE task to the main task.
    assert (
        "Schedule([this, phrase_id, score]() { HandleLocalStopKeyword(phrase_id, score); });" in app
    )


def test_stop_keyword_firmware_is_a_new_release_build() -> None:
    match = re.search(r"^#define MEMORIA_FIRMWARE_BUILD (\d+)$", RELEASE_HEADER, re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) >= 12
