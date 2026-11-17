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

--include-downlink additionally saves fresh mono generations at the 16 kHz or
24 kHz rate negotiated in session.accepted, observed on Edge->Nginx loopback,
not internet transmission, device receipt or audible proof. It requires an
accepted downlink_sample_rate of 16000 or 24000 with no reconnect snapshot.
Downlink PCM is
limited to 4 MiB TOTAL, with at most 256 generations and 4096 allowlisted control
records; audio metadata is bounded by the PCM limit. No text/reasons are saved.
Each generation begins at seq/sample 0 with a new decoder. Completed means an
observed completion barrier (no final sample count exists on this wire), cancelled
means a captured prefix cut off by a successor fence, and prefix_only means no
terminal boundary was observed. Capture tail remains unknown in every case.
Decodable bit corruption cannot be detected without an end-to-end checksum.

This tool trusts the audited Edge's 101 as signature-verification evidence; it
is not a JWT verifier. Run only in the separately authorized synthetic window.

Every run emits one machine-readable JSON report after the human message:
"extractor_rejection" on stderr for failures, "extractor_summary" on stdout for
success. It carries a stable reason code, its class and content-free counters
only, never headers, tokens, text, session or device identifiers.
"""

import argparse
import base64
import ctypes
import ctypes.util
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

REASONS = {
    "pcap_header_invalid": "capture_integrity",
    "pcap_link_unsupported": "protocol_mismatch",
    "pcap_record_truncated": "capture_integrity",
    "pcap_record_invalid": "capture_integrity",
    "pcap_packet_snapped": "capture_integrity",
    "pcap_timestamp_regressed": "capture_integrity",
    "pcap_span_exceeded": "bounds",
    "pcap_input_limit": "bounds",
    "wall_time_exceeded": "bounds",
    "packet_truncated": "capture_integrity",
    "packet_not_ipv4": "protocol_mismatch",
    "ip_header_invalid": "capture_integrity",
    "ip_fragment": "protocol_mismatch",
    "packet_not_loopback_tcp": "protocol_mismatch",
    "tcp_header_invalid": "capture_integrity",
    "tcp_port_mismatch": "protocol_mismatch",
    "flow_limit_exceeded": "bounds",
    "tcp_syn_invalid": "protocol_mismatch",
    "tcp_handshake_incomplete": "capture_boundary",
    "tcp_reset": "session_lifecycle",
    "tcp_sequence_wrap": "bounds",
    "tcp_retransmission_conflict": "capture_integrity",
    "tcp_reassembly_queue_limit": "bounds",
    "tcp_data_after_fin": "capture_integrity",
    "tcp_reassembly_gap": "capture_integrity",
    "tcp_ack_beyond_capture": "capture_integrity",
    "http_header_too_large": "bounds",
    "http_header_malformed": "protocol_mismatch",
    "target_upgrade_request_invalid": "protocol_mismatch",
    "target_jwt_invalid": "protocol_mismatch",
    "target_jwt_claims_mismatch": "protocol_mismatch",
    "target_upgrade_response_invalid": "protocol_mismatch",
    "websocket_extension_negotiated": "protocol_mismatch",
    "second_target_session": "session_lifecycle",
    "target_handshake_not_found": "capture_boundary",
    "target_session_not_accepted": "capture_boundary",
    "uplink_audio_absent": "capture_boundary",
    "parser_buffer_limit": "bounds",
    "websocket_frame_invalid": "protocol_mismatch",
    "websocket_mask_invalid": "protocol_mismatch",
    "websocket_opcode_invalid": "protocol_mismatch",
    "websocket_frame_too_large": "bounds",
    "websocket_control_frame_invalid": "protocol_mismatch",
    "websocket_fragment_invalid": "protocol_mismatch",
    "websocket_data_after_close": "session_lifecycle",
    "websocket_frame_incomplete": "capture_boundary",
    "json_invalid": "protocol_mismatch",
    "json_duplicate_key": "protocol_mismatch",
    "json_not_object": "protocol_mismatch",
    "device_hello_invalid": "protocol_mismatch",
    "device_hello_audio_unsupported": "protocol_mismatch",
    "device_hello_capabilities_unsupported": "protocol_mismatch",
    "session_accepted_invalid": "protocol_mismatch",
    "session_accepted_downlink_rate_unsupported": "protocol_mismatch",
    "session_accepted_reconnect_snapshot": "session_lifecycle",
    "uplink_audio_before_accept": "protocol_mismatch",
    "uplink_audio_header_invalid": "protocol_mismatch",
    "uplink_audio_first_frame_discontinuous": "protocol_mismatch",
    "uplink_audio_discontinuous": "protocol_mismatch",
    "uplink_pcm_limit": "bounds",
    "opus_decode_failed": "protocol_mismatch",
    "opus_decoder_unavailable": "environment",
    "downlink_audio_before_accept": "protocol_mismatch",
    "downlink_control_invalid": "protocol_mismatch",
    "downlink_after_close": "session_lifecycle",
    "downlink_control_limit": "bounds",
    "downlink_fence_invalid": "protocol_mismatch",
    "downlink_generation_reused": "protocol_mismatch",
    "downlink_fence_regressed": "protocol_mismatch",
    "downlink_generation_limit": "bounds",
    "downlink_generation_start_missing": "protocol_mismatch",
    "downlink_cancel_fence_mismatch": "protocol_mismatch",
    "downlink_flush_fence_invalid": "protocol_mismatch",
    "downlink_fence_mismatch": "protocol_mismatch",
    "downlink_audio_header_invalid": "protocol_mismatch",
    "downlink_audio_generation_mismatch": "protocol_mismatch",
    "downlink_audio_first_frame_discontinuous": "protocol_mismatch",
    "downlink_audio_discontinuous": "protocol_mismatch",
    "downlink_pcm_limit": "bounds",
    "unsupported_platform": "environment",
    "runtime_interrupted": "environment",
    "output_exists": "environment",
    "output_write_failed": "environment",
    "internal_error": "environment",
}


def find_libopus():
    found = ctypes.util.find_library("opus")
    if found:
        return found
    for path in ("/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib"):
        if Path(path).is_file():
            return path
    return None


class Invalid(Exception):
    def __init__(self, reason):
        super().__init__("capture rejected")
        self.reason = reason if reason in REASONS else "internal_error"


def require(ok, reason):
    if not ok:
        raise Invalid(reason)


def object_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "json_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except Invalid:
        raise
    except Exception:
        raise Invalid("json_invalid") from None
    require(isinstance(value, dict), "json_not_object")
    return value


class Buffer:
    def __init__(self):
        self.data = bytearray()
        self.times = []

    def add(self, data, stamp):
        require(len(self.data) + len(data) <= LIMIT, "parser_buffer_limit")
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
        require(0 <= seq < 2**32 - 1, "tcp_sequence_wrap")
        self.next = seq + 1
        self.pending = []
        self.consume = consume

    def feed(self, seq, data, stamp):
        require(0 <= seq and seq + len(data) < 2**32, "tcp_sequence_wrap")
        # Sequence wrap and ambiguous overlap fail closed; duplicates already
        # delivered are ignored. Retransmission timestamps never replace originals.
        require(abs(seq - self.next) < 2**31, "tcp_sequence_wrap")
        if seq < self.next:
            data = data[self.next - seq :]
            seq = self.next
        if not data:
            return
        for start, old, _ in self.pending:
            lo, hi = max(start, seq), min(start + len(old), seq + len(data))
            if lo < hi:
                require(
                    old[lo - start : hi - start] == data[lo - seq : hi - seq],
                    "tcp_retransmission_conflict",
                )
        self.pending.append((seq, data, stamp))
        require(
            len(self.pending) <= 256 and sum(len(x[1]) for x in self.pending) <= LIMIT,
            "tcp_reassembly_queue_limit",
        )
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
        require(len(buffer.data) <= 16384, "http_header_too_large")
        return None
    require(end <= 16384, "http_header_too_large")
    raw, _ = buffer.take(end + 4)
    try:
        lines = raw[:-4].decode("ascii").split("\r\n")
    except UnicodeDecodeError:
        raise Invalid("http_header_malformed") from None
    result = {}
    for line in lines[1:]:
        require(":" in line and not line.startswith((" ", "\t")), "http_header_malformed")
        name, value = line.split(":", 1)
        name = name.lower()
        require(name not in result, "http_header_malformed")
        result[name] = value.strip()
    return lines[0], result


class WS:
    def __init__(self, masked, callback, counts=None):
        self.buffer = Buffer()
        self.masked = masked
        self.callback = callback
        self.counts = counts
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
            require(not self.closed, "websocket_data_after_close")
            require(not first & 112, "websocket_frame_invalid")
            require(bool(second & 128) == self.masked, "websocket_mask_invalid")
            size, offset = second & 127, 2
            if size in (126, 127):
                width = 2 if size == 126 else 8
                if len(b.data) < 2 + width:
                    return
                size = int.from_bytes(b.data[2 : 2 + width], "big")
                require(size >= (126 if width == 2 else 65536), "websocket_frame_invalid")
                offset += width
            require(size <= 16384, "websocket_frame_too_large")
            require(op in (0, 1, 2, 8, 9, 10), "websocket_opcode_invalid")
            if op >= 8:
                require(
                    fin and size <= 125 and not (op == 8 and size == 1),
                    "websocket_control_frame_invalid",
                )
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
                if self.counts is not None:
                    self.counts["ws_control_frames"] += 1
                if op == 8:
                    require(self.opcode is None, "websocket_fragment_invalid")
                    self.closed = True
                continue
            if op:
                require(self.opcode is None, "websocket_fragment_invalid")
                self.opcode = op
            else:
                require(self.opcode is not None, "websocket_fragment_invalid")
            self.fragment.extend(payload)
            self.stamps.extend(stamps)
            require(
                len(self.fragment) <= 16384 and len(self.stamps) <= 32768,
                "websocket_frame_too_large",
            )
            if fin:
                if self.counts is not None:
                    key = "ws_text_messages" if self.opcode == 1 else "ws_binary_messages"
                    self.counts[key] += 1
                self.callback(
                    self.opcode, bytes(self.fragment), (min(self.stamps), max(self.stamps))
                )
                self.fragment.clear()
                self.stamps.clear()
                self.opcode = None


class Opus:
    def __init__(self, path, rate=16000, frame_samples=320):
        try:
            self.lib = ctypes.CDLL(path)
        except OSError:
            raise Invalid("opus_decoder_unavailable") from None
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
        self.state = self.lib.opus_decoder_create(rate, 1, ctypes.byref(code))
        require(self.state and code.value == 0, "opus_decoder_unavailable")
        self.frame_samples = frame_samples

    def decode(self, data):
        out = (ctypes.c_int16 * self.frame_samples)()
        require(
            self.lib.opus_decode(self.state, data, len(data), out, self.frame_samples, 0)
            == self.frame_samples,
            "opus_decode_failed",
        )
        return struct.pack(f"<{self.frame_samples}h", *out)

    def close(self):
        if self.state:
            self.lib.opus_decoder_destroy(self.state)
            self.state = None


MAX_DOWNLINK_PCM = 4 * 1024 * 1024
MAX_GENERATIONS = 256
MAX_CONTROLS = 4096
FENCE_KEYS = ("session_epoch", "turn_id", "generation_id", "tool_epoch")


def uint(value, bits=32, positive=False, reason="internal_error"):
    if not (type(value) is int and int(positive) <= value < 2**bits):
        raise Invalid(reason)
    return value


def fence_of(value, reason="downlink_fence_invalid"):
    if not isinstance(value, dict):
        raise Invalid(reason)
    return tuple(uint(value.get(k), positive=k != "tool_epoch", reason=reason) for k in FENCE_KEYS)


class Downlink:
    """c442 device_ws_downlink.go projection; no Core-to-device remapping.

    Only fresh, negotiated 16 kHz generations are accepted. Reconnect snapshots,
    ambiguous priority-lane ordering and discontinuities fail closed. A completion
    barrier proves only the observed Edge prefix, not delivery or audible playout.
    Decoder state is isolated per full fence; production encoder equivalence is
    deliberately unverified (the Edge encoder can persist across generations).
    """

    def __init__(self, flow):
        self.flow = flow
        self.generations = []
        self.controls = []
        self.current = None
        self.last_fence = None
        self.seen_generation_ids = set()
        self.cancel_fence = None
        self.flush_fence = None
        self.total_pcm = 0
        self.decoder = None
        self.closed = False
        self.rate = 16000
        self.frame_samples = 320
        self.frame_bytes = 640

    def end_decoder(self):
        if self.decoder:
            self.decoder.close()
            self.decoder = None

    def control(self, msg, stamp):
        counts = self.flow.owner.counts
        kind = msg.get("type")
        kinds = (
            "generation.started",
            "generation.completed",
            "generation.cancelled",
            "generation.pause",
            "generation.resume",
            "playback.flush",
            "session.close",
            "session.error",
        )
        if kind not in kinds:
            counts["downlink_controls_skipped"] += 1
            return
        require(not self.closed, "downlink_after_close")
        require(
            msg.get("version") == 2
            and msg.get("session_id") == self.flow.session
            and msg.get("stream_epoch") == self.flow.epoch,
            "downlink_control_invalid",
        )
        require(len(self.controls) < MAX_CONTROLS, "downlink_control_limit")
        record = {
            "type": kind,
            "control_sequence": uint(
                msg.get("control_sequence"), reason="downlink_control_invalid"
            ),
            "server_monotonic_ms": uint(
                msg.get("server_monotonic_ms"), 64, reason="downlink_control_invalid"
            ),
            "full_message_available_ns": stamp,
        }
        if kind in ("session.close", "session.error"):
            self.closed = True
            self.end_decoder()
            self.controls.append(record)
            counts["downlink_controls"] += 1
            return
        fence = fence_of(msg.get("fence"), "downlink_fence_invalid")
        record["fence"] = dict(zip(FENCE_KEYS, fence, strict=True))
        g = self.current
        if kind == "generation.started":
            # Audio carries only the generation ID, so fence reuse is ambiguous.
            require(fence[2] not in self.seen_generation_ids, "downlink_generation_reused")
            self.seen_generation_ids.add(fence[2])
            require(
                self.last_fence is None or fence > self.last_fence or fence == self.flush_fence,
                "downlink_fence_regressed",
            )
            require(len(self.generations) < MAX_GENERATIONS, "downlink_generation_limit")
            self.end_decoder()
            g = {
                "fence": record["fence"],
                "status": "prefix_only",
                "frames": [],
                "pcm": bytearray(),
                "sample_end": 0,
            }
            self.generations.append(g)
            counts["downlink_generations"] += 1
            self.current = g
            self.last_fence = fence
            self.cancel_fence = None
            self.flush_fence = None
        elif kind in ("generation.cancelled", "playback.flush"):
            # CANCEL announces the successor; FLUSH carries an explicit replacement.
            # Neither is a start or permission to decode replacement audio.
            if fence != self.cancel_fence:
                require(g is not None, "downlink_generation_start_missing")
                require(g["status"] == "prefix_only", "downlink_fence_mismatch")
                old = fence_of(g["fence"])
                if kind == "generation.cancelled":
                    require(
                        fence == (old[0], old[1], old[2] + 1, old[3]),
                        "downlink_cancel_fence_mismatch",
                    )
                else:
                    require(fence > old, "downlink_flush_fence_invalid")
                g["status"] = "cancelled_prefix"
                self.cancel_fence = fence
                self.last_fence = fence
                self.end_decoder()
            if kind == "playback.flush":
                replacement = uint(
                    msg.get("replacement_generation_id"),
                    positive=True,
                    reason="downlink_flush_fence_invalid",
                )
                require(replacement == fence[2], "downlink_flush_fence_invalid")
                record["replacement_generation_id"] = replacement
                self.flush_fence = fence
        else:
            require(g is not None, "downlink_generation_start_missing")
            require(fence == fence_of(g["fence"]), "downlink_fence_mismatch")
            require(g["status"] == "prefix_only", "downlink_fence_mismatch")
            if kind == "generation.completed":
                g["status"] = "completion_observed"
                self.end_decoder()
        self.controls.append(record)
        counts["downlink_controls"] += 1

    def audio(self, payload, stamp):
        counts = self.flow.owner.counts
        require(not self.closed, "downlink_after_close")
        require(len(payload) >= 31, "downlink_audio_header_invalid")
        version, direct, flags, epoch, seq, sample, count, generation, size = struct.unpack(
            "!BBHIIQIIH", payload[:30]
        )
        require(
            (version, direct, flags, epoch, count)
            == (1, 2, 0, self.flow.epoch, self.frame_samples),
            "downlink_audio_header_invalid",
        )
        require(
            0 < size <= 2048 and len(payload) == 30 + size,
            "downlink_audio_header_invalid",
        )
        g = self.current
        require(g is not None, "downlink_generation_start_missing")
        require(g["status"] == "prefix_only", "downlink_audio_generation_mismatch")
        require(generation == g["fence"]["generation_id"], "downlink_audio_generation_mismatch")
        if g["frames"]:
            require(
                seq == len(g["frames"]) and sample == g["sample_end"],
                "downlink_audio_discontinuous",
            )
        else:
            require(seq == 0 and sample == 0, "downlink_audio_first_frame_discontinuous")
        require(self.total_pcm + self.frame_bytes <= MAX_DOWNLINK_PCM, "downlink_pcm_limit")
        if self.decoder is None:
            self.decoder = Opus(self.flow.owner.libopus, self.rate, self.frame_samples)
        g["pcm"].extend(self.decoder.decode(payload[30:]))
        self.total_pcm += self.frame_bytes
        g["sample_end"] += count
        g["frames"].append(
            {
                "sequence": seq,
                "generation_id": generation,
                "sample_start": sample,
                "frame_samples": count,
                "full_message_available_ns": stamp,
            }
        )
        counts["downlink_frames"] += 1


class Flow:
    def __init__(self, owner):
        self.owner = owner
        self.tcp = [None, None]
        self.buffers = [Buffer(), Buffer()]
        self.ws = [
            WS(True, lambda *args: self.message(0, *args), owner.counts),
            WS(False, lambda *args: self.message(1, *args), owner.counts),
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
        self.available = [0, 0]
        self.downlink = Downlink(self) if owner.include_downlink else None

    def consume(self, direction, data, stamp):
        if self.ignored:
            return
        self.available[direction] = max(self.available[direction], stamp)
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
                self.owner.counts["flows_ignored_non_target"] += 1
                self.ignore()
                return
            require(
                h.get("upgrade", "").lower() == "websocket"
                and "upgrade" in [v.strip() for v in h.get("connection", "").lower().split(",")]
                and h.get("protocol-version") == "2"
                and h.get("sec-websocket-version") == "13",
                "target_upgrade_request_invalid",
            )
            auth = h.get("authorization", "")
            require(auth.startswith("Bearer "), "target_jwt_invalid")
            parts = auth[7:].split(".")
            require(len(parts) == 3, "target_jwt_invalid")
            try:
                claims = object_json(
                    base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
                )
            except Exception:
                raise Invalid("target_jwt_invalid") from None
            require(
                claims.get("device_id") == TARGET
                and claims.get("client_type") == "device"
                and claims.get("typ") == "memoria_device_media"
                and claims.get("client_id") == h.get("x-client-id"),
                "target_jwt_claims_mismatch",
            )
            self.session, self.epoch = claims.get("session_id"), claims.get("stream_epoch")
            require(
                isinstance(self.session, str)
                and self.session
                and type(self.epoch) is int
                and 0 < self.epoch < 2**32,
                "target_jwt_claims_mismatch",
            )
            key = h.get("sec-websocket-key", "")
            try:
                decoded_key = base64.b64decode(key, validate=True)
            except Exception:
                raise Invalid("target_upgrade_request_invalid") from None
            require(len(decoded_key) == 16, "target_upgrade_request_invalid")
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
            self.owner.counts["target_upgrade_refused"] += 1
            self.ignore()
            return
        require(
            h.get("sec-websocket-accept") == self.accept_key
            and h.get("upgrade", "").lower() == "websocket"
            and "upgrade" in h.get("connection", "").lower(),
            "target_upgrade_response_invalid",
        )
        require("sec-websocket-extensions" not in h, "websocket_extension_negotiated")
        self.accept_key = None
        require(self.owner.selected is None, "second_target_session")
        self.owner.selected = self
        self.owner.counts["target_handshakes"] += 1
        self.owner.counts["stream_epoch"] = self.epoch
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
        counts = self.owner.counts
        if op == 1:
            msg = object_json(payload)
            if direction == 0 and not self.hello:
                require(
                    msg.get("type") == "device.hello"
                    and msg.get("version") == 2
                    and msg.get("device_id") == TARGET
                    and msg.get("stream_epoch") == self.epoch,
                    "device_hello_invalid",
                )
                audio = msg.get("audio")
                require(
                    isinstance(audio, dict)
                    and all(
                        audio.get(k) == v
                        for k, v in {
                            "uplink_codec": "opus",
                            "uplink_sample_rate": 16000,
                            "channels": 1,
                            "frame_ms": 20,
                        }.items()
                    ),
                    "device_hello_audio_unsupported",
                )
                require(
                    isinstance(msg.get("firmware_version"), str)
                    and 0 < len(msg["firmware_version"]) <= 128
                    and isinstance(msg.get("board_profile"), str)
                    and msg["board_profile"],
                    "device_hello_invalid",
                )
                rates = audio.get("downlink_sample_rates")
                require(
                    isinstance(rates, list)
                    and 1 <= len(rates) <= 2
                    and 16000 in rates
                    and all(r in (16000, 24000) for r in rates),
                    "device_hello_audio_unsupported",
                )
                cap = msg.get("capabilities")
                require(
                    isinstance(cap, dict)
                    and cap.get("aec_mode") in ("none", "fd_low_cost", "fd_high_quality")
                    and cap.get("aec_reference")
                    in ("none", "software_post_gain_pre_i2s", "hardware_loopback")
                    and cap.get("playback_watermark") in ("none", "approximate", "exact"),
                    "device_hello_capabilities_unsupported",
                )
                # Full capability/binding policy remains the production Edge's responsibility;
                # session.accepted attests its validation, not physical AEC effectiveness.
                self.hello = True
                counts["target_hello"] = 1
            elif direction == 1 and not self.accepted:
                require(
                    self.hello
                    and msg.get("type") == "session.accepted"
                    and msg.get("version") == 2
                    and msg.get("session_id") == self.session
                    and msg.get("stream_epoch") == self.epoch
                    and msg.get("interaction_authority") == "python_authoritative",
                    "session_accepted_invalid",
                )
                self.accepted = True
                rate = msg.get("downlink_sample_rate")
                counts["accepted_downlink_sample_rate"] = (
                    rate if type(rate) is int and rate in (16000, 24000) else -1
                )
                if self.downlink:
                    require(
                        type(rate) is int and rate in (16000, 24000),
                        "session_accepted_downlink_rate_unsupported",
                    )
                    require(
                        msg.get("current_fence") is None
                        and msg.get("current_generation_active") is False,
                        "session_accepted_reconnect_snapshot",
                    )
                    self.downlink.rate = rate
                    self.downlink.frame_samples = rate // 50
                    self.downlink.frame_bytes = 2 * self.downlink.frame_samples
                else:
                    self.session = None
                counts["target_accepted"] = 1
            elif direction == 1 and self.downlink:
                self.downlink.control(msg, max(stamps[1], self.available[1]))
            return
        if direction == 1:
            if self.downlink:
                require(self.accepted, "downlink_audio_before_accept")
                self.downlink.audio(payload, max(stamps[1], self.available[1]))
            return
        require(self.accepted, "uplink_audio_before_accept")
        require(len(payload) >= 31, "uplink_audio_header_invalid")
        version, direct, flags, epoch, seq, sample, count, generation, size = struct.unpack(
            "!BBHIIQIIH", payload[:30]
        )
        require(
            (version, direct, flags, epoch, count, generation) == (1, 1, 0, self.epoch, 320, 0),
            "uplink_audio_header_invalid",
        )
        require(0 < size <= 2048 and len(payload) == 30 + size, "uplink_audio_header_invalid")
        if self.frames:
            require(seq == self.sequence and sample == self.sample, "uplink_audio_discontinuous")
        else:
            require(seq == 0 and sample == 0, "uplink_audio_first_frame_discontinuous")
        require(len(self.pcm) + 640 <= 4 * 1024 * 1024, "uplink_pcm_limit")
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
        counts["uplink_frames"] += 1
        self.sequence += 1
        self.sample += count


class Capture:
    def __init__(self, libopus, include_downlink=False):
        self.libopus = libopus
        self.include_downlink = include_downlink
        self.flows = {}
        self.selected = None
        self.counts = {
            "pcap_records": 0,
            "pcap_bytes": 0,
            "capture_span_ms": 0,
            "flows_opened": 0,
            "flows_ignored_non_target": 0,
            "target_upgrade_refused": 0,
            "target_handshakes": 0,
            "preexisting_flow_packets": 0,
            "preexisting_flow_payload_bytes": 0,
            "target_hello": 0,
            "target_accepted": 0,
            "accepted_downlink_sample_rate": 0,
            "stream_epoch": None,
            "uplink_frames": 0,
            "ws_text_messages": 0,
            "ws_binary_messages": 0,
            "ws_control_frames": 0,
            "downlink_controls": 0,
            "downlink_controls_skipped": 0,
            "downlink_generations": 0,
            "downlink_frames": 0,
            "tcp_pending_segments_up": 0,
            "tcp_pending_segments_down": 0,
            "ws_buffered_bytes_up": 0,
            "ws_buffered_bytes_down": 0,
        }

    def packet(self, raw, stamp):
        require(len(raw) >= 14, "packet_truncated")
        if raw[12:14] != b"\x08\x00":
            raise Invalid("packet_not_ipv4")
        ip = raw[14:]
        require(len(ip) >= 20, "ip_header_invalid")
        require(ip[0] >> 4 == 4, "packet_not_ipv4")
        ihl = (ip[0] & 15) * 4
        total = int.from_bytes(ip[2:4], "big")
        require(20 <= ihl <= total <= len(ip), "ip_header_invalid")
        require(not int.from_bytes(ip[6:8], "big") & 0x3FFF, "ip_fragment")
        require(
            ip[9] == 6 and ip[12:16] == ip[16:20] == b"\x7f\0\0\1",
            "packet_not_loopback_tcp",
        )
        tcp = ip[ihl:total]
        require(len(tcp) >= 20, "tcp_header_invalid")
        source, dest, seq = struct.unpack("!HHI", tcp[:8])
        require((source == 8794) != (dest == 8794), "tcp_port_mismatch")
        direction = int(source == 8794)
        key = dest if direction else source
        offset = (tcp[12] >> 4) * 4
        require(20 <= offset <= len(tcp), "tcp_header_invalid")
        flags, data = tcp[13], tcp[offset:]
        flow = self.flows.get(key)
        if flow is None:
            if direction or flags & 0x12 != 2:
                self.counts["preexisting_flow_packets"] += 1
                self.counts["preexisting_flow_payload_bytes"] += len(data)
                return  # pre-existing flows can never qualify
            require(len(self.flows) < 16, "flow_limit_exceeded")
            flow = self.flows[key] = Flow(self)
            self.counts["flows_opened"] += 1
        if flow.ignored:
            return
        if flags & 2:
            require(not data, "tcp_syn_invalid")
            if flow.tcp[direction] is None:
                flow.tcp[direction] = TCP(seq, lambda b, t: flow.consume(direction, b, t))
            else:
                require(flow.tcp[direction].next == seq + 1, "tcp_syn_invalid")
        require(flow.tcp[direction] is not None, "tcp_handshake_incomplete")
        if flags & 4:
            raise Invalid("tcp_reset")
        if flags & 16:
            flow.acks[1 - direction] = max(
                flow.acks[1 - direction], int.from_bytes(tcp[8:12], "big")
            )
        if flags & 1:
            require(seq + len(data) + 1 < 2**32, "tcp_sequence_wrap")
            flow.fins[direction] = seq + len(data)
        if data:
            require(
                flow.fins[direction] is None or seq + len(data) <= flow.fins[direction],
                "tcp_data_after_fin",
            )
            flow.tcp[direction].feed(seq, data, stamp)

    def read(self, stream):
        header = stream.read(24)
        require(len(header) == 24, "pcap_header_invalid")
        formats = {
            b"\xd4\xc3\xb2\xa1": ("<", 1000),
            b"\xa1\xb2\xc3\xd4": (">", 1000),
            b"\x4d\x3c\xb2\xa1": ("<", 1),
            b"\xa1\xb2\x3c\x4d": (">", 1),
        }
        require(header[:4] in formats, "pcap_header_invalid")
        endian, scale = formats[header[:4]]
        major, minor, _, _, snap, link = struct.unpack(endian + "HHIIII", header[4:])
        require(major == 2 and minor == 4, "pcap_header_invalid")
        require(link == 1, "pcap_link_unsupported")
        require(65535 <= snap <= MAX_INPUT, "pcap_header_invalid")
        count, start, wall = 24, None, time.monotonic()
        while True:
            h = stream.read(16)
            if not h:
                break
            require(len(h) == 16, "pcap_record_truncated")
            sec, frac, size, original = struct.unpack(endian + "IIII", h)
            require(size == original, "pcap_packet_snapped")
            require(0 < size <= 65549, "pcap_record_invalid")
            require(frac * scale < 10**9, "pcap_record_invalid")
            count += size + 16
            self.counts["pcap_bytes"] = count
            require(count <= MAX_INPUT, "pcap_input_limit")
            require(time.monotonic() - wall <= MAX_SECONDS, "wall_time_exceeded")
            timestamp = sec * 10**9 + frac * scale
            start = timestamp if start is None else start
            require(timestamp >= start, "pcap_timestamp_regressed")
            require(timestamp - start <= MAX_SECONDS * 10**9, "pcap_span_exceeded")
            self.counts["capture_span_ms"] = (timestamp - start) // 10**6
            raw = stream.read(size)
            require(len(raw) == size, "pcap_record_truncated")
            self.counts["pcap_records"] += 1
            self.packet(raw, timestamp)
        f = self.selected
        # Order only chooses which reason is reported when several end
        # conditions fail: transport/integrity gaps outrank session-content
        # shortfalls.
        require(f is not None, "target_handshake_not_found")
        require(all(not t.pending for t in f.tcp), "tcp_reassembly_gap")
        for d in (0, 1):
            require(f.fins[d] is None or f.fins[d] == f.tcp[d].next, "tcp_reassembly_gap")
            require(
                f.acks[d] <= f.tcp[d].next + int(f.fins[d] is not None),
                "tcp_ack_beyond_capture",
            )
        require(
            all(not w.buffer.data and w.opcode is None for w in f.ws),
            "websocket_frame_incomplete",
        )
        require(f.accepted, "target_session_not_accepted")
        require(f.frames, "uplink_audio_absent")
        return f

    def close(self):
        for flow in self.flows.values():
            if flow.decoder:
                flow.decoder.close()
            if flow.downlink:
                flow.downlink.end_decoder()


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
    files = [("uplink.wav", flow.pcm, 16000)]
    if flow.downlink:
        dl = flow.downlink
        generations = []
        for index, g in enumerate(dl.generations):
            name = f"downlink-{index:03d}.wav"
            files.append((name, g["pcm"], dl.rate))
            generations.append(
                {k: v for k, v in g.items() if k != "pcm"}
                | {"wav": name, "pcm_sha256": hashlib.sha256(g["pcm"]).hexdigest()}
            )
        metadata["downlink"] = {
            "observation_point": "edge_to_nginx_loopback",
            "internet_send_device_receive_audible": "unverified",
            "capture_tail": "unknown",
            "sample_rate": dl.rate,
            "integrity": "server_emitted_contiguous_prefixes",
            "completion_scope": "observed_edge_barrier_only_no_final_sample_count",
            "decoder_equivalence_to_production": "unverified",
            "generations": generations,
            "controls": dl.controls,
        }
    files.append(("metadata.json", metadata, None))
    created = []
    try:
        for name, data, rate in files:
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(name)
            with os.fdopen(fd, "wb") as out:
                if name.endswith(".wav"):
                    with wave.open(out, "wb") as wav:
                        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                        wav.writeframes(data)
                else:
                    out.write(json.dumps(data, separators=(",", ":")).encode())
    except BaseException:
        for name in created:
            (directory / name).unlink(missing_ok=True)
        directory.rmdir()
        raise


def report(capture, reason, exception_type=None):
    counts = dict(capture.counts)
    flow = capture.selected
    if flow is not None:
        counts["tcp_pending_segments_up"] = len(flow.tcp[0].pending) if flow.tcp[0] else 0
        counts["tcp_pending_segments_down"] = len(flow.tcp[1].pending) if flow.tcp[1] else 0
        counts["ws_buffered_bytes_up"] = len(flow.ws[0].buffer.data)
        counts["ws_buffered_bytes_down"] = len(flow.ws[1].buffer.data)
    payload = {
        "schema": 1,
        "reason": reason,
        "class": REASONS[reason] if reason is not None else None,
        "counts": counts,
    }
    if reason == "internal_error" and exception_type is not None:
        payload["exception_type"] = exception_type
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--libopus", default=find_libopus())
    parser.add_argument(
        "--include-downlink",
        action="store_true",
        help="save bounded Edge-to-Nginx reference prefixes at the negotiated 16k/24k rate; not audible proof",
    )
    args = parser.parse_args()
    if not args.libopus:
        parser.error("libopus not found; install it or specify --libopus")
    capture = Capture(args.libopus, args.include_downlink)

    def timeout(*_):
        raise Invalid("wall_time_exceeded")

    def interrupted(*_):
        raise Invalid("runtime_interrupted")

    signal.signal(signal.SIGALRM, timeout)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.alarm(MAX_SECONDS + 5)
    reason, exception_type = "internal_error", None
    try:
        require(sys.platform == "darwin", "unsupported_platform")
        flow = capture.read(sys.stdin.buffer)
        try:
            save(flow, args.output)
        except FileExistsError:
            raise Invalid("output_exists") from None
        except OSError:
            raise Invalid("output_write_failed") from None
    except Invalid as exc:
        reason = exc.reason
    except Exception as exc:
        exception_type = type(exc).__name__
    else:
        print("Saved target uplink; capture tail unknown.")
        print("extractor_summary " + report(capture, None))
        return 0
    finally:
        signal.alarm(0)
        capture.close()
    # Never interpolate exception text: JSON/headers/paths can contain credentials.
    print(
        "Capture rejected; no successful evidence. Check bounds and handshake.",
        file=sys.stderr,
    )
    print("extractor_rejection " + report(capture, reason, exception_type), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
