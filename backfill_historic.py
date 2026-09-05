#!/usr/bin/env python3
"""
One-off (or occasional) resync: read the Powerpal's historic minute-record
buffer over BLE and recompute the daemon's persisted lifetime energy total
from it, instead of relying only on totals accumulated from live notifications.

Useful:
  - Right after setup_pairing.py, before starting the daemon for the first
    time, so the Energy Dashboard's lifetime total starts from real history
    instead of zero.
  - Any time you suspect the running total has drifted because of missed
    live notifications (daemon downtime, BLE dropouts).

Caveats:
  - The Powerpal only buffers a rolling window of minute records (whatever
    59da0005 firstRec currently reports -- around 2 months in testing).
    Older history is gone from the device itself.
  - This corrects the *lifetime total* baseline going forward. It does not,
    and cannot, back-date Home Assistant's Energy Dashboard history graph --
    MQTT sensor states are only ever "now"; HA has no API for injecting past
    statistics via MQTT.

Stop powerpal-daemon.service first: the Powerpal only accepts one BLE link.
See PROTOCOL.md for how the historic-replay request (59da0002) was found.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

from daemon import load_config, load_state, save_state
from powerpal_protocol import (
    CH_BATCH_SIZE,
    CH_FIRST_REC,
    CH_MEASUREMENT,
    CH_MEASUREMENT_ACCESS,
    CH_PAIRING_CODE,
    device_id_from_address,
    pack_code,
    unpack_u16,
    unpack_u32,
    wh_from_pulses,
)


async def fetch(address: str, pairing_code: int, start_ts, end_ts, idle_timeout: float) -> dict:
    dev = await BleakScanner.find_device_by_address(address, timeout=20.0)
    if dev is None:
        sys.exit(
            "Device not advertising. Is powerpal-daemon.service still running?\n"
            "Stop it first: systemctl --user stop powerpal-daemon.service"
        )

    records: dict[int, int] = {}
    async with BleakClient(dev, timeout=30.0) as client:
        await client.write_gatt_char(CH_PAIRING_CODE, pack_code(pairing_code), response=False)
        await client.write_gatt_char(CH_BATCH_SIZE, pack_code(1), response=False)
        await asyncio.sleep(0.3)

        first_rec = await client.read_gatt_char(CH_FIRST_REC)
        device_first_ts = unpack_u32(first_rec, 0)
        device_last_ts = unpack_u32(first_rec, 4)
        print(f"Device buffer covers {time.ctime(device_first_ts)} .. {time.ctime(device_last_ts)}")

        start_ts = device_first_ts if start_ts is None else start_ts
        end_ts = device_last_ts if end_ts is None else end_ts
        print(f"Requesting  {time.ctime(start_ts)} .. {time.ctime(end_ts)}")

        loop = asyncio.get_running_loop()
        last_rx = loop.time()
        latest_ts_seen = 0

        def on_measurement(_, data: bytearray):
            nonlocal last_rx, latest_ts_seen
            last_rx = loop.time()
            ts = unpack_u32(data, 0)
            pulses = unpack_u16(data, 4)
            records[ts] = pulses
            latest_ts_seen = max(latest_ts_seen, ts)

        await client.start_notify(CH_MEASUREMENT, on_measurement)
        await client.write_gatt_char(
            CH_MEASUREMENT_ACCESS, pack_code(start_ts) + pack_code(end_ts), response=True
        )

        print(f"Streaming (idle timeout {idle_timeout:.0f}s)...", flush=True)
        last_progress = 0
        while True:
            await asyncio.sleep(1.0)
            if len(records) - last_progress >= 5000:
                last_progress = len(records)
                print(f"  ...{len(records)} records so far, up to {time.ctime(latest_ts_seen)}")
            if latest_ts_seen >= end_ts:
                print("Reached requested end timestamp.")
                break
            if loop.time() - last_rx > idle_timeout:
                print("No new records for a while -- stream appears finished/stalled.")
                break
        await client.stop_notify(CH_MEASUREMENT)

    return records


async def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.ini"))
    ap.add_argument("--start", type=int, default=None, help="Unix ts to start from (default: device's earliest)")
    ap.add_argument("--end", type=int, default=None, help="Unix ts to end at (default: device's latest)")
    ap.add_argument(
        "--idle-timeout", type=float, default=20.0,
        help="Seconds of no new records before assuming the stream is done (default 20)",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="Write the recomputed total to the state file (default: dry run, report only)",
    )
    ap.add_argument("--mqtt", action="store_true", help="Also publish the corrected total to MQTT (implies --apply)")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    address = cfg["device"]["address"]
    pairing_code = cfg["device"].getint("pairing_code")
    pulses_per_kwh = cfg["device"].getint("pulses_per_kwh", fallback=1000)
    state_path = Path(cfg["state"].get("file", "~/.local/state/powerpal-daemon/energy_wh.json")).expanduser()

    records = await fetch(address, pairing_code, args.start, args.end, args.idle_timeout)
    if not records:
        sys.exit("No records received -- nothing to do.")

    got_start, got_end = min(records), max(records)
    total_pulses = sum(records.values())
    total_wh = wh_from_pulses(total_pulses, pulses_per_kwh)
    print(f"\nReceived {len(records)} records spanning {time.ctime(got_start)} .. {time.ctime(got_end)}")
    print(f"Sum: {total_pulses} pulses = {total_wh / 1000.0:.3f} kWh")

    state = load_state(state_path)
    print(
        f"\nCurrent state file: total={state['total_wh'] / 1000.0:.3f} kWh "
        f"last_measurement_ts={state.get('last_measurement_ts', 0)}"
    )

    if not (args.apply or args.mqtt):
        print(
            "\nDry run -- nothing written. Re-run with --apply to overwrite the state file's "
            "total with the sum computed above."
        )
        return

    state["total_wh"] = total_wh
    state["last_measurement_ts"] = max(got_end, state.get("last_measurement_ts", 0))
    save_state(state_path, state)
    print(f"\nWrote {state_path}")

    if args.mqtt:
        import aiomqtt

        device_id = device_id_from_address(address)
        energy_topic = f"powerpal/{device_id}/energy"
        async with aiomqtt.Client(
            hostname=cfg["mqtt"].get("host", "127.0.0.1"),
            port=cfg["mqtt"].getint("port", fallback=1883),
            username=cfg["mqtt"].get("username", "") or None,
            password=cfg["mqtt"].get("password", "") or None,
        ) as mqtt:
            await mqtt.publish(energy_topic, f"{total_wh / 1000.0:.4f}", retain=True)
        print(f"Published corrected total to {energy_topic}")

    print("\nNow start/restart the daemon: systemctl --user restart powerpal-daemon.service")


if __name__ == "__main__":
    asyncio.run(main())
