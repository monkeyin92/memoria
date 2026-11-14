#!/usr/bin/env python3
"""Local-only, bounded diagnostic extractor; never run on a production host.

stdin: classic pcap, EN10MB/IPv4/TCP loopback port 8794. Start before SYN.
Only one target session is allowed. No PCAP, HTTP headers, JWT or text is saved.
No packet-loss concealment: any observed gap/truncation rejects the whole run.
A clean EOF is only a complete captured prefix, never proof of capture-tail losslessness.
Hard bounds: 16 flows, 32 MiB input, 128 KiB per reassembly/parser buffer,
256 queued TCP segments/direction, 4 MiB decoded PCM, 120 seconds plus a
125-second wall alarm. One target session, starting at audio sequence/sample 0.
Unsupported link types, IP fragments, sequence wrap and reconnects fail closed.

This tool trusts the audited Edge's 101 as signature-verification evidence; it
is not a JWT verifier. Run only in the separately authorized synthetic window.
"""

import argparse
import base64
import ctypes
import hashlib
import json
import os
import signal
import struct
import sys
import time
import wave
from pathlib import Path

TARGET = "dev_atk_a4cb8fd6095c"
LIMIT = 131072
MAX_INPUT = 32 * 1024 * 1024
MAX_SECONDS = 120


class Invalid(Exception):
    pass


def require(ok):
    if not ok:
        raise Invalid("capture rejected")


def object_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    require(isinstance(value, dict))
    return value


class Buffer:
    def __init__(self):
        self.data = bytearray()
        self.times = []

    def add(self, data, stamp):
        require(len(self.data) + len(data) <= LIMIT)
        self.data.extend(data)
        self.times.extend([stamp] * len(data))

    def take(self, count):
        data = bytes(self.data[:count])
        stamps = self.times[:count]
        del self.data[:count]
        del self.times[:count]
        return data, (min(stamps), max(stamps))


class TCP:
    def __init__(self, seq, consume):
        self.next = seq + 1
        self.pending = []
        self.consume = consume

    def feed(self, seq, data, stamp):
        # Sequence wrap and ambiguous overlap fail closed; duplicates already
        # delivered are ignored. Retransmission timestamps never replace originals.
        require(abs(seq - self.next) < 2**31)
        if seq < self.next:
            data = data[self.next - seq :]
            seq = self.next
        if not data:
            return
        for start, old, _ in self.pending:
            lo, hi = max(start, seq), min(start + len(old), seq + len(data))
            if lo < hi:
                require(old[lo - start : hi - start] == data[lo - seq : hi - seq])
        self.pending.append((seq, data, stamp))
        require(len(self.pending) <= 256 and sum(len(x[1]) for x in self.pending) <= LIMIT)
        self.pending.sort(key=lambda x: x[0])
        while self.pending and self.pending[0][0] <= self.next:
            start, chunk, timestamp = self.pending.pop(0)
            chunk = chunk[max(0, self.next - start) :]
            if chunk:
                self.next += len(chunk)
                self.consume(chunk, timestamp)


def headers(buffer):
    end = buffer.data.find(b"\r\n\r\n")
    if end < 0:
        require(len(buffer.data) <= 16384)
        return None
    require(end <= 16384)
    raw, _ = buffer.take(end + 4)
    lines = raw[:-4].decode("ascii").split("\r\n")
    result = {}
    for line in lines[1:]:
        require(":" in line and not line.startswith((" ", "\t")))
        name, value = line.split(":", 1)
        name = name.lower()
        require(name not in result)
        result[name] = value.strip()
    return lines[0], result


class WS:
    def __init__(self, masked, callback):
        self.buffer = Buffer()
        self.masked = masked
        self.callback = callback
        self.fragment = bytearray()
        self.opcode = None
        self.stamps = []
        self.closed = False

    def feed(self, data, stamp):
        self.buffer.add(data, stamp)
        b = self.buffer
        while len(b.data) >= 2:
            first, second = b.data[:2]
            fin, op = bool(first & 128), first & 15
            require(not self.closed and not first & 112 and bool(second & 128) == self.masked)
            size, offset = second & 127, 2
            if size in (126, 127):
                width = 2 if size == 126 else 8
                if len(b.data) < 2 + width:
                    return
                size = int.from_bytes(b.data[2 : 2 + width], "big")
                require(size >= (126 if width == 2 else 65536))
                offset += width
            require(size <= 16384 and op in (0, 1, 2, 8, 9, 10))
            if op >= 8:
                require(fin and size <= 125 and not (op == 8 and size == 1))
            if self.masked:
                offset += 4
            if len(b.data) < offset + size:
                return
            raw, stamps = b.take(offset + size)
            payload = raw[offset:]
            if self.masked:
                key = raw[offset - 4 : offset]
                payload = bytes(x ^ key[i % 4] for i, x in enumerate(payload))
            if op >= 8:
                if op == 8:
                    require(self.opcode is None)
                    self.closed = True
                continue
            if op:
                require(self.opcode is None)
                self.opcode = op
            else:
                require(self.opcode is not None)
            self.fragment.extend(payload)
            self.stamps.extend(stamps)
            require(len(self.fragment) <= 16384 and len(self.stamps) <= 32768)
            if fin:
                self.callback(
                    self.opcode, bytes(self.fragment), (min(self.stamps), max(self.stamps))
                )
                self.fragment.clear()
                self.stamps.clear()
                self.opcode = None


