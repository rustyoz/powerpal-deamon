# powerpal-deamon

A small local daemon that reads a [Powerpal](https://powerpal.net) energy
monitor over Bluetooth LE and publishes live power/energy/battery to an MQTT
broker, using Home Assistant's MQTT discovery -- so it shows up in HA with no
manual entity configuration. No phone app, no cloud, no ESP32 required: it
talks directly to the Powerpal from a Linux host's own Bluetooth adapter.

See [PROTOCOL.md](PROTOCOL.md) for the reverse-engineered BLE protocol.

## Requirements

- Linux with BlueZ (a normal desktop/server install has this)
- Python 3.10+
- An MQTT broker reachable from this host, with Home Assistant's MQTT
  integration pointed at the same broker
- Your Powerpal's 6-digit pairing code (from the Powerpal app)

## Install

```sh
git clone <this repo> powerpal-deamon
cd powerpal-deamon
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Setup (once)

Close the Powerpal phone app first -- the device accepts only one BLE
connection, and the app will hold it.

```sh
./.venv/bin/python setup_pairing.py
```

This scans for the Powerpal, pairs with it (BLE passkey = your pairing code),
verifies the app-layer unlock, asks for your MQTT broker details and meter
constant, and writes `config.ini`. Re-run it any time to re-pair or change
settings -- it pre-fills your previous answers.

## Run

```sh
./.venv/bin/python daemon.py
```

Or install it as a persistent service:

```sh
cp powerpal-daemon.service.example ~/.config/systemd/user/powerpal-daemon.service
# edit the two ExecStart paths in that file to point at this checkout
systemctl --user daemon-reload
systemctl --user enable --now powerpal-daemon.service
loginctl enable-linger $USER   # keep it running after logout / across reboot
journalctl --user -u powerpal-daemon -f   # logs
```

## What you get in Home Assistant

Three sensors auto-created via MQTT discovery, all under one "Powerpal" device:

- `sensor.powerpal_power` -- W, live, updates on every meter pulse
- `sensor.powerpal_energy` -- kWh, lifetime total (`total_increasing`,
  add it to **Settings -> Dashboards -> Energy**)
- `sensor.powerpal_battery` -- %

Entity IDs are fixed (not derived from the device's Bluetooth address), so
they stay stable across re-pairing. This assumes one Powerpal per broker --
running a second one against the same broker will collide on these entity
IDs.

The lifetime energy total is persisted to the `state.file` path in
`config.ini` and survives daemon restarts.

## Backfilling history

The Powerpal buffers a rolling window of past minute-records (around 2
months). `backfill_historic.py` pulls that buffer over BLE and recomputes the
daemon's lifetime energy total from it, so a fresh setup doesn't start the
Energy Dashboard total at zero:

```sh
systemctl --user stop powerpal-daemon.service   # frees the BLE link
./.venv/bin/python backfill_historic.py         # dry run: reports the total, writes nothing
./.venv/bin/python backfill_historic.py --apply --mqtt
systemctl --user start powerpal-daemon.service
```

This corrects the lifetime total baseline going forward; it can't back-date
the Energy Dashboard's history graph itself (MQTT sensor states are only ever
"now"). See [PROTOCOL.md](PROTOCOL.md#historic-records) for how the historic
replay request works.

## Configuration

See [config.example.ini](config.example.ini) for the full set of options
(device address/pairing code/meter constant, MQTT connection, state file
path). `setup_pairing.py` writes `config.ini` for you; it's gitignored since
it holds credentials.

## License

[MIT](LICENSE)
