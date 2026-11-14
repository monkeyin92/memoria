"""Synthetic PCAP only: no sockets, SSH, device audio, or credentials."""

import base64
import importlib.util
import io
import json
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

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
                },
                masked=False,
            ),
            split,
        )
        return self

    def pcap(self, link=1):
        result = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, link)
        for i, packet in enumerate(self.packets):
            result += struct.pack("<IIII", 1700000000, i * 1000, len(packet), len(packet)) + packet
        return result


class ExtractTest(unittest.TestCase):
    def parse(self, fixture, raw=None):
        capture = m.Capture(LIB)
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


if __name__ == "__main__":
    unittest.main()
