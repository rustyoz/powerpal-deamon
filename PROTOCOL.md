# Powerpal BLE protocol

Reverse-engineered from a Powerpal (firmware `1.0.4_3`, Nordic nRF52) on Linux/BlueZ,
cross-checked against [WeekendWarrior1/powerpal_ble](https://github.com/WeekendWarrior1/powerpal_ble)'s
ESP32 sketch.

## Advertising

- Name: `Powerpal <hex device id>`.
- Advertises service `0000fcd7-...` (Powerpal Pty Ltd's SIG allocation).
- Accepts exactly one BLE connection at a time and stops advertising while
  connected -- e.g. while the phone app is connected.

## GATT

Standard: Device Info (`0x180A`), Battery (`0x180F`, `0x2A19` = 1 byte %), Nordic
DFU (`0xFE59`).

Vendor service **`59daABCD-12F4-25A6-7D4F-55961DCE4205`**:

| UUID | name | access | payload |
|---|---|---|---|
| `59da0001` | measurement | notify, read | 20 B: `u32le ts` + `u16le pulses_in_minute` + 14 B opaque |
| `59da0002` | measurementAccess | write, indicate | write `u32le start_ts` + `u32le end_ts` to replay historic minute-records (see below) |
| `59da0003` | pulse | notify, read | `u32le` ms between the last two pulses |
| `59da0004` | time | notify, read, write | `u32le` device RTC (unix) |
| `59da0005` | firstRec | read | `u32le first_record_ts` + `u32le last_record_ts` |
| `59da0008` | ledSensitivity | r/w + n/i | sensor sensitivity |
| `59da0009` | apikey | read + n/i | 16 B cloud API key (format as UUID) |
| `59da0010` | serialNumber | read + n/i | `u32le` device id |
| `59da0011` | pairingCode | read, write + n/i | `u32le` unlock code |
| `59da0012` | millisSinceLastPulse | read | `u32le` ms since most recent pulse (counts up, resets on pulse) |
| `59da0013` | readingBatchSize | r/w + n/i | `u32le`: minute-records per `measurement` notification |

## Authentication

Two layers, both required, in order:

1. **BLE bond with MITM passkey pairing.** The 6-digit pairing code shown in the
   Powerpal app *is* the SMP passkey. Register a BlueZ pairing agent with
   `KeyboardOnly` capability, have it answer `RequestPasskey` with the code, and
   call `Device1.Pair()`. The bond then persists on that host/adapter.
2. **App-layer unlock.** Once the link is encrypted, write the same code as
   `u32le` to `59da0011`. Until that write succeeds, every vendor characteristic
   returns a locked stub (its own 128-bit UUID with a 3-byte prefix) instead of
   real data, and CCCD/value writes fail with ATT error `0x05` (insufficient
   authentication) or `0x03` (write not permitted).

**Gotcha:** an *unbonded* connection is dropped by the device ("terminated by
remote user") after ~7-11s -- typically before a host BLE stack finishes its
own GATT (re-)discovery, so libraries that don't bond first tend to fail every
time on Linux. Pair once (`setup_pairing.py` does this) and normal reconnects
work fine afterwards because the bond and GATT cache persist.

## Live data model

The meter emits one LED pulse per `1000 / pulses_per_kwh` Wh. `pulses_per_kwh`
is a constant printed on the meter (often as "imp/kWh"); it is not exposed over
BLE, so you must configure it.

- **`59da0003` pulse**: fires on every pulse with the interval (ms) since the
  previous one -> instantaneous power:
  `W = 3_600_000_000 / (period_ms * pulses_per_kwh)`.
- **`59da0001` measurement**: fires once per minute-record (set
  `readingBatchSize = 1` via `59da0013` for one notification per minute) with
  `ts` (aligned to `:00`) and `pulses` seen in that minute:
  `W_avg = pulses * 60_000 / pulses_per_kwh`, `kWh = pulses / pulses_per_kwh`.
  Bytes 6..20 of the record are constant over a short capture (looks like a
  tariff fixed-point + a static signature); not needed for power/energy and
  ignored here, same as every known third-party implementation.

## Historic records

The device buffers a rolling window of past minute-records (about 2 months in
testing -- see `59da0005` firstRec for the currently available range) and can
replay them on demand instead of only streaming live:

1. Read `59da0005` (firstRec) to get `first_ts`/`last_ts` (both `u32le` unix
   seconds), the range currently buffered on the device.
2. Subscribe to notifications on `59da0001` (measurement) as usual.
3. Write `u32le start_ts` + `u32le end_ts` (8 bytes total, write-with-response)
   to `59da0002` (measurementAccess).
4. The device replays every minute-record between `start_ts` and `end_ts` as a
   burst of ordinary `59da0001` notifications, in the same 20-byte format as
   live data, at BLE-connection-interval speed (verified: >4000 records in
   ~30s, one record per minute of wall-clock history, no gaps, no duplicates).

Notes from testing:
- Writing only `start_ts` (4 bytes) does nothing -- both bounds are required.
- `59da0002` never itself indicates anything (no explicit "done" signal); detect
  completion by watching for a record whose `ts` reaches your requested `end_ts`,
  or an idle timeout if the stream stalls.
- Once started, further writes to `59da0002` (tried `0x01`, restarting from
  `ts=0`, re-sending `start_ts` as a write-without-response) did not restart or
  interrupt an in-flight replay.
- Replayed records are indistinguishable on the wire from live ones -- a
  consumer has to track "have I seen this `ts` already" itself if it also has
  a live subscription running concurrently.

See `backfill_historic.py` for a working implementation that uses this to
recompute the daemon's lifetime energy total from device history.

## Cloud API

With `apikey` (`59da0009`) and `serialNumber` (`59da0010`) you can bypass BLE
entirely: `GET https://readings.powerpal.net/api/v1/device/<serial>` with
header `Authorization: <apikey>`.

## Prior art

- [WeekendWarrior1/powerpal_ble](https://github.com/WeekendWarrior1/powerpal_ble) -- original RE, ESP32 sketch.
- [pento/powerpal-esphome](https://github.com/pento/powerpal-esphome), [gurrier/esphome-powerpal_ble](https://github.com/gurrier/esphome-powerpal_ble) -- ESPHome components.
- [`powerpal-ble` on PyPI](https://pypi.org/project/powerpal-ble/) -- Python parser lib (no bundled Home Assistant integration).

This project differs mainly in running directly against a host's BlueZ instead
of an ESP32, and in publishing over MQTT discovery instead of a native HA
integration.
