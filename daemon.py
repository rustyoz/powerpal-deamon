#!/usr/bin/env python3
"""
Powerpal BLE -> MQTT bridge for Home Assistant (MQTT discovery).

Reads config.ini (see config.example.ini). Run setup_pairing.py first to
generate it. See PROTOCOL.md for how the Powerpal characteristics were
reverse-engineered.
"""
import argparse
import asyncio
import configparser
import json
import logging
from pathlib import Path

import aiomqtt
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from powerpal_protocol import (
    BATTERY_UUID,
    CH_BATCH_SIZE,
    CH_MEASUREMENT,
    CH_PAIRING_CODE,
    CH_PULSE,
    device_id_from_address,
    pack_code,
    unpack_u16,
    unpack_u32,
    watts_from_pulse_period_ms,
    wh_from_pulses,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("powerpal-daemon")


def load_config(path: Path) -> configparser.ConfigParser:
    if not path.exists():
        raise SystemExit(f"{path} not found. Run setup_pairing.py first.")
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg


def build_discovery_configs(device_id: str, device_name: str, topics: dict) -> dict:
    device_block = {
        "identifiers": [device_id],
        "name": device_name,
        "manufacturer": "Powerpal Pty Ltd",
        "model": "Powerpal",
    }

    def common(uniq, object_id, topic, extra):
        return {
            "unique_id": uniq,
            # fixes the entity_id to sensor.<object_id> instead of one derived
            # from the device name (which would embed the MAC address)
            "object_id": object_id,
            "state_topic": topic,
            "availability_topic": topics["avail"],
            "device": device_block,
            **extra,
        }

    prefix = topics["discovery_prefix"]
    return {
        f"{prefix}/sensor/powerpal_power/config": common(
            "powerpal_power", "powerpal_power", topics["power"],
            {"name": "Power", "device_class": "power",
             "unit_of_measurement": "W", "state_class": "measurement"}),
        f"{prefix}/sensor/powerpal_energy/config": common(
            "powerpal_energy", "powerpal_energy", topics["energy"],
            {"name": "Energy", "device_class": "energy",
             "unit_of_measurement": "kWh", "state_class": "total_increasing"}),
        f"{prefix}/sensor/powerpal_battery/config": common(
            "powerpal_battery", "powerpal_battery", topics["battery"],
            {"name": "Battery", "device_class": "battery",
             "unit_of_measurement": "%", "state_class": "measurement"}),
    }


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"total_wh": 0.0, "last_measurement_ts": 0}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


