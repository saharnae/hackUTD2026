"""Read and decode the FPGA's 0x20 status frames.
Usage: python tools/read_status.py COM3 [baud]      (baud defaults to 1000000)
Needs: adapter RXD on Arduino IO1 (tx_o), adapter TXD on IO0 (rx_in), GND to GND.
Type nothing; it prints one line per good frame. In the bring-up design every byte this script sends (--poke) is counted in orders=.
"""
import sys, time
from gen_status_frame import crc16, unpack_payload

def parse_stream(buf, out):
    """Pull complete frames out of buf (a bytearray). Returns number of CRC failures."""
    bad = 0
    while True:
        i = buf.find(b"\x5A")
        if i < 0: buf.clear(); return bad
        del buf[:i]
        if len(buf) < 3: return bad
        ln = buf[2]
        total = 3 + ln + 2
        if len(buf) < total: return bad
        body = bytes(buf[1:3 + ln]); rx = (buf[3 + ln] << 8) | buf[4 + ln]
        if crc16(body) == rx: out.append((buf[1], bytes(buf[3:3 + ln]))); del buf[:total]
        else: bad += 1; del buf[:1]

def show(t, p):
    if t != 0x20 or len(p) != 33: return f"type 0x{t:02X} len {len(p)} (not a 33-byte status frame)"
    d = unpack_payload(p); st = d["state"]
    names = ["armed", "tripped", "book_ready", "snapshot_ok", "hb_ok", "shock_busy", "ARM", "kill"]
    flags = " ".join(f"{n}={(st >> i) & 1}" for i, n in enumerate(names))
    return (f"now={d['now_us']}us {flags} trip={d['trip_reason']} rej={d['last_reject']} tok={d['tokens']} "
            f"snap={d['snapshot_id']} ref={d['ref_seq']} feed={d['feed_seq']} S={d['S']} eq={d['equity_x6400']} "
            f"orders={d['orders_sent']} fills={d['fills']} lat={d['last_latency_ns']}ns "
            f"bad={d['frames_bad']} cfgrej={d['config_rejects']} logdrop={d['log_drops']}")

def main(port, baud, poke):
    import serial
    s = serial.Serial(port, baud, timeout=0.2)
    buf = bytearray(); frames = []; bad = 0; t0 = time.time(); last = 0
    while True:
        buf += s.read(256)
        bad += parse_stream(buf, frames)
        for t, p in frames: print(show(t, p))
        frames.clear()
        if poke and time.time() - last > 1: s.write(b"x"); last = time.time()
        if time.time() - t0 > 5 and bad: print(f"({bad} CRC failures so far)"); t0 = time.time()

if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit(__doc__)
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 1_000_000, "--poke" in sys.argv)
