"""
Reverse-engineered Powerpal BLE protocol.

Shared by setup_pairing.py (device discovery + pairing) and daemon.py
(the running bridge). See PROTOCOL.md for how this was derived.
"""
import struct

SERVICE_UUID = "59daabcd-12f4-25a6-7d4f-55961dce4205"
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

def _u(short: str) -> str:
    return f"59da{short}-12f4-25a6-7d4f-55961dce4205"

CH_MEASUREMENT      = _u("0001")   # notify/read  20B: u32le ts + u16le pulses_in_minute + 14B opaque
CH_MEASUREMENT_ACCESS = _u("0002") # write/indicate  write u32le start_ts + u32le end_ts to replay
                                    # historic minute records (same 20B format) as a burst of
                                    # CH_MEASUREMENT notifications; see PROTOCOL.md
CH_PULSE            = _u("0003")   # notify/read  u32le ms between the last two pulses
CH_FIRST_REC        = _u("0005")   # read         u32le first_ts + u32le last_ts
CH_SERIAL           = _u("0010")   # read         u32le device id
CH_PAIRING_CODE     = _u("0011")   # read/write   u32le unlock code (also the BLE SMP passkey)
CH_MILLIS_LAST_PULSE = _u("0012")  # read         u32le ms since most recent pulse
CH_BATCH_SIZE       = _u("0013")   # read/write   u32le minute-records per measurement notification


def pack_code(code: int) -> bytes:
    return struct.pack("<I", code)

def unpack_u32(b: bytes, offset: int = 0) -> int:
    return int.from_bytes(b[offset:offset + 4], "little")

def unpack_u16(b: bytes, offset: int = 0) -> int:
    return int.from_bytes(b[offset:offset + 2], "little")

def watts_from_pulse_period_ms(period_ms: int, pulses_per_kwh: int) -> float:
    """Instantaneous power from the inter-pulse period (59da0003)."""
    if not period_ms:
        return 0.0
    return 3_600_000_000.0 / (period_ms * pulses_per_kwh)

def watts_from_pulses_per_minute(pulses: int, pulses_per_kwh: int) -> float:
    """Average power over a 1-minute measurement record (59da0001)."""
    return pulses * 60_000.0 / pulses_per_kwh

def wh_from_pulses(pulses: int, pulses_per_kwh: int) -> float:
    return pulses * (1000.0 / pulses_per_kwh)

def device_id_from_address(address: str) -> str:
    """Stable, filesystem/MQTT-topic-safe id derived from the BLE address."""
    return "powerpal_" + address.replace(":", "").lower()
