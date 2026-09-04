#!/usr/bin/env python3
"""
Scan for a Powerpal, pair with it (BLE MITM passkey pairing, needed before any
characteristic besides the service UUID advert is readable), verify the app-layer
unlock, then write everything out to config.ini.

Run this once per Powerpal / per host. Re-run it any time to re-pair or to
change the MQTT settings -- it pre-fills defaults from the existing config.ini.
"""
import asyncio
import configparser
import struct
import sys
from pathlib import Path

from bleak import BleakClient, BleakScanner
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from powerpal_protocol import (
    CH_BATCH_SIZE,
    CH_PAIRING_CODE,
    pack_code,
    unpack_u32,
)

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.ini"
ADAPTER = "/org/bluez/hci0"
AGENT_PATH = "/powerpal_setup/agent"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


async def scan_for_powerpal():
    print("Scanning for Powerpal devices (10s)...")
    found = []
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
    for addr, (dev, adv) in devices.items():
        name = adv.local_name or dev.name or ""
        if "powerpal" in name.lower():
            found.append((addr, name, adv.rssi))
    if not found:
        print("No Powerpal advertising. Make sure the Powerpal phone app is fully")
        print("closed (it holds the only BLE connection slot) and try again.")
        sys.exit(1)
    if len(found) == 1:
        addr, name, rssi = found[0]
        print(f"Found: {name}  {addr}  (rssi {rssi})")
        return addr
    print("Multiple Powerpals found:")
    for i, (addr, name, rssi) in enumerate(found):
        print(f"  [{i}] {name}  {addr}  (rssi {rssi})")
    idx = int(ask("Pick one", "0"))
    return found[idx][0]


class PasskeyAgent(ServiceInterface):
    """BlueZ pairing agent that supplies the Powerpal's fixed passkey."""

    def __init__(self, passkey: int):
        super().__init__("org.bluez.Agent1")
        self.passkey = passkey

    @method()
    def Release(self):
        pass

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        return self.passkey

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        pass

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
        pass

    @method()
    def Cancel(self):
        pass


async def bond(address: str, passkey: int):
    devpath = "/org/bluez/hci0/dev_" + address.replace(":", "_")
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    bus.export(AGENT_PATH, PasskeyAgent(passkey))

    mgr_intro = await bus.introspect("org.bluez", "/org/bluez")
    mgr = bus.get_proxy_object("org.bluez", "/org/bluez", mgr_intro).get_interface(
        "org.bluez.AgentManager1"
    )
    await mgr.call_register_agent(AGENT_PATH, "KeyboardOnly")
    await mgr.call_request_default_agent(AGENT_PATH)

    root_intro = await bus.introspect("org.bluez", "/")
    om = bus.get_proxy_object("org.bluez", "/", root_intro).get_interface(
        "org.freedesktop.DBus.ObjectManager"
    )
    objs = await om.call_get_managed_objects()
    if devpath not in objs:
        ad_intro = await bus.introspect("org.bluez", ADAPTER)
        adapter = bus.get_proxy_object("org.bluez", ADAPTER, ad_intro).get_interface(
            "org.bluez.Adapter1"
        )
        await adapter.call_start_discovery()
        for _ in range(40):
            await asyncio.sleep(0.5)
            objs = await om.call_get_managed_objects()
            if devpath in objs:
                break
        try:
            await adapter.call_stop_discovery()
        except Exception:
            pass
    if devpath not in objs:
        print(f"Could not find {address} on the bus.")
        sys.exit(1)

    dev_intro = await bus.introspect("org.bluez", devpath)
    dev_obj = bus.get_proxy_object("org.bluez", devpath, dev_intro)
    dev = dev_obj.get_interface("org.bluez.Device1")
    props = dev_obj.get_interface("org.freedesktop.DBus.Properties")

    already = (await props.call_get("org.bluez.Device1", "Paired")).value
    if not already:
        print("Pairing (BLE passkey exchange)...")
        try:
            await dev.call_pair()
        except Exception as e:
            print(f"Pair() reported: {e!r} (continuing -- often benign)")
        try:
            await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
        except Exception:
            pass
    else:
        print("Already paired/bonded with this host.")

    if not (await props.call_get("org.bluez.Device1", "Connected")).value:
        await dev.call_connect()
    for _ in range(40):
        if (await props.call_get("org.bluez.Device1", "ServicesResolved")).value:
            break
        await asyncio.sleep(0.5)

    return bus, devpath


