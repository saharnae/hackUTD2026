"""UART frame encode/decode (1 Mbaud 8N1 link between laptop and FPGA).

Frame: sync 0x5A, type (1), length (1), payload (length bytes), CRC-16 (2).

ASSUMPTIONS:
  F1. CRC-16-CCITT (PROPOSED): poly 0x1021, init 0xFFFF, no reflection, no final XOR
      (CRC-16/CCITT-FALSE, check 0x29B1 over b"123456789"), computed over
      type, length and payload; sent big-endian (high byte first).
  F2. length is one byte = payload length in bytes (0..255). The snapshot frame is
      therefore 1 + 1 + 1 + 95 + 2 = 100 bytes.
  F3. Payload lengths are enforced for every type, per docs/FRAMES.md (PROPOSED layouts
      except 0x10): 0x10 95, 0x11 4, 0x12 10, 0x14 8, 0x15 0, 0x16 2, 0x17 16, 0x20 33,
      0x21 20. 0x13 load block is 2 + 2N bytes with N = 1..126 (even length 4..254).
  F4. An unknown type byte or a wrong length for its type is treated like a bad CRC:
      the decoder drops that sync byte and hunts for the next 0x5A. encode() raises
      ValueError naming the type and the length rule.
  F5. Config (0x12) is applied only while armed = 0 (docs/FRAMES.md FR4). A config
      frame received while armed, or with any field out of range, is ignored in full and
      counted in config_rejects. ConfigRegister models that rule for the FPGA block.
"""

SYNC = 0x5A
T_SNAPSHOT, T_HEARTBEAT, T_CONFIG, T_LOAD_BLOCK = 0x10, 0x11, 0x12, 0x13
T_HEDGE_ORDER, T_KILL, T_CONTROL, T_FEED_WORD = 0x14, 0x15, 0x16, 0x17
T_STATUS, T_LOG = 0x20, 0x21
FRAME_TYPES = {
    T_SNAPSHOT: "snapshot", T_HEARTBEAT: "heartbeat", T_CONFIG: "config",
    T_LOAD_BLOCK: "load block", T_HEDGE_ORDER: "hedge order", T_KILL: "kill",
    T_CONTROL: "control", T_FEED_WORD: "feed word", T_STATUS: "status",
    T_LOG: "order and fill log",
}
FIXED_LEN = {T_SNAPSHOT: 95, T_HEARTBEAT: 4, T_CONFIG: 10, T_HEDGE_ORDER: 8, T_KILL: 0,
             T_CONTROL: 2, T_FEED_WORD: 16, T_STATUS: 33, T_LOG: 20}   # F3
LOAD_BLOCK_WORDS = (1, 126)                                          # 0x13: 2 + 2N bytes
HEADER_LEN, CRC_LEN = 3, 2


def length_ok(ftype, n):
    if ftype == T_LOAD_BLOCK:
        return n % 2 == 0 and 2 + 2 * LOAD_BLOCK_WORDS[0] <= n <= 2 + 2 * LOAD_BLOCK_WORDS[1]
    return FIXED_LEN.get(ftype) == n


def length_rule(ftype):
    if ftype == T_LOAD_BLOCK:
        return "2 + 2N bytes, N = 1..126"
    return f"{FIXED_LEN[ftype]} bytes"


def crc16_ccitt(data, init=0xFFFF):
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode(ftype, payload=b""):
    """Build one frame. Raises ValueError for unknown type or bad length."""
    payload = bytes(payload)
    if ftype not in FRAME_TYPES:
        raise ValueError(f"unknown frame type 0x{ftype:02X}")
    if not length_ok(ftype, len(payload)):
        raise ValueError(f"type 0x{ftype:02X} ({FRAME_TYPES[ftype]}) payload must be "
                         f"{length_rule(ftype)}, got {len(payload)}")
    body = bytes([ftype, len(payload)]) + payload
    return bytes([SYNC]) + body + crc16_ccitt(body).to_bytes(2, "big")


