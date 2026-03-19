#!/usr/bin/env python3

from AylaAPI import AylaAPI, Device
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
    s.settimeout(0)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def send_ping_forever(api: AylaAPI, device: Device):
    keep_alive = device.Lanip['lanip'].get('keep_alive', 30)
    logging.info(f'[Main] Registering {device.dsn} ({device.lan_ip}), then keep-alive every {keep_alive}s')
    device.register()
    while True:
        time.sleep(keep_alive)
        device.ping()


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
    args = parser.parse_args()

    if not args.bind:
        logging.error('[Main] Could not determine bind IP — set BIND_IP env var or use --bind')
        raise SystemExit(1)

    logging.info(f'[Main] Starting Ayla bridge on {args.bind}:{args.port}')

    bridge = AylaAPI(args.bind, args.port, args.devices)

    # Wait for server thread to start
    while bridge.server is None:
        time.sleep(0.1)

    logging.info(f'[Main] Bridge ready — managing {len(bridge.devices)} device(s)')

    # Spawn one ping keepalive thread per device
    for device in bridge.devices:
        threading.Thread(
            target=send_ping_forever,
            args=[bridge, device],
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