async def unlock_and_verify(bus, devpath: str, code: int) -> bool:
    """Write + verify the pairing code over the same D-Bus connection used to
    pair. (bleak refuses to connect-by-address unless its own scanner found
    the device first, which it won't while BlueZ already holds the link --
    so we drive the GATT write via BlueZ's D-Bus API directly here.)"""
    print("Unlocking...")
    root_intro = await bus.introspect("org.bluez", "/")
    om = bus.get_proxy_object("org.bluez", "/", root_intro).get_interface(
        "org.freedesktop.DBus.ObjectManager"
    )
    objs = await om.call_get_managed_objects()
    chars = {}
    for path, ifaces in objs.items():
        if not path.startswith(devpath):
            continue
        c = ifaces.get("org.bluez.GattCharacteristic1")
        if c:
            chars[c["UUID"].value.lower()] = path

    async def char_iface(path):
        intro = await bus.introspect("org.bluez", path)
        return bus.get_proxy_object("org.bluez", path, intro).get_interface(
            "org.bluez.GattCharacteristic1"
        )

    pairing_path = chars.get(CH_PAIRING_CODE)
    batch_path = chars.get(CH_BATCH_SIZE)
    if not pairing_path:
        print("pairingCode characteristic not found -- GATT discovery incomplete?")
        return False

    pairing_char = await char_iface(pairing_path)
    await pairing_char.call_write_value(pack_code(code), {"type": Variant("s", "command")})
    if batch_path:
        batch_char = await char_iface(batch_path)
        await batch_char.call_write_value(pack_code(1), {"type": Variant("s", "command")})

    readback = await pairing_char.call_read_value({})
    ok = unpack_u32(bytes(readback)) == code
    print("Unlock OK." if ok else f"Unlock FAILED, read back {bytes(readback).hex()}.")
    return ok


def load_existing_defaults():
    cfg = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH)
    return cfg


def write_config(address, code, pulses_per_kwh, mqtt_host, mqtt_port, mqtt_user,
                  mqtt_pass, discovery_prefix, state_file):
    cfg = configparser.ConfigParser()
    cfg["device"] = {
        "address": address,
        "pairing_code": str(code),
        "pulses_per_kwh": str(pulses_per_kwh),
    }
    cfg["mqtt"] = {
        "host": mqtt_host,
        "port": str(mqtt_port),
        "username": mqtt_user,
        "password": mqtt_pass,
        "discovery_prefix": discovery_prefix,
    }
    cfg["state"] = {"file": state_file}
    with open(CONFIG_PATH, "w") as f:
        cfg.write(f)
    print(f"\nWrote {CONFIG_PATH}")


async def main():
    existing = load_existing_defaults()
    dev_defaults = existing["device"] if existing.has_section("device") else {}
    mqtt_defaults = existing["mqtt"] if existing.has_section("mqtt") else {}
    state_defaults = existing["state"] if existing.has_section("state") else {}

    address = dev_defaults.get("address") or await scan_for_powerpal()
    print(f"\nUsing device: {address}")

    code = ask("Pairing code (from the Powerpal app)", dev_defaults.get("pairing_code", ""))
    if not code.isdigit():
        print("Pairing code must be numeric.")
        sys.exit(1)
    code = int(code)

    bus, devpath = await bond(address, code)

    pulses_per_kwh = int(ask("Meter constant (pulses per kWh, check your meter's sticker)",
                              dev_defaults.get("pulses_per_kwh", "1000")))

    ok = await unlock_and_verify(bus, devpath, code)
    if not ok:
        print("Setup did not verify cleanly -- double check the pairing code and retry.")
        sys.exit(1)

    print("\nMQTT broker settings (blank username/password if the broker has no auth):")
    mqtt_host = ask("  host", mqtt_defaults.get("host", "127.0.0.1"))
    mqtt_port = ask("  port", mqtt_defaults.get("port", "1883"))
    mqtt_user = ask("  username", mqtt_defaults.get("username", "homeassistant"))
    mqtt_pass = ask("  password", mqtt_defaults.get("password", ""))
    discovery_prefix = ask("  HA discovery prefix", mqtt_defaults.get("discovery_prefix", "homeassistant"))
    state_file = ask("State file path", state_defaults.get(
        "file", "~/.local/state/powerpal-daemon/energy_wh.json"))

    write_config(address, code, pulses_per_kwh, mqtt_host, mqtt_port, mqtt_user,
                 mqtt_pass, discovery_prefix, state_file)
    print("Done. Run `python daemon.py` to start streaming, or install the systemd service.")


if __name__ == "__main__":
    asyncio.run(main())
