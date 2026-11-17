"""Synthetic PCAP only: no sockets, SSH, device audio, or credentials."""

import ast
import base64
import ctypes
import importlib.util
import io
import json
import math
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "extract_device_uplink_pcap.py"
spec = importlib.util.spec_from_file_location("extractor", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
LIB = m.find_libopus()
if not LIB:
    raise RuntimeError("libopus is required for the real codec regression tests")


def cli_command(*args, platform="darwin"):
    # Synthetic PCAP only: exercise the supported-host path on Linux CI too.
    bootstrap = (
        "import ctypes.util,runpy,sys; "
        f"sys.platform={platform!r}; "
        "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    return [sys.executable, "-c", bootstrap, str(SCRIPT), "--libopus", LIB, *args]


def ws(payload, op=1, masked=True, fin=True):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    key = b"ABCD"
    n = len(payload)
    header = bytes([(128 if fin else 0) | op, (128 if masked else 0) | (n if n < 126 else 126)])
    if n >= 126:
        header += struct.pack("!H", n)
    if masked:
        return header + key + bytes(c ^ key[i % 4] for i, c in enumerate(payload))
    return header + payload


def audio(seq=0, sample=0, direction=1, epoch=7):
    # RFC Opus silence packet: a real decoder must produce 320 samples at 16 kHz.
    opus = b"\xf8\xff\xfe"
    return struct.pack("!BBHIIQIIH", 1, direction, 0, epoch, seq, sample, 320, 0, len(opus)) + opus


class Fixture:
    def __init__(self):
        self.packets = []
        self.seq = [1001, 2001]
        self.packet(0, 1000, b"", flags=2)
        self.packet(1, 2000, b"", flags=18)

    def packet(self, d, seq, payload, flags=24, ack=0, port=40000):
        tcp = (
            struct.pack(
                "!HHIIBBHHH",
                8794 if d else port,
                port if d else 8794,
                seq,
                ack,
                80,
                flags,
                65535,
                0,
                0,
            )
            + payload
        )
        ip = (
            struct.pack(
                "!BBHHHBBH4s4s", 69, 0, 20 + len(tcp), 0, 0, 64, 6, 0, b"\x7f\0\0\1", b"\x7f\0\0\1"
            )
            + tcp
        )
        self.packets.append(b"\0" * 12 + b"\x08\x00" + ip)

    def send(self, d, data, split=False):
        if split:
            # Out of order, overlapping identical retransmission, then missing prefix.
            start = self.seq[d]
            self.packet(d, start + 7, data[7:])
            self.packet(d, start + 7, data[7:])
            self.packet(d, start, data[:10])
        else:
            self.packet(d, self.seq[d], data)
        self.seq[d] += len(data)

    def handshake(
        self,
        target=m.TARGET,
        status=101,
        claim=None,
        split=False,
        epoch=7,
        hello=None,
        accepted=None,
    ):
        claims = {
            "device_id": claim or target,
            "client_type": "device",
            "typ": "memoria_device_media",
            "client_id": "synthetic",
            "session_id": "synthetic-session",
            "stream_epoch": epoch,
        }
        self.token = (
            "synthetic."
            + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            + ".synthetic"
        )
        key = base64.b64encode(b"0123456789abcdef").decode()
        self.send(
            0,
            (
                f"GET /v1/device/media HTTP/1.1\r\nDevice-Id: {target}\r\n"
                f"Authorization: Bearer {self.token}\r\nX-Client-ID: synthetic\r\n"
                "Protocol-Version: 2\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\nUpgrade: websocket\r\nConnection: upgrade\r\n\r\n"
            ).encode(),
            split,
        )
        accept = base64.b64encode(
            m.hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.send(
            1,
            (
                f"HTTP/1.1 {status} Result\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode(),
            split,
        )
        self.send(
            0,
            ws(
                hello
                or {
                    "type": "device.hello",
                    "version": 2,
                    "device_id": target,
                    "stream_epoch": epoch,
                    "firmware_version": "0.2.0",
                    "board_profile": "memoria-atk-dnesp32s3-v1",
                    "capabilities": {
                        "aec_mode": "fd_low_cost",
                        "aec_reference": "software_post_gain_pre_i2s",
                        "playback_watermark": "exact",
                    },
                    "audio": {
                        "downlink_sample_rates": [16000, 24000],
                        "uplink_codec": "opus",
                        "uplink_sample_rate": 16000,
                        "channels": 1,
                        "frame_ms": 20,
                    },
                }
            ),
            split,
        )
        self.send(
            1,
            ws(
                accepted
                or {
                    "type": "session.accepted",
                    "version": 2,
                    "session_id": "synthetic-session",
                    "stream_epoch": epoch,
                    "interaction_authority": "python_authoritative",
                    "downlink_sample_rate": 16000,
                    "current_fence": None,
                    "current_generation_active": False,
                },
                masked=False,
            ),
            split,
        )
        return self

    def pcap(self, link=1):
        result = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, link)
        for i, packet in enumerate(self.packets):
            result += (
                struct.pack(
                    "<IIII", 1700000000 + i // 1000, (i % 1000) * 1000, len(packet), len(packet)
                )
                + packet
            )
        return result


COUNT_KEYS = {
    "pcap_records",
    "pcap_bytes",
    "capture_span_ms",
    "flows_opened",
    "flows_ignored_non_target",
    "target_upgrade_refused",
    "target_handshakes",
    "preexisting_flow_packets",
    "preexisting_flow_payload_bytes",
    "target_hello",
    "target_accepted",
    "accepted_downlink_sample_rate",
    "stream_epoch",
    "uplink_frames",
    "ws_text_messages",
    "ws_binary_messages",
    "ws_control_frames",
    "downlink_controls",
    "downlink_controls_skipped",
    "downlink_generations",
    "downlink_frames",
    "tcp_pending_segments_up",
    "tcp_pending_segments_down",
    "ws_buffered_bytes_up",
    "ws_buffered_bytes_down",
}

REASON_CLASSES = {
    "capture_boundary",
    "capture_integrity",
    "protocol_mismatch",
    "session_lifecycle",
    "bounds",
    "environment",
}


def cli_report(result, marker):
    for stream in (result.stdout, result.stderr):
        for line in stream.decode().splitlines():
            if line.startswith(marker + " "):
                return json.loads(line.split(" ", 1)[1])
    return None


class ExtractTest(unittest.TestCase):
    def parse(self, fixture, raw=None, downlink=False):
        capture = self.capture = m.Capture(LIB, downlink)
        self.addCleanup(capture.close)
        return capture.read(io.BytesIO(raw if raw is not None else fixture.pcap()))

    def reject(self, fixture, reason, raw=None, downlink=False):
        with self.assertRaises(m.Invalid) as cm:
            self.parse(fixture, raw, downlink)
        self.assertEqual(cm.exception.reason, reason)

    def report_counts(self):
        return json.loads(m.report(self.capture, "internal_error"))["counts"]

    def assert_report_shape(self, payload):
        self.assertLessEqual(
            set(payload), {"schema", "reason", "class", "exception_type", "counts"}
        )
        self.assertEqual(set(payload["counts"]), COUNT_KEYS)
        for value in payload["counts"].values():
            self.assertTrue(value is None or type(value) is int)

    def assert_streams_clean(self, result, token):
        for blob in (result.stdout, result.stderr):
            for forbidden in (
                "synthetic-session",
                "Bearer",
                "synthetic",
                "PRIVATE",
                m.TARGET,
                token,
            ):
                self.assertNotIn(forbidden.encode(), blob)

    def test_real_opus_split_retransmission_and_private_output(self):
        f = Fixture().handshake(split=True)
        f.send(0, ws(audio(), op=2), split=True)
        f.send(0, ws(audio(1, 320), op=2))
        flow = self.parse(f)
        self.assertEqual(len(flow.pcm), 1280)
        self.assertEqual(flow.frames[1]["sample_end"], 640)
        self.assertLess(flow.frames[0]["packet_time_min_ns"], flow.frames[0]["packet_time_max_ns"])
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "evidence"
            m.save(flow, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for p in output.iterdir():
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            with wave.open(str(output / "uplink.wav")) as wav:
                self.assertEqual((wav.getframerate(), wav.getnframes()), (16000, 640))
            metadata = (output / "metadata.json").read_text()
            for forbidden in ("synthetic", "Authorization", "Bearer", "device.hello"):
                self.assertNotIn(forbidden, metadata)
            self.assertEqual(json.loads(metadata)["capture_tail"], "unknown")
            with self.assertRaises(FileExistsError):
                m.save(flow, output)

    def test_fragment_ping_downlink_and_close(self):
        f = Fixture().handshake()
        wire = audio()
        f.send(0, ws(wire[:15], 2, fin=False) + ws(b"ping", 9) + ws(wire[15:], 0))
        f.send(1, ws(audio(direction=2), 2, masked=False))
        f.send(0, ws(b"", 8))
        self.assertEqual(len(self.parse(f).frames), 1)

    def test_rejects_sequence_wrap_at_end_of_capture(self):
        for fin in (False, True):
            with self.subTest(fin=fin):
                f = Fixture().handshake()
                f.send(0, ws(audio(), 2))
                shift = 2**32 - f.seq[0] + (-1 if fin else 10)
                if fin:
                    f.packet(0, f.seq[0], b"", flags=17)
                packets = []
                for packet in f.packets:
                    packet = bytearray(packet)
                    if struct.unpack("!H", packet[36:38])[0] == 8794:
                        seq = struct.unpack("!I", packet[38:42])[0]
                        struct.pack_into("!I", packet, 38, seq + shift)
                    packets.append(bytes(packet))
                f.packets = packets
                with tempfile.TemporaryDirectory() as root:
                    output = Path(root) / "evidence"
                    result = subprocess.run(
                        cli_command("--output", str(output)),
                        input=f.pcap(),
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertFalse(output.exists())
                    self.assertNotIn(b"synthetic", result.stderr)
                    rejection = cli_report(result, "extractor_rejection")
                    self.assertEqual(rejection["reason"], "tcp_sequence_wrap")

    def test_rejects_syn_at_sequence_wrap_boundary(self):
        with self.assertRaises(m.Invalid) as cm:
            m.TCP(2**32 - 1, lambda *_: None)
        self.assertEqual(cm.exception.reason, "tcp_sequence_wrap")

    def test_reject_invalid_evidence(self):
        cases = []
        for opts, reason in (
            ({"target": "other"}, "target_handshake_not_found"),
            ({"status": 401}, "target_handshake_not_found"),
            ({"claim": "other"}, "target_jwt_claims_mismatch"),
        ):
            f = Fixture().handshake(**opts)
            f.send(0, ws(audio(), 2))
            cases.append((f, reason))
        for payload, reason in (
            (audio(1), "uplink_audio_first_frame_discontinuous"),
            (audio(sample=320), "uplink_audio_first_frame_discontinuous"),
            (audio(direction=2), "uplink_audio_header_invalid"),
            (audio(epoch=8), "uplink_audio_header_invalid"),
            (audio()[:-1], "uplink_audio_header_invalid"),
        ):
            f = Fixture().handshake()
            f.send(0, ws(payload, 2))
            cases.append((f, reason))
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2, masked=False))
        cases.append((f, "websocket_mask_invalid"))
        # The gap-starved audio leaves zero uplink frames at EOF; the absent-audio
        # check is evaluated before the TCP-pending check, so this surfaces as
        # uplink_audio_absent rather than tcp_reassembly_gap.
        f = Fixture().handshake()
        f.packet(0, f.seq[0] + 5, ws(audio(), 2))
        cases.append((f, "uplink_audio_absent"))
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        f.packet(1, f.seq[1], b"", flags=16, ack=f.seq[0] + 100)
        cases.append((f, "tcp_ack_beyond_capture"))
        for i, (f, reason) in enumerate(cases):
            with self.subTest(case=i):
                self.reject(f, reason)

    def test_truncated_unsupported_no_handshake(self):
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        for raw, reason in (
            (f.pcap()[:-1], "pcap_record_truncated"),
            (f.pcap(link=113), "pcap_link_unsupported"),
            (f.pcap()[:40], "pcap_record_truncated"),
            (b"", "pcap_header_invalid"),
        ):
            with self.subTest(size=len(raw)):
                self.reject(f, reason, raw)
        g = Fixture()
        g.send(0, ws(audio(), 2))
        self.reject(g, "target_handshake_not_found")

    def test_bounds_and_conflicting_reassembly(self):
        delivered = []
        tcp = m.TCP(100, lambda data, stamp: delivered.append(data))
        tcp.feed(110, b"abc", 1)
        with self.assertRaises(m.Invalid) as cm:
            tcp.feed(111, b"ZZ", 2)
        self.assertEqual(cm.exception.reason, "tcp_retransmission_conflict")
        with self.assertRaises(m.Invalid) as cm:
            m.Buffer().add(b"x" * (m.LIMIT + 1), 1)
        self.assertEqual(cm.exception.reason, "parser_buffer_limit")
        capture = m.Capture(LIB)
        for port in range(40000, 40016):
            f = Fixture()
            f.packet(0, 1000, b"", flags=2, port=port)
            capture.packet(f.packets[-1], 1)
        f.packet(0, 1000, b"", flags=2, port=40016)
        with self.assertRaises(m.Invalid) as cm:
            capture.packet(f.packets[-1], 1)
        self.assertEqual(cm.exception.reason, "flow_limit_exceeded")

    def test_ws_lengths_control_and_incomplete(self):
        messages = []
        socket = m.WS(True, lambda *args: messages.append(args))
        wire = ws(b"a" * 126)
        for byte in wire:
            socket.feed(bytes([byte]), 1)
        self.assertEqual(messages[0][1], b"a" * 126)
        for wire, reason in (
            (ws(b"x", 9, fin=False), "websocket_control_frame_invalid"),
            (ws(b"x" * 126, 9), "websocket_control_frame_invalid"),
            (ws(b"x", 0), "websocket_fragment_invalid"),
            (b"\x82\xff" + struct.pack("!Q", 65536), "websocket_frame_too_large"),
        ):
            with self.subTest(reason=reason), self.assertRaises(m.Invalid) as cm:
                m.WS(True, lambda *args: None).feed(wire, 1)
            self.assertEqual(cm.exception.reason, reason)
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        f.send(0, ws(audio(1, 320), 2)[:-1])
        self.reject(f, "websocket_frame_incomplete")
        self.assertGreater(self.report_counts()["ws_buffered_bytes_up"], 0)

    def test_non_target_flow_dropped_and_success_cli(self):
        other = Fixture().handshake(target="other")
        target = Fixture().handshake()
        target.send(0, ws(audio(), 2))
        # Different client port, same trusted Edge port.
        remapped = []
        for raw in other.packets:
            raw = bytearray(raw)
            offset = 34 if int.from_bytes(raw[34:36], "big") == 40000 else 36
            raw[offset : offset + 2] = struct.pack("!H", 40001)
            remapped.append(bytes(raw))
        target.packets = remapped + target.packets
        self.assertEqual(len(self.parse(target).frames), 1)
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "evidence"
            result = subprocess.run(
                cli_command("--output", str(output)),
                input=target.pcap(),
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(p.name for p in output.iterdir()), {"uplink.wav", "metadata.json"})
            summary = cli_report(result, "extractor_summary")
            self.assert_report_shape(summary)
            self.assertIsNone(summary["reason"])
            self.assert_streams_clean(result, target.token)

    def test_cli_no_secret_errors_or_partial_files(self):
        f = Fixture().handshake(claim="secret-must-not-leak")
        f.send(0, ws(audio(), 2))
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "capture"
            result = subprocess.run(
                cli_command("--output", str(out)),
                input=f.pcap(),
                capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(out.exists())
            self.assertNotIn(b"secret", result.stderr)
            self.assertNotIn(b"Bearer", result.stderr)
            rejection = cli_report(result, "extractor_rejection")
            self.assertEqual(rejection["reason"], "target_jwt_claims_mismatch")
            self.assert_report_shape(rejection)
            self.assert_streams_clean(result, f.token)


def control(kind, generation=1, epoch=7, **extra):
    return {
        "type": kind,
        "version": 2,
        "session_id": "synthetic-session",
        "stream_epoch": epoch,
        "control_sequence": 1,
        "server_monotonic_ms": 123,
        "fence": {
            "session_epoch": epoch,
            "turn_id": 1,
            "generation_id": generation,
            "tool_epoch": 0,
        },
        "reason": "PRIVATE TEXT",
        **extra,
    }


def down_audio(packet, seq=0, generation=1, **changes):
    values = dict(
        version=1,
        direction=2,
        flags=0,
        epoch=7,
        seq=seq,
        sample=seq * 320,
        count=320,
        generation=generation,
        size=len(packet),
    )
    values.update(changes)
    return struct.pack("!BBHIIQIIH", *values.values()) + packet


def encoded_fixture(count=4, rate=16000):
    lib = ctypes.CDLL(LIB)
    lib.opus_encoder_create.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    error = ctypes.c_int()
    encoder = lib.opus_encoder_create(rate, 1, 2049, ctypes.byref(error))
    assert encoder and error.value == 0
    frame_samples = rate // 50  # production 20 ms frames
    packets = []
    try:
        for frame in range(count):
            pcm = (ctypes.c_int16 * frame_samples)(
                *(
                    int(9000 * math.sin((frame * frame_samples + i) * 0.071))
                    for i in range(frame_samples)
                )
            )
            out = ctypes.create_string_buffer(2048)
            size = lib.opus_encode(encoder, pcm, frame_samples, out, len(out))
            assert size > 0
            packets.append(out.raw[:size])
    finally:
        lib.opus_encoder_destroy(encoder)
    return packets


def reference_decode(packets):
    # Independent direct sequential libopus calls, without extractor wrappers.
    lib = ctypes.CDLL(LIB)
    lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_decoder_create.restype = ctypes.c_void_p
    lib.opus_decode.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    error = ctypes.c_int()
    decoder = lib.opus_decoder_create(16000, 1, ctypes.byref(error))
    assert decoder and not error.value
    result = []
    try:
        for packet in packets:
            pcm = (ctypes.c_int16 * 1920)()
            n = lib.opus_decode(decoder, packet, len(packet), pcm, 1920, 0)
            assert n == 320
            result.extend(pcm[:n])
    finally:
        lib.opus_decoder_destroy(decoder)
    return result


class DownlinkTest(unittest.TestCase):
    parse = ExtractTest.parse
    reject = ExtractTest.reject
    report_counts = ExtractTest.report_counts
    assert_report_shape = ExtractTest.assert_report_shape
    assert_streams_clean = ExtractTest.assert_streams_clean

    def fixture(self):
        f = Fixture().handshake(split=True)
        f.send(0, ws(audio(), 2))
        return f

    def send_control(self, f, kind, generation=1, **extra):
        f.send(1, ws(control(kind, generation, **extra), masked=False), split=True)

    def test_multigeneration_reference_and_allowlist(self):
        packets = encoded_fixture()
        f = self.fixture()
        for generation in (1, 3, 4):
            self.send_control(f, "generation.started", generation)
            for seq, packet in enumerate(packets):
                wire = down_audio(packet, seq, generation)
                f.send(
                    1,
                    ws(wire[:12], 2, False, False)
                    + ws(b"ping", 9, False)
                    + ws(wire[12:], 0, False),
                    split=True,
                )
            if generation == 1:
                self.send_control(f, "generation.cancelled", 2)
                self.send_control(f, "playback.flush", 2, replacement_generation_id=2)
            elif generation == 3:
                self.send_control(f, "generation.completed", 3)
        f.send(1, ws({"type": "screen.subtitle", "text": "PRIVATE TEXT"}, masked=False))
        flow = self.parse(f, downlink=True)
        self.assertEqual(flow.pcm, self.parse(f).pcm)
        self.assertEqual(
            [g["status"] for g in flow.downlink.generations],
            ["cancelled_prefix", "completion_observed", "prefix_only"],
        )
        for g in flow.downlink.generations:
            self.assertEqual(list(struct.unpack("<1280h", g["pcm"])), reference_decode(packets))
            self.assertEqual(g["frames"][0]["sequence"], 0)
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "capture"
            m.save(flow, out)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
            for path in out.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            metadata = (out / "metadata.json").read_text()
            for secret in ("PRIVATE", "synthetic", "reason", "session_id", "subtitle"):
                self.assertNotIn(secret, metadata)
            for i in range(3):
                with wave.open(str(out / f"downlink-{i:03d}.wav")) as wav:
                    self.assertEqual((wav.getframerate(), wav.getnchannels()), (16000, 1))
                    self.assertEqual(wav.readframes(1280), flow.downlink.generations[i]["pcm"])

    def test_cross_turn_generation_id_must_be_unique(self):
        packet = encoded_fixture(1)[0]
        for generation, reason in (
            (1, "downlink_generation_reused"),
            (2, "downlink_audio_generation_mismatch"),
        ):
            f = self.fixture()
            self.send_control(f, "generation.started", 1)
            fence = {"session_epoch": 7, "turn_id": 2, "generation_id": generation, "tool_epoch": 0}
            self.send_control(f, "generation.started", generation, fence=fence)
            f.send(1, ws(down_audio(packet, generation=1), 2, masked=False))
            with self.subTest(replacement=generation):
                self.reject(f, reason, downlink=True)
        f = self.fixture()
        self.send_control(f, "generation.started", 1)
        self.send_control(
            f,
            "generation.started",
            2,
            fence={
                "session_epoch": 7,
                "turn_id": 2,
                "generation_id": 2,
                "tool_epoch": 0,
            },
        )
        f.send(1, ws(down_audio(packet, generation=2), 2, masked=False))
        flow = self.parse(f, downlink=True)
        self.assertEqual(len(flow.downlink.generations[1]["frames"]), 1)
        self.assertEqual(
            list(struct.unpack("<320h", flow.downlink.generations[1]["pcm"])),
            reference_decode([packet]),
        )

    def test_reject_downlink_wire_and_boundary_errors(self):
        packet = encoded_fixture(1)[0]
        for changes, reason in (
            ({"seq": 1}, "downlink_audio_first_frame_discontinuous"),
            ({"sample": 320}, "downlink_audio_first_frame_discontinuous"),
            ({"generation": 2}, "downlink_audio_generation_mismatch"),
            ({"flags": 1}, "downlink_audio_header_invalid"),
            ({"flags": 2}, "downlink_audio_header_invalid"),
            ({"epoch": 8}, "downlink_audio_header_invalid"),
            ({"count": 480}, "downlink_audio_header_invalid"),
            ({"size": 2049}, "downlink_audio_header_invalid"),
            ({"direction": 1}, "downlink_audio_header_invalid"),
            ({"version": 2}, "downlink_audio_header_invalid"),
        ):
            with self.subTest(changes=changes):
                f = self.fixture()
                self.send_control(f, "generation.started")
                f.send(1, ws(down_audio(packet, **changes), 2, False))
                self.reject(f, reason, downlink=True)
        for start, terminal, payload, reason in (
            (False, None, packet, "downlink_generation_start_missing"),
            (True, "generation.completed", packet, "downlink_audio_generation_mismatch"),
            # down_audio() wraps the 1-byte payload in a valid 31-byte header,
            # so this reaches the codec and fails as opus_decode_failed.
            (True, None, b"\x03", "opus_decode_failed"),
        ):
            f = self.fixture()
            if start:
                self.send_control(f, "generation.started")
            if terminal:
                self.send_control(f, terminal)
            f.send(1, ws(down_audio(payload), 2, False))
            self.reject(f, reason, downlink=True)
        for extra, reason in (
            ({"session_id": "other"}, "downlink_control_invalid"),
            ({"stream_epoch": 8}, "downlink_control_invalid"),
            ({"fence": {"generation_id": True}}, "downlink_fence_invalid"),
        ):
            f = self.fixture()
            self.send_control(f, "generation.started", **extra)
            self.reject(f, reason, downlink=True)

    def test_aggregate_pcm_and_metadata_limits(self):
        packet = encoded_fixture(1)[0]
        f = self.fixture()
        for generation in (1, 2):
            self.send_control(f, "generation.started", generation)
            f.send(1, ws(down_audio(packet, generation=generation), 2, False))
            self.send_control(f, "generation.completed", generation)
        with patch.object(m, "MAX_DOWNLINK_PCM", 640):
            self.reject(f, "downlink_pcm_limit", downlink=True)
        self.assertEqual(self.parse(f, downlink=True).downlink.total_pcm, 1280)
        for limit, reason in (
            ("MAX_CONTROLS", "downlink_control_limit"),
            ("MAX_GENERATIONS", "downlink_generation_limit"),
        ):
            with patch.object(m, limit, 1):
                self.reject(f, reason, downlink=True)

    def test_full_message_availability_waits_for_tcp_prefix(self):
        f = self.fixture()
        self.send_control(f, "generation.started")
        packet = encoded_fixture(1)[0]
        wire = ws(down_audio(packet), 2, False) + ws(down_audio(packet, 1), 2, False)
        f.send(1, wire, split=True)
        expected = 1700000000 * 10**9 + (len(f.packets) - 1) * 1000000
        flow = self.parse(f, downlink=True)
        self.assertEqual(
            [x["full_message_available_ns"] for x in flow.downlink.current["frames"]],
            [expected, expected],
        )

    def test_actual_four_mib_limit_across_generations(self):
        packet = encoded_fixture(1)[0]
        f = self.fixture()
        self.send_control(f, "generation.started")
        for seq in range(4000):
            f.send(1, ws(down_audio(packet, seq), 2, False))
        self.send_control(f, "generation.completed")
        self.send_control(f, "generation.started", 2)
        remaining = m.MAX_DOWNLINK_PCM // 640 - 4000
        for seq in range(remaining):
            f.send(1, ws(down_audio(packet, seq, 2), 2, False))
        self.assertEqual(self.parse(f, downlink=True).downlink.total_pcm, (4000 + remaining) * 640)
        f.send(1, ws(down_audio(packet, remaining, 2), 2, False))
        self.reject(f, "downlink_pcm_limit", downlink=True)

    def test_flush_replacement_is_authoritative_and_requires_start(self):
        f = self.fixture()
        self.send_control(f, "generation.started")
        self.send_control(f, "playback.flush", 9, replacement_generation_id=9)
        self.send_control(f, "generation.started", 9)
        f.send(1, ws(down_audio(encoded_fixture(1)[0], generation=9), 2, False))
        flow = self.parse(f, downlink=True)
        self.assertEqual(flow.downlink.current["fence"]["generation_id"], 9)
        self.assertEqual(flow.downlink.generations[0]["status"], "cancelled_prefix")

    def test_wrong_device_excluded_and_cli_option(self):
        other = Fixture().handshake(target="other")
        other.send(1, ws(control("generation.started", 88), masked=False))
        other.send(1, ws(b"PRIVATE TEXT", 2, False))
        f = self.fixture()
        self.send_control(f, "generation.started")
        f.send(1, ws(down_audio(encoded_fixture(1)[0]), 2, False))
        remapped = []
        for raw in other.packets:
            raw = bytearray(raw)
            offset = 34 if int.from_bytes(raw[34:36], "big") == 40000 else 36
            raw[offset : offset + 2] = struct.pack("!H", 40001)
            remapped.append(bytes(raw))
        f.packets = remapped + f.packets
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "capture"
            result = subprocess.run(
                cli_command("--output", str(out), "--include-downlink"),
                input=f.pcap(),
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            meta = json.loads((out / "metadata.json").read_text())
            self.assertEqual(len(meta["downlink"]["generations"]), 1)
            self.assertNotIn("PRIVATE", json.dumps(meta))
            summary = cli_report(result, "extractor_summary")
            self.assert_report_shape(summary)
            self.assert_streams_clean(result, f.token)

    def test_downlink_gap_and_incomplete_eof(self):
        for tcp_gap, reason in (
            (True, "tcp_reassembly_gap"),
            (False, "websocket_frame_incomplete"),
        ):
            f = self.fixture()
            self.send_control(f, "generation.started")
            wire = ws(down_audio(encoded_fixture(1)[0]), 2, False)
            if tcp_gap:
                f.packet(1, f.seq[1] + 1, wire)
            else:
                f.send(1, wire[:-1])
            self.reject(f, reason, downlink=True)


if __name__ == "__main__":
    unittest.main()


def test_system_opus_library_takes_precedence():
    with patch.object(m.ctypes.util, "find_library", return_value="libopus.so.0"):
        assert m.find_libopus() == "libopus.so.0"


def test_missing_opus_library_is_explicit():
    with (
        patch.object(m.ctypes.util, "find_library", return_value=None),
        patch.object(m.Path, "is_file", return_value=False),
    ):
        assert m.find_libopus() is None


def test_non_mac_cli_still_rejects_audio_output():
    f = Fixture().handshake()
    f.send(0, ws(audio(), 2))
    with tempfile.TemporaryDirectory() as root:
        out = Path(root) / "must-not-exist"
        result = subprocess.run(
            cli_command("--output", str(out), platform="linux"), input=f.pcap(), capture_output=True
        )
        assert result.returncode == 1
        assert not out.exists()
        assert cli_report(result, "extractor_rejection")["reason"] == "unsupported_platform"


def test_reason_catalog_is_statically_complete():
    tree = ast.parse(SCRIPT.read_text(), filename=str(SCRIPT))
    # require/uint/fence_of forward the reason argument their callers already
    # validated as a REASONS literal, so their bodies are exempt from the
    # literal-arg check; the REASONS table itself is excluded from the
    # "referenced in source" literal scan.
    helper_lines = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "REASONS" for t in node.targets)
        ) or (isinstance(node, ast.FunctionDef) and node.name in {"require", "uint", "fence_of"}):
            helper_lines.update(range(node.lineno, node.end_lineno + 1))
    literals = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.lineno not in helper_lines
        ):
            literals.add(node.value)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name == "require":
            assert len(node.args) == 2 and not node.keywords
            arg = node.args[1]
            assert isinstance(arg, ast.Constant) and arg.value in m.REASONS
        elif name == "Invalid" and node.lineno not in helper_lines:
            assert len(node.args) == 1
            arg = node.args[0]
            assert isinstance(arg, ast.Constant) and arg.value in m.REASONS
    assert set(m.REASONS) <= literals
    assert set(m.REASONS.values()) == REASON_CLASSES


def real_hello(epoch=3252):
    # Exact key set emitted by MemoriaProtocol::DeviceHelloV2:
    # firmware/esp32/overlay/files/main/memoria/memoria_protocol.cc (~line 3080).
    return {
        "type": "device.hello",
        "version": 2,
        "device_id": m.TARGET,
        "firmware_version": "0.2.0",
        "board_profile": "memoria-atk-dnesp32s3-v1",
        "stream_epoch": epoch,
        "capabilities": {
            "display": True,
            "microphone": True,
            "speaker": True,
            "simultaneous_capture_playback": True,
            "aec_mode": "fd_low_cost",
            "aec_reference": "software_post_gain_pre_i2s",
            "aec_reference_verified": False,
            "local_vad": True,
            "local_stop_keyword": True,
            "physical_stop_button": True,
            "playback_watermark": "exact",
            "local_duck": False,
            "barge_in_level": 1,
        },
        "audio": {
            "uplink_codec": "opus",
            "uplink_sample_rate": 16000,
            "downlink_sample_rates": [16000, 24000],
            "channels": 1,
            "frame_ms": 20,
        },
    }


def real_accepted(epoch=3252, rate=24000):
    # Exact deviceSessionAccepted key set (services/media_edge/
    # device_ws_protocol.go:443) with a DeviceSettingsClaim snapshot
    # (device_ws_auth.go:106); wake_word_* keys are omitempty and absent here.
    return {
        "type": "session.accepted",
        "version": 2,
        "session_id": "synthetic-session",
        "stream_epoch": epoch,
        "interaction_authority": "python_authoritative",
        "audio_mode": "interrupt_assist",
        "downlink_sample_rate": rate,
        "runtime_profile_version": 3,
        "device_settings": {
            "settings_version": 1,
            "volume_limit": 70,
            "screen_brightness": 60,
            "night_mode": False,
            "do_not_disturb": False,
            "learning_mode": "off",
            "audio_mode": "interrupt_assist",
            "wake_mode": "button",
            "allowed_barge_in": ["button"],
        },
        "current_fence": None,
        "current_generation_active": False,
        "acoustic_attestation": None,
    }


def uplink_frame(seq, sample, packet, epoch=3252):
    return struct.pack("!BBHIIQIIH", 1, 1, 0, epoch, seq, sample, 320, 0, len(packet)) + packet


class RealShapeTest(unittest.TestCase):
    parse = ExtractTest.parse
    reject = ExtractTest.reject
    report_counts = ExtractTest.report_counts
    assert_report_shape = ExtractTest.assert_report_shape
    assert_streams_clean = ExtractTest.assert_streams_clean

    def real_fixture(self, epoch=3252):
        # device.hello advertises [16000, 24000] and the Edge resolves 24000
        # (TestResolveDownlinkRatePrefersNative24kForV2), so a production
        # session.accepted carries downlink_sample_rate 24000.
        f = Fixture().handshake(
            epoch=epoch, hello=real_hello(epoch), accepted=real_accepted(epoch, 24000)
        )
        for seq, packet in enumerate(encoded_fixture(50)):
            f.send(0, ws(uplink_frame(seq, seq * 320, packet, epoch), 2))
        fence = {"session_epoch": epoch, "turn_id": 1, "generation_id": 1, "tool_epoch": 0}
        f.send(1, ws(control("generation.started", epoch=epoch, fence=fence), masked=False))
        for seq, packet in enumerate(encoded_fixture(3, 24000)):
            f.send(
                1,
                ws(
                    down_audio(packet, seq, 1, epoch=epoch, count=480, sample=seq * 480),
                    2,
                    masked=False,
                ),
            )
        f.send(1, ws(control("generation.completed", epoch=epoch, fence=fence), masked=False))
        f.send(1, ws({"type": "screen.subtitle", "text": "PRIVATE TEXT"}, masked=False))
        return f

    def test_real_shape_uplink_only_succeeds_and_reports_24k(self):
        f = self.real_fixture()
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "evidence"
            result = subprocess.run(
                cli_command("--output", str(out)), input=f.pcap(), capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            meta = json.loads((out / "metadata.json").read_text())
            self.assertEqual(meta["stream_epoch"], 3252)
            self.assertEqual(len(meta["frames"]), 50)
            summary = cli_report(result, "extractor_summary")
            self.assert_report_shape(summary)
            self.assertIsNone(summary["reason"])
            self.assertIsNone(summary["class"])
            self.assertEqual(summary["counts"]["accepted_downlink_sample_rate"], 24000)
            self.assertEqual(summary["counts"]["uplink_frames"], 50)
            self.assertEqual(summary["counts"]["stream_epoch"], 3252)
            self.assert_streams_clean(result, f.token)

    def test_real_shape_24k_downlink_rejected_with_reason(self):
        f = self.real_fixture()
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "evidence"
            result = subprocess.run(
                cli_command("--output", str(out), "--include-downlink"),
                input=f.pcap(),
                capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(out.exists())
            rejection = cli_report(result, "extractor_rejection")
            self.assert_report_shape(rejection)
            self.assertEqual(rejection["reason"], "session_accepted_downlink_rate_unsupported")
            self.assertEqual(rejection["class"], "protocol_mismatch")
            counts = rejection["counts"]
            self.assertEqual(counts["target_handshakes"], 1)
            self.assertEqual(counts["target_hello"], 1)
            self.assertEqual(counts["target_accepted"], 0)
            self.assertEqual(counts["accepted_downlink_sample_rate"], 24000)
            self.assertEqual(counts["stream_epoch"], 3252)
            self.assert_streams_clean(result, f.token)

    def test_capture_starting_after_connect_is_capture_boundary(self):
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        f.packets = f.packets[2:]  # capture window opens after the TCP handshake
        self.reject(f, "target_handshake_not_found")
        self.assertEqual(m.REASONS["target_handshake_not_found"], "capture_boundary")
        self.assertEqual(self.report_counts()["flows_opened"], 0)
        self.assertGreater(self.report_counts()["preexisting_flow_packets"], 0)
        self.assertGreater(self.report_counts()["preexisting_flow_payload_bytes"], 0)

    def test_target_upgrade_refusal_is_counted(self):
        f = Fixture().handshake(status=401)
        f.send(0, ws(audio(), 2))
        self.reject(f, "target_handshake_not_found")
        self.assertEqual(self.report_counts()["target_upgrade_refused"], 1)
        self.assertEqual(self.report_counts()["target_handshakes"], 0)

    def test_missing_middle_tcp_segment_is_reassembly_gap(self):
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        f.packet(0, f.seq[0] + 5, ws(audio(1, 320), 2))
        self.reject(f, "tcp_reassembly_gap")
        self.assertEqual(m.REASONS["tcp_reassembly_gap"], "capture_integrity")
        self.assertGreater(self.report_counts()["tcp_pending_segments_up"], 0)

    def test_uplink_first_frame_and_later_discontinuity(self):
        f = Fixture().handshake()
        f.send(0, ws(audio(1), 2))
        self.reject(f, "uplink_audio_first_frame_discontinuous")
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        f.send(0, ws(audio(2, 640), 2))
        self.reject(f, "uplink_audio_discontinuous")

    def test_downlink_audio_before_any_generation_started(self):
        f = Fixture().handshake()  # session.accepted at 16000
        f.send(0, ws(audio(), 2))
        f.send(1, ws(down_audio(encoded_fixture(1)[0]), 2, masked=False))
        self.reject(f, "downlink_generation_start_missing", downlink=True)
        self.assertEqual(self.report_counts()["downlink_generations"], 0)

    def test_second_target_handshake_is_session_lifecycle(self):
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        second = Fixture().handshake()
        remapped = []
        for raw in second.packets:
            raw = bytearray(raw)
            offset = 34 if int.from_bytes(raw[34:36], "big") == 40000 else 36
            raw[offset : offset + 2] = struct.pack("!H", 40001)
            remapped.append(bytes(raw))
        f.packets += remapped
        self.reject(f, "second_target_session")
        self.assertEqual(m.REASONS["second_target_session"], "session_lifecycle")
        self.assertEqual(self.report_counts()["target_handshakes"], 1)

    def test_peer_ack_beyond_captured_bytes(self):
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        f.packet(1, f.seq[1], b"", flags=16, ack=f.seq[0] + 100)
        self.reject(f, "tcp_ack_beyond_capture")
        self.assertEqual(m.REASONS["tcp_ack_beyond_capture"], "capture_integrity")