async def ble_loop(client_address, pairing_code, pulses_per_kwh, mqtt, topics, state, state_path):
    dev = await BleakScanner.find_device_by_address(client_address, timeout=20.0)
    if dev is None:
        raise BleakError("device not advertising (phone app connected? out of range?)")

    async with BleakClient(dev, timeout=30.0) as client:
        log.info("BLE connected")
        await client.write_gatt_char(CH_PAIRING_CODE, pack_code(pairing_code), response=False)
        await client.write_gatt_char(CH_BATCH_SIZE, pack_code(1), response=False)

        battery = await client.read_gatt_char(BATTERY_UUID)
        await mqtt.publish(topics["battery"], str(battery[0]), retain=True)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_pulse(_, data: bytearray):
            loop.call_soon_threadsafe(queue.put_nowait, ("pulse", bytes(data)))

        def on_measurement(_, data: bytearray):
            loop.call_soon_threadsafe(queue.put_nowait, ("measurement", bytes(data)))

        await client.start_notify(CH_PULSE, on_pulse)
        await client.start_notify(CH_MEASUREMENT, on_measurement)
        await mqtt.publish(topics["avail"], "online", retain=True)
        log.info("subscribed, streaming")

        last_battery_poll = loop.time()
        while client.is_connected:
            try:
                kind, data = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                if loop.time() - last_battery_poll > 600:
                    last_battery_poll = loop.time()
                    try:
                        b = await client.read_gatt_char(BATTERY_UUID)
                        await mqtt.publish(topics["battery"], str(b[0]), retain=True)
                    except BleakError:
                        pass
                continue

            if kind == "pulse":
                ms = unpack_u32(data)
                watts = watts_from_pulse_period_ms(ms, pulses_per_kwh)
                await mqtt.publish(topics["power"], f"{watts:.0f}")

            elif kind == "measurement":
                ts = unpack_u32(data, 0)
                pulses = unpack_u16(data, 4)
                if ts > state["last_measurement_ts"]:
                    state["last_measurement_ts"] = ts
                    state["total_wh"] += wh_from_pulses(pulses, pulses_per_kwh)
                    save_state(state_path, state)
                    await mqtt.publish(topics["energy"], f"{state['total_wh'] / 1000.0:.4f}")
                    log.info("minute pulses=%d total=%.3f kWh", pulses, state["total_wh"] / 1000.0)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.ini"))
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    address = cfg["device"]["address"]
    pairing_code = cfg["device"].getint("pairing_code")
    pulses_per_kwh = cfg["device"].getint("pulses_per_kwh", fallback=1000)

    mqtt_host = cfg["mqtt"].get("host", "127.0.0.1")
    mqtt_port = cfg["mqtt"].getint("port", fallback=1883)
    mqtt_user = cfg["mqtt"].get("username", "") or None
    mqtt_pass = cfg["mqtt"].get("password", "") or None
    discovery_prefix = cfg["mqtt"].get("discovery_prefix", "homeassistant")

    state_path = Path(cfg["state"].get("file", "~/.local/state/powerpal-daemon/energy_wh.json")).expanduser()

    # "powerpal" is used as-is for entity unique_id/object_id/topics so HA
    # entity_ids come out as sensor.powerpal_power etc. with no MAC in them.
    # (device_id_from_address is still used for the *device registry* grouping
    # key, which isn't visible in entity_ids -- handy if you ever run two
    # Powerpals against the same broker.)
    entity_base = "powerpal"
    device_id = device_id_from_address(address)
    # kept plain ("Powerpal", no address) since HA derives entity_id from
    # device name + entity name -- the address would otherwise end up baked
    # into every entity_id (sensor.powerpal_aa_bb_..._power)
    device_name = "Powerpal"
    topics = {
        "discovery_prefix": discovery_prefix,
        "avail": f"{entity_base}/{device_id}/available",
        "power": f"{entity_base}/{device_id}/power",
        "energy": f"{entity_base}/{device_id}/energy",
        "battery": f"{entity_base}/{device_id}/battery",
    }

    state = load_state(state_path)
    log.info("starting, device=%s resuming total_wh=%.1f", address, state["total_wh"])

    backoff = 5
    while True:
        try:
            async with aiomqtt.Client(
                hostname=mqtt_host, port=mqtt_port,
                username=mqtt_user, password=mqtt_pass,
                will=aiomqtt.Will(topic=topics["avail"], payload="offline", retain=True),
            ) as mqtt:
                for topic, payload in build_discovery_configs(device_id, device_name, topics).items():
                    await mqtt.publish(topic, json.dumps(payload), retain=True)
                log.info("MQTT connected, discovery published")

                ble_backoff = 5
                while True:
                    try:
                        await ble_loop(address, pairing_code, pulses_per_kwh, mqtt, topics, state, state_path)
                    except (BleakError, asyncio.TimeoutError, OSError) as e:
                        log.warning("BLE error: %s -- retrying in %ds", e, ble_backoff)
                        try:
                            await mqtt.publish(topics["avail"], "offline", retain=True)
                        except Exception:
                            pass
                        await asyncio.sleep(ble_backoff)
                        ble_backoff = min(ble_backoff * 2, 60)
                    else:
                        ble_backoff = 5
        except aiomqtt.MqttError as e:
            log.warning("MQTT error: %s -- retrying in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    asyncio.run(main())
