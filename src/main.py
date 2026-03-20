#!/usr/bin/env python3

from AylaAPI import AylaAPI, Device
from get_devices import fetch_and_save
import logging
import argparse
import os
import time
import socket
import threading

try:
    from zeroconf import ServiceInfo, Zeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logging.warning('[Main] zeroconf not installed — mDNS advertisement disabled')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0)
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()


REDISCOVER_AFTER = 3  # consecutive ping failures before subnet scan


def send_ping_forever(api: AylaAPI, device: Device, subnet=None):
    keep_alive = 10  # Match phone app's 10s keep-alive interval
    failures = 0

    logging.info(f'[Main] Registering {device.dsn} ({device.lan_ip}), then keep-alive every {keep_alive}s')
    if not device.register():
        failures += 1

    while True:
        time.sleep(keep_alive)

        if device.ping():
            if failures > 0:
                logging.info(f'[Main] {device.dsn} recovered after {failures} failure(s)')
            failures = 0
            continue

        failures += 1
        logging.warning(f'[Main] {device.dsn} ping failed ({failures}/{REDISCOVER_AFTER})')

        if failures >= REDISCOVER_AFTER:
            logging.info(f'[Main] {device.dsn} unreachable at {device.lan_ip} — starting rediscovery')
            device.connected = False
            device.crypt_config = None

            if device.rediscover_ip(subnet):
                logging.info(f'[Main] {device.dsn} rediscovered at {device.lan_ip} — re-registering')
                device.register()
                failures = 0
            else:
                logging.warning(f'[Main] {device.dsn} not found on subnet — retrying in {keep_alive}s')
                failures = REDISCOVER_AFTER  # keep triggering rediscovery each cycle


def register_mdns(ip, port, devices):
    if not ZEROCONF_AVAILABLE:
        return None, None

    dsns = [d.dsn for d in devices]
    txt  = {'dsn_count': str(len(dsns))}
    for i, dsn in enumerate(dsns):
        txt[f'dsn_{i}'] = dsn

    info = ServiceInfo(
        '_ayla-bridge._tcp.local.',
        'AylaLocalBridge._ayla-bridge._tcp.local.',
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties=txt,
        server=f'{socket.gethostname()}.local.',
    )

    zc = Zeroconf()
    zc.register_service(info)
    logging.info(f'[Main] mDNS registered: _ayla-bridge._tcp.local on {ip}:{port} with {len(dsns)} device(s)')
    return zc, info


def unregister_mdns(zc, info):
    if zc and info:
        zc.unregister_service(info)
        zc.close()
        logging.info('[Main] mDNS unregistered')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )

    local_ip = get_local_ip()

    parser = argparse.ArgumentParser(description='Ayla IoT local API bridge')
    parser.add_argument('--bind', dest='bind', type=str,
                        default=os.environ.get('BIND_IP', local_ip),
                        help='IP address to bind the bridge server on')
    parser.add_argument('--port', dest='port', type=int,
                        default=int(os.environ.get('BIND_PORT', 10275)),
                        help='Port to bind the bridge server on')
    parser.add_argument('--devices', dest='devices', type=str,
                        default=os.environ.get('DEVICES_PATH', '../json/devices.json'),
                        help='Path to devices.json')
    parser.add_argument('--subnet', dest='subnet', type=str,
                        default=os.environ.get('SUBNET', None),
                        help='Subnet prefix for device rediscovery (e.g. 192.168.1). Auto-detected from device IP if not set.')
    args = parser.parse_args()

    if not args.bind:
        logging.error('[Main] Could not determine bind IP — set BIND_IP env var or use --bind')
        raise SystemExit(1)

    logging.info(f'[Main] Starting Ayla bridge on {args.bind}:{args.port}')

    # Auto-fetch devices.json if credentials are provided and file is missing
    ayla_email = os.environ.get('APC_EMAIL')
    ayla_password = os.environ.get('APC_PASSWORD')
    if ayla_email and ayla_password and not os.path.exists(args.devices):
        logging.info(f'[Main] devices.json not found at {args.devices} — fetching from Ayla cloud')
        if not fetch_and_save(ayla_email, ayla_password, args.devices):
            logging.error('[Main] Failed to fetch devices.json — cannot start')
            raise SystemExit(1)
    elif not os.path.exists(args.devices):
        logging.error(f'[Main] {args.devices} not found — set APC_EMAIL and APC_PASSWORD to auto-fetch, or run get_devices.py manually')
        raise SystemExit(1)

    bridge = AylaAPI(args.bind, args.port, args.devices)

    # Wait for server thread to start
    while bridge.server is None:
        time.sleep(0.1)

    logging.info(f'[Main] Bridge ready — managing {len(bridge.devices)} device(s)')

    # Spawn one ping keepalive thread per device
    for device in bridge.devices:
        threading.Thread(
            target=send_ping_forever,
            args=[bridge, device, args.subnet],
            daemon=True,
            name=f'ping-{device.dsn}',
        ).start()

    # Register mDNS service
    zc, mdns_info = register_mdns(args.bind, args.port, bridge.devices)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info('[Main] Shutting down...')
        unregister_mdns(zc, mdns_info)
        bridge.stop()