def decode(frame):
    """Decode exactly one complete frame -> (type, payload). Raises ValueError if bad."""
    frame = bytes(frame)
    if len(frame) < HEADER_LEN + CRC_LEN or frame[0] != SYNC:
        raise ValueError("missing sync or too short")
    ftype, length = frame[1], frame[2]
    if len(frame) != HEADER_LEN + length + CRC_LEN:
        raise ValueError("length mismatch")
    if not _header_ok(ftype, length):
        raise ValueError("bad type or length")
    if crc16_ccitt(frame[1:3 + length]) != int.from_bytes(frame[-2:], "big"):
        raise ValueError("bad CRC")
    return ftype, frame[3:3 + length]


def _header_ok(ftype, length):
    return ftype in FRAME_TYPES and length_ok(ftype, length)


# ---------------------------------------------------------------- 0x12 config (F5)
CONFIG_FIELDS = (          # name, bytes, default, lo, hi   (docs/FRAMES.md)
    ("edge_threshold", 2, 384, 256, 768),       # ticks Q8
    ("exit_edge", 2, 128, 64, 384),             # ticks Q8
    ("min_move", 1, 2, 1, 5),                   # cents
    ("max_orders_per_event", 1, 2, 1, 4),
    ("size_cap_base", 1, 5, 2, 10),
    ("size_cap_max", 1, 10, 2, 10),             # also >= size_cap_base
    ("time_stop_us", 2, 5000, 1000, 20000),
)
CONFIG_DEFAULTS = {n: d for n, _, d, _, _ in CONFIG_FIELDS}


def encode_config(params):
    return b"".join(int(params[n]).to_bytes(nb, "big") for n, nb, _, _, _ in CONFIG_FIELDS)


def decode_config(payload):
    """10-byte payload -> dict. Raises ValueError on wrong length or any field out of range."""
    if len(payload) != FIXED_LEN[T_CONFIG]:
        raise ValueError(f"config payload must be {FIXED_LEN[T_CONFIG]} bytes, got {len(payload)}")
    out, off = {}, 0
    for n, nb, _, lo, hi in CONFIG_FIELDS:
        v = int.from_bytes(payload[off:off + nb], "big")
        off += nb
        if not lo <= v <= hi:
            raise ValueError(f"config {n} = {v} outside {lo}..{hi}")
        out[n] = v
    if out["size_cap_max"] < out["size_cap_base"]:
        raise ValueError("config size_cap_max < size_cap_base")
    return out


class ConfigRegister:
    """Reference behaviour of the FPGA config block (F5): apply only while not armed."""

    def __init__(self):
        self.params, self.config_rejects = dict(CONFIG_DEFAULTS), 0

    def apply(self, payload, armed):
        """Returns True if applied. Ignored in full (and counted) when armed or invalid."""
        try:
            new = decode_config(payload)
        except ValueError:
            new = None
        if armed or new is None:
            self.config_rejects += 1
            return False
        self.params = new
        return True


def encode_feed_word(word):
    return encode(T_FEED_WORD, word.to_bytes(16, "big"))


def decode_feed_word(payload):
    if len(payload) != 16:
        raise ValueError("feed word payload is 16 bytes")
    return int.from_bytes(payload, "big")


class FrameDecoder:
    """Streaming decoder. feed(bytes) returns the list of good (type, payload) frames.

    Bytes before a sync are skipped. On a bad CRC, unknown type or wrong fixed length
    the sync byte is discarded and the hunt restarts at the next byte (F4).
    Counters: frames_ok, bad_frames, bytes_skipped.
    """

    def __init__(self):
        self.buf = bytearray()
        self.frames_ok = self.bad_frames = self.bytes_skipped = 0

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                self.bytes_skipped += len(self.buf)
                self.buf.clear()
                return out
            if i:
                self.bytes_skipped += i
                del self.buf[:i]
            if len(self.buf) < HEADER_LEN:
                return out
            ftype, length = self.buf[1], self.buf[2]
            if not _header_ok(ftype, length):
                self._reject()
                continue
            total = HEADER_LEN + length + CRC_LEN
            if len(self.buf) < total:
                return out
            body = bytes(self.buf[1:3 + length])
            if crc16_ccitt(body) != int.from_bytes(self.buf[total - 2:total], "big"):
                self._reject()
                continue
            out.append((ftype, body[2:]))
            self.frames_ok += 1
            del self.buf[:total]

    def _reject(self):
        self.bad_frames += 1
        self.bytes_skipped += 1
        del self.buf[:1]
