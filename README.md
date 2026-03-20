# AylaLocalAPI — Ayla IoT Local Protocol Bridge

A Python bridge that communicates with Ayla IoT devices over the local network using the Ayla LAN protocol (AES-128-CBC encrypted, HMAC-SHA256 signed). Exposes a simple REST API for external consumers like SmartThings Edge drivers or home automation systems.

Currently tested with the **APC Smart SurgeArrest** (PH6U4X32) by Schneider Electric.

## Origin & Credits

This project is a fork of [jakecrowley/AylaLocalAPI](https://github.com/jakecrowley/AylaLocalAPI) by **Jake Crowley**, who reverse-engineered the Ayla IoT local LAN protocol — including the key exchange, AES-128-CBC encryption, and HMAC-SHA256 signing. His original work established the core protocol implementation (`AylaEncryption.py`, `AylaAPI.py`, `get_devices.py`) and documented the [Ayla IoT RSA key exchange](Ayla%20IoT%20RSA%20key%20exchange.md) process.

This fork extends the original with:
- **REST bridge API** (`/api/health`, `/api/status`, `/api/command`) for external consumers
- **Physical button state detection** — POST for initial registration, PUT with `notify=1` every 10s keep-alive (matching the official APC phone app's protocol)
- **IP rediscovery** — automatic subnet scan when a device's IP changes (e.g., DHCP lease renewal)
- **Docker deployment** with host networking for Raspberry Pi
- **mDNS advertisement** (`_ayla-bridge._tcp`) for automatic discovery
- **Threaded HTTP server** to handle concurrent requests
- **Property value caching** with immediate update on commands

## How It Works

```
APC Device ◄──Ayla LAN Protocol──► Python Bridge ◄──REST API──► Consumer
(192.168.1.31)    (port 80)        (192.168.1.8:10275)          (Edge Driver, curl, etc.)
```

1. Bridge sends POST to device's `/local_reg.json` to register (triggers key exchange)
2. Device initiates key exchange — bridge derives AES session keys from shared `lanip_key`
3. Bridge sends PUT with `notify=1` every 10s to maintain the session
4. Device pushes encrypted property updates (including physical button toggles) via datapoint POSTs
5. Bridge decrypts and caches property values, serves them via REST API
6. External consumers send commands via `POST /api/command`, bridge encrypts and queues them

## Prerequisites

- **APC Smart SurgeArrest** (or other Ayla IoT device) set up via the official app
- **Python 3.11+** (or Docker)
- **Ayla cloud credentials** — needed once to retrieve `devices.json` with the device's `lanip_key`

## Quick Start

### 1. Retrieve Device Credentials

```bash
pip install -r requirements.txt
python src/get_devices.py <email> <password>
```

This authenticates with the Ayla cloud, retrieves your device info and local encryption keys, and writes `json/devices.json`.

### 2. Run with Docker (Recommended)

Edit `docker-compose.yml` to set your `BIND_IP`:

```yaml
environment:
  - BIND_IP=192.168.1.8    # Your host's LAN IP
  - BIND_PORT=10275
```

```bash
docker compose up -d --build
```

> **Note:** `network_mode: host` is required — the device connects back to the bridge IP, which must be routable from the device's perspective.

### 3. Run Directly

```bash
python src/main.py --bind 192.168.1.8 --port 10275
```

### Environment Variables

| Variable       | Default          | Description                                      |
|----------------|------------------|--------------------------------------------------|
| `BIND_IP`      | *(auto-detect)*  | IP address for the bridge to listen on           |
| `BIND_PORT`    | `10275`          | Port for the bridge REST API                     |
| `DEVICES_PATH` | `../json/devices.json` | Path to device credentials file            |
| `SUBNET`       | *(from device IP)* | Subnet prefix for IP rediscovery (e.g., `192.168.1`) |

### 4. Verify

```bash
curl http://192.168.1.8:10275/api/health
# {"ok": true, "devices": 1, "uptime": 42}

curl http://192.168.1.8:10275/api/status
# {"devices": [{"dsn": "AC000W004147567", "connected": true, "properties": {...}}]}
```

## REST API

| Endpoint              | Method | Description                        |
|-----------------------|--------|------------------------------------|
| `/api/health`         | GET    | Bridge status and device count     |
| `/api/status`         | GET    | All devices with cached properties |
| `/api/status/<dsn>`   | GET    | Single device properties           |
| `/api/command`        | POST   | Send property change to device     |

### Command Example

```bash
# Turn on outlet 1
curl -X POST http://192.168.1.8:10275/api/command \
  -H "Content-Type: application/json" \
  -d '{"device": "AC000W004147567", "property": "outlet1", "value": 1}'
```

### Status Response

```json
{
  "dsn": "AC000W004147567",
  "name": "Sun Room Smart Surge Protector",
  "lan_ip": "192.168.1.31",
  "connected": true,
  "properties": {
    "outlet1": 1,
    "outlet2": 0,
    "outlet3": 0,
    "usb_charger1": 1,
    "usb_charger2": 1,
    "led": 1,
    "led_dim_level": 100
  }
}
```

## APC Smart SurgeArrest Properties

| Property         | Type    | Description                          |
|------------------|---------|--------------------------------------|
| `outlet1`        | boolean | Switched outlet 1 (0=off, 1=on)     |
| `outlet2`        | boolean | Switched outlet 2                    |
| `outlet3`        | boolean | Switched outlet 3                    |
| `usb_charger1`   | boolean | USB charger port 1                   |
| `usb_charger2`   | boolean | USB charger port 2                   |
| `led`            | boolean | Indicator LED on/off                 |
| `led_dim_level`  | integer | LED brightness (0–100)               |

> The PH6U4X32 has 6 physical outlets but only 3 are individually switchable. The other 3 are always-on surge-protected outlets.

## File Structure

```
AylaLocalAPI/
├── src/
│   ├── AylaAPI.py          # HTTP server, Ayla protocol handler, REST API
│   ├── AylaEncryption.py   # AES-128-CBC + HMAC-SHA256 crypto (original)
│   ├── get_devices.py       # One-time Ayla cloud auth to retrieve devices.json
│   └── main.py             # Entry point, keep-alive loop, mDNS registration
├── json/
│   └── devices.json        # Device credentials (gitignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── Ayla IoT RSA key exchange.md
```

## Device IP Rediscovery

If a device's IP address changes (e.g., DHCP lease renewal, router reboot), the bridge automatically rediscovers it:

1. Keep-alive pings fail 3 consecutive times
2. Bridge scans the subnet (derived from the device's last known IP) on port 80
3. For each host with port 80 open, bridge sends a registration request
4. Device responds with a key exchange — bridge matches the `key_id` to confirm identity
5. `lan_ip` is updated in memory and normal operation resumes

The subnet can be overridden via the `SUBNET` environment variable or `--subnet` CLI argument (e.g., `192.168.1`). If not set, it's derived from the device's last known IP.

The key exchange provides cryptographic identity verification — the bridge will never accidentally connect to the wrong device.

> **Tip:** For the most reliable setup, assign a DHCP reservation on your router for the device's MAC address. Rediscovery is a safety net, not a replacement for stable addressing.

## Physical Button State Detection

A key discovery in this fork: the device only reports physical button toggles when the bridge maintains a session using the same protocol as the official APC phone app:

1. **POST** `/local_reg.json` with `notify=0` — initial registration, triggers key exchange
2. **PUT** `/local_reg.json` with `notify=1` every 10 seconds — keep-alive

Without the PUT keep-alive (or using POST for both), the device establishes a session and responds to commands but never pushes state changes for physical button presses.

## Known Limitations

- **Phone app conflict** — Running the APC phone app simultaneously causes the device to flood repeated datapoints, which can overwhelm the bridge. The phone app becomes redundant once local control is working.
- **Session persistence** — If the bridge restarts, session keys are lost. The bridge re-registers on startup and the device re-initiates key exchange.
- **Rediscovery scan time** — Subnet scan probes up to 254 hosts with a 0.3s timeout each. Worst case ~76 seconds if the device is offline entirely.
- **Single device type tested** — Only verified with APC Smart SurgeArrest (PH6U4X32). Other Ayla IoT devices may work but are untested.

## SmartThings Integration

This bridge was built to support a [SmartThings Edge driver](https://github.com/tgift00/SmartThings-Integration) that creates parent + child devices for each surge protector, with individual control of outlets, USB ports, and LED from SmartThings, Alexa, and Google Home.

## License

MIT
