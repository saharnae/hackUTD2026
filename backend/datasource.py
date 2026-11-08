"""Where 0x20 / 0x21 frames come from and where 0x12 / 0x15 frames go.

TwinSource (default): the options pack's software twin. read_frames() returns real encoded 0x20 and 0x21
  frames built from the engine state; send_frame() decodes a frame and applies it as a pack command.
SerialSource (OFF by default, backend/config.py USE_SERIAL / LA_USE_SERIAL=1): reads frames from the board's
  UART with pyserial. Writing to the board is disabled unless SERIAL_WRITE_ENABLED (False, TODO(FINAL)).
"""

from . import _paths  # noqa: F401
from . import config as C
from .packs.options import frames_io as FIO
from .packs.options import twin as TW
import frames as F


class HardwareWriteDisabled(RuntimeError):
    pass


class TwinSource:
    kind = "twin"
    label = "SIMULATED (software twin)"

    def __init__(self, pack, seed=None):
        self.pack = pack
        self.state = pack.initial_state(C.TWIN_DEFAULT_SEED if seed is None else seed)
        self._log_t = -1.0
        self.frames_in = []                         # every frame the twin received, (type, hex) - tests / audit

    def advance(self, dt_s):
        self.state = self.pack.step(self.state, dt_s, [])
        return self.state

    def new_session(self, seed=None, regime=None):
        cmd = {"kind": "new_session"}
        if seed is not None:
            cmd["seed"] = seed
        if regime is not None:
            cmd["regime"] = regime
        self.state = self.pack.step(self.state, 0, [cmd])
        self._log_t = -1.0
        return self.state

    def read_frames(self):
        e = self.pack.engine_at(self.state)
        out = FIO.status_frame(TW.status_fields(e, self.state["config_rejects"]))
        for rec in TW.log_records(e, self._log_t):
            out += FIO.log_frame(rec)
        self._log_t = float(e.t_us)
        return out

    def send_frame(self, frame):
        t, payload = F.decode(frame)
        self.frames_in.append((t, bytes(frame).hex()))
        if t == F.T_KILL:
            self.state = self.pack.step(self.state, 0, [{"kind": "kill"}])
            return {"frame": "0x15", "applied": True, "t_us": self.state["t_us"]}
        if t == F.T_CONFIG:
            self.state = self.pack.step(self.state, 0, [{"kind": "config_frame", "payload_hex": payload.hex()}])
            lc = self.state["last_config"]
            return {"frame": "0x12", "applied": lc["applied"], "reason": lc["reason"], "t_us": self.state["t_us"]}
        return {"frame": f"0x{t:02X}", "applied": False, "reason": "the twin does not model this frame type"}


class SerialSource:
    kind = "serial"
    label = "REAL BOARD (serial port, read-only by default)"
    state = None

    def __init__(self, port=None, baud=None, write_enabled=None):
        port = port or C.SERIAL_PORT
        if not port:
            raise RuntimeError("serial data source selected but no port configured (LA_SERIAL_PORT)")
        import serial                                        # pyserial, imported only when selected
        self.ser = serial.Serial(port, baud or C.SERIAL_BAUD, timeout=0.05)
        self.write_enabled = C.SERIAL_WRITE_ENABLED if write_enabled is None else write_enabled

    def advance(self, dt_s):
        return None                                           # real time passes on its own

    def read_frames(self):
        return self.ser.read(4096)

    def send_frame(self, frame):
        if not self.write_enabled:
            raise HardwareWriteDisabled("sending frames to the real board is disabled (SERIAL_WRITE_ENABLED=False)")
        self.ser.write(bytes(frame))
        return {"frame": f"0x{bytes(frame)[1]:02X}", "applied": None, "reason": "sent; board result arrives in 0x20"}


def make_source(pack, seed=None, use_serial=None):
    if C.USE_SERIAL if use_serial is None else use_serial:
        return SerialSource()
    return TwinSource(pack, seed)
