"""Receipt acceptance requires positive controls and all server responses."""

import json

import pytest
from scripts.summarize_software_soak_r5 import summarize


@pytest.fixture
def receipt(tmp_path):
    config = {"candidate": "frozen", "dirty": "", "duration_s": 45, "edge_binary_sha256": "binary"}
    result = {
        "window_completed": True,
        "actual_window_s": 45,
        "restarts": [{"old_exit": -9}],
        "cleanup": {"all_reaped": True, "remove_exit": 0},
    }
    events = [
        {"event": "cycle_pass", "connections": 0, "leases": 0, "goroutines": 12, "latency_ms": 1},
        *(
            {"event": event}
            for event in (
                "control_positive",
                "actual_opus_downlink",
                "actual_transcript",
                "fault_tcp_recovered",
                "bridge_old_terminal",
                "fault_bridge_recovered",
            )
        ),
        {
            "event": "fault_slow_bounded_old_rejected",
            "dropped": 1,
            "max_audio_queue": 10,
            "new_generation": 2,
            "old_generation": 1,
        },
        {
            "event": "http_request",
            "method": "GET",
            "path": "/v1/devices/dev_test_01/session-state",
            "status": 401,
            "request": None,
        },
        {
            "event": "http_request",
            "method": "POST",
            "path": "/fixture/session-policy",
            "status": 403,
            "request": {"session_id": "missing-session"},
        },
        {"event": "http_request", "status": 200},
    ]
    for name, data in (("config.json", config), ("result.json", result)):
        (tmp_path / name).write_text(json.dumps(data))
    (tmp_path / "edge.log").write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in events)
    )
    (tmp_path / "bridge.log").write_text(
        "verified profile session=synthetic\nfixture authority clients after registry drain remaining=0\nold issued profile rejected session=session_1 old_epoch=1 current_epoch=2\n"
    )
    (tmp_path / "control.log").write_text(
        "\n".join(
            [
                'INFO: "GET /v1/devices/dev_test_01/session-state HTTP/1.1" 401 Unauthorized',
                'INFO: "GET /v1/devices/dev_test_01/session-state HTTP/1.1" 401 Unauthorized',
                'INFO: "POST /fixture/session-policy HTTP/1.1" 403 Forbidden',
                'INFO: "POST /fixture/session-policy HTTP/1.1" 403 Forbidden',
                'INFO: "POST /fixture/session-policy HTTP/1.1" 200 OK',
            ]
        )
    )
    samples = [
        {
            "elapsed_s": elapsed,
            "processes": {
                name: {"pid": index, "rss_bytes": 100, "fd_count": 4, "established_tcp": 1}
                for index, name in enumerate(("control", "bridge", "edge"))
            },
        }
        for elapsed in range(0, 46, 5)
    ]
    (tmp_path / "process.jsonl").write_text("\n".join(json.dumps(row) for row in samples))
    return tmp_path


def test_complete_receipt_passes(receipt):
    result = summarize(receipt)
    assert result["software_window_pass"]
    assert all(result["predicates"].values())


@pytest.mark.parametrize(
    "fault",
    [
        "dirty",
        "no_positive",
        "server500",
        "server403",
        "policy403",
        "driver403",
        "unknown",
        "missing_end",
    ],
)
def test_independent_negative_receipts_fail(receipt, fault):
    if fault == "dirty":
        config = json.loads((receipt / "config.json").read_text())
        config["dirty"] = " M services/session_runtime/service.py\n"
        (receipt / "config.json").write_text(json.dumps(config))
    elif fault == "no_positive":
        path = receipt / "edge.log"
        path.write_text(
            "\n".join(
                line
                for line in path.read_text().splitlines()
                if '"event":"control_positive"' not in line
            )
        )
    elif fault.startswith("server") or fault == "policy403":
        status = 500 if fault == "server500" else 403
        path = (
            "/fixture/session-policy"
            if fault == "policy403"
            else "/v1/devices/dev_test_01/session-state"
        )
        with (receipt / "control.log").open("a") as log:
            log.write(f'\nINFO: 127.0.0.1:1 - "POST {path} HTTP/1.1" {status} Unexpected\n')
    elif fault == "driver403":
        with (receipt / "edge.log").open("a") as log:
            log.write(
                "\n"
                + json.dumps(
                    {
                        "event": "http_request",
                        "method": "POST",
                        "path": "/fixture/start",
                        "status": 403,
                        "request": {"session_id": "valid"},
                    }
                )
                + "\n"
            )
    elif fault == "unknown":
        path = receipt / "edge.log"
        path.write_text(
            path.read_text().replace('"event":"control_positive"', '"event":"control_unknown"')
        )
    else:
        result = json.loads((receipt / "result.json").read_text())
        result["window_completed"] = False
        (receipt / "result.json").write_text(json.dumps(result))
    result = summarize(receipt)
    assert result["software_window_pass"] is False


@pytest.mark.parametrize("reason", ["PersistentSessionNotFound", "PersistentSessionDenied"])
def test_server_denial_scenario_must_match_request(receipt, reason):
    config = json.loads((receipt / "config.json").read_text())
    config["http_receipt_schema"] = "scenario-v1"
    (receipt / "config.json").write_text(json.dumps(config))
    with (receipt / "control.log").open("a") as log:
        for status in (403, 403, 200):
            log.write(
                "\n"
                + json.dumps(
                    {
                        "event": "fixture_policy_response",
                        "session_id": "missing-session" if status == 403 else "valid-session",
                        "status": status,
                        "reason": reason,
                    }
                )
            )
    assert summarize(receipt)["software_window_pass"] is (reason == "PersistentSessionNotFound")