class Opus:
    def __init__(self, path):
        self.lib = ctypes.CDLL(path)
        self.lib.opus_decoder_create.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.lib.opus_decoder_create.restype = ctypes.c_void_p
        self.lib.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        code = ctypes.c_int()
        self.state = self.lib.opus_decoder_create(16000, 1, ctypes.byref(code))
        require(self.state and code.value == 0)

    def decode(self, data):
        out = (ctypes.c_int16 * 320)()
        require(self.lib.opus_decode(self.state, data, len(data), out, 320, 0) == 320)
        return struct.pack("<320h", *out)

    def close(self):
        if self.state:
            self.lib.opus_decoder_destroy(self.state)
            self.state = None


class Flow:
    def __init__(self, owner):
        self.owner = owner
        self.tcp = [None, None]
        self.buffers = [Buffer(), Buffer()]
        self.ws = [
            WS(True, lambda *args: self.message(0, *args)),
            WS(False, lambda *args: self.message(1, *args)),
        ]
        self.request = False
        self.upgraded = False
        self.ignored = False
        self.hello = False
        self.accepted = False
        self.session = None
        self.epoch = None
        self.accept_key = None
        self.sequence = 0
        self.sample = 0
        self.decoder = None
        self.acks = [0, 0]
        self.fins = [None, None]
        self.pcm = bytearray()
        self.frames = []

    def consume(self, direction, data, stamp):
        if self.ignored:
            return
        if self.upgraded:
            self.ws[direction].feed(data, stamp)
            return
        self.buffers[direction].add(data, stamp)
        if not self.request:
            request = headers(self.buffers[0])
            if request is None:
                return
            line, h = request
            if line != "GET /v1/device/media HTTP/1.1" or h.get("device-id") != TARGET:
                self.ignore()
                return
            require(
                h.get("upgrade", "").lower() == "websocket"
                and "upgrade" in [v.strip() for v in h.get("connection", "").lower().split(",")]
                and h.get("protocol-version") == "2"
                and h.get("sec-websocket-version") == "13"
            )
            auth = h.get("authorization", "")
            require(auth.startswith("Bearer "))
            parts = auth[7:].split(".")
            require(len(parts) == 3)
            claims = object_json(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            require(
                claims.get("device_id") == TARGET
                and claims.get("client_type") == "device"
                and claims.get("typ") == "memoria_device_media"
                and claims.get("client_id") == h.get("x-client-id")
            )
            self.session, self.epoch = claims.get("session_id"), claims.get("stream_epoch")
            require(
                isinstance(self.session, str)
                and self.session
                and type(self.epoch) is int
                and 0 < self.epoch < 2**32
            )
            key = h.get("sec-websocket-key", "")
            require(len(base64.b64decode(key, validate=True)) == 16)
            self.accept_key = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            self.request = True
            # Do not retain the parsed credentials across waiting for the response.
            del h, auth, parts, claims, request
        response = headers(self.buffers[1])
        if response is None:
            return
        line, h = response
        if not line.startswith("HTTP/1.1 101 "):
            self.ignore()
            return
        require(
            h.get("sec-websocket-accept") == self.accept_key
            and h.get("upgrade", "").lower() == "websocket"
            and "upgrade" in h.get("connection", "").lower()
        )
        require("sec-websocket-extensions" not in h)
        self.accept_key = None
        require(self.owner.selected is None)
        self.owner.selected = self
        self.upgraded = True
        # In the normal ordered handshake client hello precedes accepted.
        for d in (0, 1):
            b = self.buffers[d]
            while b.data:
                timestamp = b.times[0]
                n = 1
                while n < len(b.times) and b.times[n] == timestamp:
                    n += 1
                chunk, _ = b.take(n)
                self.ws[d].feed(chunk, timestamp)

    def ignore(self):
        self.ignored = True
        self.buffers = []
        for tcp in self.tcp:
            if tcp:
                tcp.pending.clear()

    def message(self, direction, op, payload, stamps):
        if op == 1:
            msg = object_json(payload)
            if direction == 0 and not self.hello:
                require(
                    msg.get("type") == "device.hello"
                    and msg.get("version") == 2
                    and msg.get("device_id") == TARGET
                    and msg.get("stream_epoch") == self.epoch
                )
                audio = msg.get("audio", {})
                require(
                    all(
                        audio.get(k) == v
                        for k, v in {
                            "uplink_codec": "opus",
                            "uplink_sample_rate": 16000,
                            "channels": 1,
                            "frame_ms": 20,
                        }.items()
                    )
                )
                require(
                    isinstance(msg.get("firmware_version"), str)
                    and 0 < len(msg["firmware_version"]) <= 128
                    and isinstance(msg.get("board_profile"), str)
                    and msg["board_profile"]
                )
                rates = audio.get("downlink_sample_rates", [])
                require(
                    isinstance(rates, list)
                    and 1 <= len(rates) <= 2
                    and 16000 in rates
                    and all(r in (16000, 24000) for r in rates)
                )
                cap = msg.get("capabilities", {})
                require(
                    cap.get("aec_mode") in ("none", "fd_low_cost", "fd_high_quality")
                    and cap.get("aec_reference")
                    in ("none", "software_post_gain_pre_i2s", "hardware_loopback")
                    and cap.get("playback_watermark") in ("none", "approximate", "exact")
                )
                # Full capability/binding policy remains the production Edge's responsibility;
                # session.accepted attests its validation, not physical AEC effectiveness.
                self.hello = True
            elif direction == 1 and not self.accepted:
                require(
                    self.hello
                    and msg.get("type") == "session.accepted"
                    and msg.get("version") == 2
                    and msg.get("session_id") == self.session
                    and msg.get("stream_epoch") == self.epoch
                    and msg.get("interaction_authority") == "python_authoritative"
                )
                self.accepted = True
                self.session = None
            return
        if direction == 1:
            return
        require(self.accepted and len(payload) >= 31)
        version, direct, flags, epoch, seq, sample, count, generation, size = struct.unpack(
            "!BBHIIQIIH", payload[:30]
        )
        require((version, direct, flags, epoch, count, generation) == (1, 1, 0, self.epoch, 320, 0))
        require(0 < size <= 2048 and len(payload) == 30 + size)
        require(seq == self.sequence and sample == self.sample)
        require(len(self.pcm) + 640 <= 4 * 1024 * 1024)
        if self.decoder is None:
            self.decoder = Opus(self.owner.libopus)
        self.pcm.extend(self.decoder.decode(payload[30:]))
        self.frames.append(
            {
                "sequence": seq,
                "sample_start": sample,
                "sample_end": sample + count,
                "packet_time_min_ns": stamps[0],
                "packet_time_max_ns": stamps[1],
            }
        )
        self.sequence += 1
        self.sample += count


class Capture:
    def __init__(self, libopus):
        self.libopus = libopus
        self.flows = {}
        self.selected = None

    def packet(self, raw, stamp):
        require(len(raw) >= 14)
        if raw[12:14] != b"\x08\x00":
            raise Invalid("unsupported packet")
        ip = raw[14:]
        require(len(ip) >= 20 and ip[0] >> 4 == 4)
        ihl = (ip[0] & 15) * 4
        total = int.from_bytes(ip[2:4], "big")
        require(20 <= ihl <= total <= len(ip) and not int.from_bytes(ip[6:8], "big") & 0x3FFF)
        require(ip[9] == 6 and ip[12:16] == ip[16:20] == b"\x7f\0\0\1")
        tcp = ip[ihl:total]
        require(len(tcp) >= 20)
        source, dest, seq = struct.unpack("!HHI", tcp[:8])
        require((source == 8794) != (dest == 8794))
        direction = int(source == 8794)
        key = dest if direction else source
        offset = (tcp[12] >> 4) * 4
        require(20 <= offset <= len(tcp))
        flags, data = tcp[13], tcp[offset:]
        flow = self.flows.get(key)
        if flow is None:
            if direction or flags & 0x12 != 2:
                return  # pre-existing flows can never qualify
            require(len(self.flows) < 16)
            flow = self.flows[key] = Flow(self)
        if flow.ignored:
            return
        if flags & 2:
            require(not data)
            if flow.tcp[direction] is None:
                flow.tcp[direction] = TCP(seq, lambda b, t: flow.consume(direction, b, t))
            else:
                require(flow.tcp[direction].next == seq + 1)
        require(flow.tcp[direction] is not None)
        if flags & 4:
            raise Invalid("connection reset")
        if flags & 16:
            flow.acks[1 - direction] = max(
                flow.acks[1 - direction], int.from_bytes(tcp[8:12], "big")
            )
        if flags & 1:
            flow.fins[direction] = seq + len(data)
        if data:
            require(flow.fins[direction] is None or seq + len(data) <= flow.fins[direction])
            flow.tcp[direction].feed(seq, data, stamp)

    def read(self, stream):
        header = stream.read(24)
        require(len(header) == 24)
        formats = {
            b"\xd4\xc3\xb2\xa1": ("<", 1000),
            b"\xa1\xb2\xc3\xd4": (">", 1000),
            b"\x4d\x3c\xb2\xa1": ("<", 1),
            b"\xa1\xb2\x3c\x4d": (">", 1),
        }
        require(header[:4] in formats)
        endian, scale = formats[header[:4]]
        major, minor, _, _, snap, link = struct.unpack(endian + "HHIIII", header[4:])
        require((major, minor, link) == (2, 4, 1) and 65535 <= snap <= MAX_INPUT)
        count, start, wall = 24, None, time.monotonic()
        while True:
            h = stream.read(16)
            if not h:
                break
            require(len(h) == 16)
            sec, frac, size, original = struct.unpack(endian + "IIII", h)
            require(size == original and 0 < size <= 65549 and frac * scale < 10**9)
            count += size + 16
            require(count <= MAX_INPUT and time.monotonic() - wall <= MAX_SECONDS)
            timestamp = sec * 10**9 + frac * scale
            start = timestamp if start is None else start
            require(0 <= timestamp - start <= MAX_SECONDS * 10**9)
            raw = stream.read(size)
            require(len(raw) == size)
            self.packet(raw, timestamp)
        f = self.selected
        require(f is not None and f.accepted and f.frames)
        require(all(not t.pending for t in f.tcp))
        for d in (0, 1):
            require(f.fins[d] is None or f.fins[d] == f.tcp[d].next)
            require(f.acks[d] <= f.tcp[d].next + int(f.fins[d] is not None))
        require(all(not w.buffer.data and w.opcode is None for w in f.ws))
        return f

    def close(self):
        for flow in self.flows.values():
            if flow.decoder:
                flow.decoder.close()


def save(flow, directory):
    # New private directory only; never follow an existing output directory or overwrite.
    directory.mkdir(mode=0o700)
    metadata = {
        "pcm_sha256": hashlib.sha256(flow.pcm).hexdigest(),
        "audited_edge_revision": "c44267c9221f904dd1eebddad920eb81da4bb53a",
        "schema": 1,
        "stream_epoch": flow.epoch,
        "sample_rate": 16000,
        "sample_start": 0,
        "sample_end": flow.sample,
        "missing_frames_observed": 0,
        "integrity": "contiguous_captured_prefix",
        "capture_tail": "unknown",
        "pcap_kernel_drop_count": None,
        "decoder_equivalence_to_production": "unverified",
        "tcp_retransmissions": "deduplicated",
        "jwt_verification": "edge_101_attested",
        "aec_effectiveness": "unknown",
        "frames": flow.frames,
    }
    try:
        for name, data in [("uplink.wav", None), ("metadata.json", metadata)]:
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                if data is None:
                    with wave.open(out, "wb") as wav:
                        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        wav.writeframes(flow.pcm)
                else:
                    out.write(json.dumps(data, separators=(",", ":")).encode())
    except BaseException:
        for name in ("uplink.wav", "metadata.json"):
            (directory / name).unlink(missing_ok=True)
        directory.rmdir()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--libopus", default="/opt/homebrew/lib/libopus.dylib")
    args = parser.parse_args()
    capture = Capture(args.libopus)

    def timeout(*_):
        raise Invalid("capture timeout")

    signal.signal(signal.SIGALRM, timeout)
    signal.signal(signal.SIGTERM, timeout)
    signal.signal(signal.SIGINT, timeout)
    signal.alarm(MAX_SECONDS + 5)
    try:
        require(sys.platform == "darwin")
        flow = capture.read(sys.stdin.buffer)
        save(flow, args.output)
        print("Saved target uplink; capture tail unknown.")
        return 0
    except Exception:
        # Never interpolate exception text: JSON/headers/paths can contain credentials.
        print(
            "Capture rejected; no successful evidence. Check bounds and handshake.", file=sys.stderr
        )
        return 1
    finally:
        signal.alarm(0)
        capture.close()


if __name__ == "__main__":
    sys.exit(main())
