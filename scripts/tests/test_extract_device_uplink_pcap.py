"""Synthetic PCAP only: no sockets, SSH, device audio, or credentials."""

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
LIB = "/opt/homebrew/lib/libopus.dylib"


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

    def handshake(self, target=m.TARGET, status=101, claim=None, split=False):
        claims = {
            "device_id": claim or target,
            "client_type": "device",
            "typ": "memoria_device_media",
            "client_id": "synthetic",
            "session_id": "synthetic-session",
            "stream_epoch": 7,
        }
        token = (
            "synthetic."
            + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            + ".synthetic"
        )
        key = base64.b64encode(b"0123456789abcdef").decode()
        self.send(
            0,
            (
                f"GET /v1/device/media HTTP/1.1\r\nDevice-Id: {target}\r\n"
                f"Authorization: Bearer {token}\r\nX-Client-ID: synthetic\r\n"
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
                {
                    "type": "device.hello",
                    "version": 2,
                    "device_id": target,
                    "stream_epoch": 7,
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
                {
                    "type": "session.accepted",
                    "version": 2,
                    "session_id": "synthetic-session",
                    "stream_epoch": 7,
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


class ExtractTest(unittest.TestCase):
    def parse(self, fixture, raw=None, downlink=False):
        capture = m.Capture(LIB, downlink)
        self.addCleanup(capture.close)
        return capture.read(io.BytesIO(raw if raw is not None else fixture.pcap()))

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
                        [sys.executable, str(SCRIPT), "--output", str(output), "--libopus", LIB],
                        input=f.pcap(),
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertFalse(output.exists())
                    self.assertNotIn(b"synthetic", result.stderr)

    def test_rejects_syn_at_sequence_wrap_boundary(self):
        with self.assertRaises(m.Invalid):
            m.TCP(2**32 - 1, lambda *_: None)

    def test_reject_invalid_evidence(self):
        cases = []
        for opts in ({"target": "other"}, {"status": 401}, {"claim": "other"}):
            f = Fixture().handshake(**opts)
            f.send(0, ws(audio(), 2))
            cases.append(f)
        for payload in (
            audio(1),
            audio(sample=320),
            audio(direction=2),
            audio(epoch=8),
            audio()[:-1],
        ):
            f = Fixture().handshake()
            f.send(0, ws(payload, 2))
            cases.append(f)
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2, masked=False))
        cases.append(f)
        f = Fixture().handshake()
        f.packet(0, f.seq[0] + 5, ws(audio(), 2))
        cases.append(f)
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        f.packet(1, f.seq[1], b"", flags=16, ack=f.seq[0] + 100)
        cases.append(f)
        for i, f in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(m.Invalid):
                self.parse(f)

    def test_truncated_unsupported_no_handshake(self):
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        for raw in (f.pcap()[:-1], f.pcap(link=113), f.pcap()[:40], b""):
            with self.subTest(size=len(raw)), self.assertRaises(m.Invalid):
                self.parse(f, raw)
        g = Fixture()
        g.send(0, ws(audio(), 2))
        with self.assertRaises(m.Invalid):
            self.parse(g)

    def test_bounds_and_conflicting_reassembly(self):
        delivered = []
        tcp = m.TCP(100, lambda data, stamp: delivered.append(data))
        tcp.feed(110, b"abc", 1)
        with self.assertRaises(m.Invalid):
            tcp.feed(111, b"ZZ", 2)
        with self.assertRaises(m.Invalid):
            m.Buffer().add(b"x" * (m.LIMIT + 1), 1)
        capture = m.Capture(LIB)
        for port in range(40000, 40016):
            f = Fixture()
            f.packet(0, 1000, b"", flags=2, port=port)
            capture.packet(f.packets[-1], 1)
        f.packet(0, 1000, b"", flags=2, port=40016)
        with self.assertRaises(m.Invalid):
            capture.packet(f.packets[-1], 1)

    def test_ws_lengths_control_and_incomplete(self):
        messages = []
        socket = m.WS(True, lambda *args: messages.append(args))
        wire = ws(b"a" * 126)
        for byte in wire:
            socket.feed(bytes([byte]), 1)
        self.assertEqual(messages[0][1], b"a" * 126)
        for wire in (
            ws(b"x", 9, fin=False),
            ws(b"x" * 126, 9),
            ws(b"x", 0),
            b"\x82\xff" + struct.pack("!Q", 65536),
        ):
            with self.assertRaises(m.Invalid):
                m.WS(True, lambda *args: None).feed(wire, 1)
        f = Fixture().handshake()
        f.send(0, ws(audio(), 2))
        f.send(0, ws(audio(1, 320), 2)[:-1])
        with self.assertRaises(m.Invalid):
            self.parse(f)

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
                [sys.executable, str(SCRIPT), "--output", str(output)],
                input=target.pcap(),
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(p.name for p in output.iterdir()), {"uplink.wav", "metadata.json"})

    def test_cli_no_secret_errors_or_partial_files(self):
        f = Fixture().handshake(claim="secret-must-not-leak")
        f.send(0, ws(audio(), 2))
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "capture"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--output", str(out)],
                input=f.pcap(),
                capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(out.exists())
            self.assertNotIn(b"secret", result.stderr)
            self.assertNotIn(b"Bearer", result.stderr)


def control(kind, generation=1, **extra):
    return {
        "type": kind,
        "version": 2,
        "session_id": "synthetic-session",
        "stream_epoch": 7,
        "control_sequence": 1,
        "server_monotonic_ms": 123,
        "fence": {"session_epoch": 7, "turn_id": 1, "generation_id": generation, "tool_epoch": 0},
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


def encoded_fixture(count=4):
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
    encoder = lib.opus_encoder_create(16000, 1, 2049, ctypes.byref(error))
    assert encoder and error.value == 0
    packets = []
    try:
        for frame in range(count):
            pcm = (ctypes.c_int16 * 320)(
                *(int(9000 * math.sin((frame * 320 + i) * 0.071)) for i in range(320))
            )
            out = ctypes.create_string_buffer(2048)
            size = lib.opus_encode(encoder, pcm, 320, out, len(out))
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

    def test_reject_downlink_wire_and_boundary_errors(self):
        packet = encoded_fixture(1)[0]
        for changes in (
            {"seq": 1},
            {"sample": 320},
            {"generation": 2},
            {"flags": 1},
            {"flags": 2},
            {"epoch": 8},
            {"count": 480},
            {"size": 2049},
            {"direction": 1},
            {"version": 2},
        ):
            with self.subTest(changes=changes):
                f = self.fixture()
                self.send_control(f, "generation.started")
                f.send(1, ws(down_audio(packet, **changes), 2, False))
                with self.assertRaises(m.Invalid):
                    self.parse(f, downlink=True)
        for start, terminal, payload in (
            (False, None, packet),
            (True, "generation.completed", packet),
            (True, None, b"\x03"),
        ):
            f = self.fixture()
            if start:
                self.send_control(f, "generation.started")
            if terminal:
                self.send_control(f, terminal)
            f.send(1, ws(down_audio(payload), 2, False))
            with self.assertRaises(m.Invalid):
                self.parse(f, downlink=True)
        for extra in (
            {"session_id": "other"},
            {"stream_epoch": 8},
            {"fence": {"generation_id": True}},
        ):
            f = self.fixture()
            self.send_control(f, "generation.started", **extra)
            with self.assertRaises(m.Invalid):
                self.parse(f, downlink=True)

    def test_aggregate_pcm_and_metadata_limits(self):
        packet = encoded_fixture(1)[0]
        f = self.fixture()
        for generation in (1, 2):
            self.send_control(f, "generation.started", generation)
            f.send(1, ws(down_audio(packet, generation=generation), 2, False))
            self.send_control(f, "generation.completed", generation)
        with patch.object(m, "MAX_DOWNLINK_PCM", 640), self.assertRaises(m.Invalid):
            self.parse(f, downlink=True)
        self.assertEqual(self.parse(f, downlink=True).downlink.total_pcm, 1280)
        for limit in ("MAX_CONTROLS", "MAX_GENERATIONS"):
            with patch.object(m, limit, 1), self.assertRaises(m.Invalid):
                self.parse(f, downlink=True)

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
        with self.assertRaises(m.Invalid):
            self.parse(f, downlink=True)

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
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output",
                    str(out),
                    "--include-downlink",
                    "--libopus",
                    LIB,
                ],
                input=f.pcap(),
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            meta = json.loads((out / "metadata.json").read_text())
            self.assertEqual(len(meta["downlink"]["generations"]), 1)
            self.assertNotIn("PRIVATE", json.dumps(meta))

    def test_downlink_gap_and_incomplete_eof(self):
        for tcp_gap in (True, False):
            f = self.fixture()
            self.send_control(f, "generation.started")
            wire = ws(down_audio(encoded_fixture(1)[0]), 2, False)
            if tcp_gap:
                f.packet(1, f.seq[1] + 1, wire)
            else:
                f.send(1, wire[:-1])
            with self.assertRaises(m.Invalid):
                self.parse(f, downlink=True)


if __name__ == "__main__":
    unittest.main()
