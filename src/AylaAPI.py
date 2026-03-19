from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from AylaEncryption import AylaEncryption
from base64 import b64encode, b64decode
import logging
import json
import time
import threading
import requests

api = None


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class AylaAPIHttpServer(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default HTTP server access logs — use logging module instead

    def _send_json(self, code, body):
        payload = json.dumps(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # -----------------------------------------------------------------------
    # GET handler
    # -----------------------------------------------------------------------
    def do_GET(self):

        # --- Ayla protocol: device polls for pending commands ---
        if self.path == '/local_lan/commands.json':
            host_ip = self.client_address[0]
            device = api.get_device_by_ip(host_ip)

            if device is None:
                logging.error(f'[AylaAPI] commands.json — device not found for IP {host_ip}')
                self.send_response(500)
                self.end_headers()
                return

            if device.crypt_config is None:
                logging.warning(f'[AylaAPI] commands.json — no session for {host_ip}, ignoring')
                self.send_response(500)
                self.end_headers()
                return

            data_str = json.dumps(device.data_pending).replace(' ', '')
            data = (
                b'{"seq_no":' + str(device.seq_no).encode() +
                b',"data":' + data_str.encode() + b'}'
            )
            device.seq_no += 1

            enc, sign = device.crypt_config.encryptAndSign(data)
            resp = f'{{"enc":"{b64encode(enc).decode()}","sign":"{b64encode(sign).decode()}"}}'

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(resp.encode('utf-8'))
            device.data_pending = {}

            logging.info(f'[AylaAPI] GET commands.json — {host_ip} — plaintext: {data}')

        # --- Bridge REST API: health check ---
        elif self.path == '/api/health':
            self._send_json(200, {
                'ok': True,
                'devices': len(api.devices),
                'uptime': int(time.time() - api.start_time),
            })

        # --- Bridge REST API: status for all devices ---
        elif self.path == '/api/status':
            self._send_json(200, {'devices': [d.to_status() for d in api.devices]})

        # --- Bridge REST API: status for a single device by DSN ---
        elif self.path.startswith('/api/status/'):
            dsn = self.path[len('/api/status/'):]
            device = api.get_device_by_dsn(dsn)
            if device is None:
                self._send_json(404, {'ok': False, 'error': f'Unknown device: {dsn}'})
            else:
                self._send_json(200, device.to_status())

        else:
            self.send_response(400)
            self.end_headers()

    # -----------------------------------------------------------------------
    # POST handler
    # -----------------------------------------------------------------------
    def do_POST(self):

        # --- Ayla protocol: device initiates key exchange ---
        if self.path == '/local_lan/key_exchange.json':
            content_length = int(self.headers['Content-Length'])
            body_json = json.loads(self.rfile.read(content_length).decode('utf-8'))

            device = api.get_device_by_key_id(body_json['key_exchange']['key_id'])

            if device is None:
                logging.error(f'[AylaAPI] key_exchange — unknown key_id {body_json["key_exchange"]["key_id"]}')
                self.send_response(500)
                self.end_headers()
                return

            config = AylaEncryption(
                body_json['key_exchange']['random_1'],
                AylaEncryption.random_token(16),
                body_json['key_exchange']['time_1'],
                int(time.time() * 1000000),
                device.Lanip['lanip']['lanip_key'],
            )

            device.crypt_config = config
            device.connected = True

            resp = f'{{"random_2": "{config.SRnd2}", "time_2": {config.NTime2}}}'

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(resp.encode('utf-8'))

            logging.info(f'[AylaAPI] key_exchange — session established with {device.lan_ip} (DSN: {device.dsn})')

        # --- Ayla protocol: device pushes property update ---
        elif self.path.startswith('/local_lan/property/datapoint.json'):
            host_ip = self.client_address[0]
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            body_json = json.loads(post_data.decode('utf-8'))

            device = api.get_device_by_ip(host_ip)

            if device is None:
                logging.error(f'[AylaAPI] datapoint — device not found for IP {host_ip}')
                self.send_response(500)
                self.end_headers()
                return

            enc  = b64decode(body_json['enc'])
            sign = b64decode(body_json['sign'])
            dec  = device.crypt_config.decryptAndVerify(enc, sign)

            # Cache decrypted property values
            try:
                dec_json = json.loads(dec.decode('utf-8').rstrip('\x00'))
                logging.info(f'[AylaAPI] datapoint — {device.dsn} decrypted: {dec_json}')

                # Try both known structures: {"property": {...}} and {"data": {...}}
                prop_data = dec_json.get('property') or dec_json.get('data')
                if prop_data:
                    name = prop_data.get('name')
                    value = prop_data.get('value')
                    if name is not None and value is not None:
                        device.update_property_cache(name, value)
                        logging.info(f'[AylaAPI] datapoint — {device.dsn} {name}={value}')
            except Exception as e:
                logging.warning(f'[AylaAPI] datapoint — failed to parse decrypted body: {e}')

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(post_data)

        # --- Bridge REST API: send command to device ---
        elif self.path == '/api/command':
            content_length = int(self.headers['Content-Length'])
            body_json = json.loads(self.rfile.read(content_length).decode('utf-8'))

            dsn      = body_json.get('device')
            prop     = body_json.get('property')
            value    = body_json.get('value')

            if not dsn or prop is None or value is None:
                self._send_json(400, {'ok': False, 'error': 'Missing device, property, or value'})
                return

            device = api.get_device_by_dsn(dsn)
            if device is None:
                self._send_json(404, {'ok': False, 'error': f'Unknown device: {dsn}'})
                return

            dp = device.get_property(prop)
            if dp is None:
                self._send_json(400, {'ok': False, 'error': f'Unknown property: {prop}'})
                return

            device.set_property(prop, value)
            device.update_property_cache(prop, value)
            logging.info(f'[AylaAPI] command — {dsn} {prop}={value}')
            self._send_json(200, {'ok': True, 'property': prop, 'value': value})

        else:
            self.send_response(400)
            self.end_headers()


# ---------------------------------------------------------------------------
# DeviceProperty
# ---------------------------------------------------------------------------
class DeviceProperty:
    def __init__(self, property):
        self.property = property

    def set_value(self, value):
        self.property['value'] = value

    def toJSON(self):
        return {
            'property': {
                'base_type': self.property['base_type'],
                'value':     self.property['value'],
                'metadata':  None,
                'name':      self.property['name'],
            }
        }


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------
class Device:
    def __init__(self, name, dsn, lan_ip, key, lan_enabled, properties, Lanip):
        self.name        = name
        self.dsn         = dsn
        self.lan_ip      = lan_ip
        self.key         = key
        self.lan_enabled = lan_enabled
        self.Lanip       = Lanip

        self.crypt_config: AylaEncryption = None
        self.connected   = False
        self.seq_no      = 1
        self.data_pending = {}

        self.properties = [DeviceProperty(p['property']) for p in properties]

        # Property value cache — populated from decrypted datapoint POSTs
        # and seeded from devices.json initial values
        self._prop_cache = {}
        for p in properties:
            prop = p['property']
            self._prop_cache[prop['name']] = prop.get('value')

    def update_property_cache(self, name, value):
        self._prop_cache[name] = value

    def to_status(self):
        controllable = {
            'outlet1', 'outlet2', 'outlet3',
            'usb_charger1', 'usb_charger2',
            'led', 'led_dim_level',
        }
        return {
            'dsn':        self.dsn,
            'name':       self.name,
            'lan_ip':     self.lan_ip,
            'connected':  self.connected,
            'properties': {
                k: v for k, v in self._prop_cache.items() if k in controllable
            },
        }

    def ping(self, notify=1):
        try:
            r = requests.put(
                f'http://{self.lan_ip}/local_reg.json',
                json={'local_reg': {'uri': '/local_lan', 'notify': notify, 'ip': api.ip, 'port': api.port}},
                timeout=5,
            )
            if r.status_code != 202:
                logging.warning(f'[AylaAPI] ping — {self.lan_ip} returned {r.status_code}')
        except Exception as e:
            logging.warning(f'[AylaAPI] ping — {self.lan_ip} failed: {e}')

    def register(self):
        try:
            r = requests.post(
                f'http://{self.lan_ip}/local_reg.json',
                json={'local_reg': {'uri': '/local_lan', 'notify': 0, 'ip': api.ip, 'port': api.port}},
                timeout=5,
            )
            if r.status_code != 202:
                logging.warning(f'[AylaAPI] register — {self.lan_ip} returned {r.status_code}')
        except Exception as e:
            logging.warning(f'[AylaAPI] register — {self.lan_ip} failed: {e}')

    def get_writeable_property_names(self):
        return [dp.property['name'] for dp in self.properties if not dp.property['read_only']]

    def get_property(self, name) -> 'DeviceProperty':
        for dp in self.properties:
            if dp.property['name'] == name:
                return dp
        return None

    def set_property(self, name, value):
        prop = self.get_property(name)
        if prop is None:
            logging.error(f'[AylaAPI] set_property — unknown property {name} on {self.dsn}')
            return
        prop.set_value(value)
        if 'properties' not in self.data_pending:
            self.data_pending['properties'] = []
        self.data_pending['properties'].append(prop.toJSON())
        self.ping(notify=1)


# ---------------------------------------------------------------------------
# AylaAPI
# ---------------------------------------------------------------------------
class AylaAPI:
    server: HTTPServer
    devices: list

    def __init__(self, ip, port, devices_path='./devices.json'):
        global api

        self.ip         = ip
        self.port       = port
        self.server     = None
        self.devices    = []
        self.start_time = time.time()

        with open(devices_path, 'r') as f:
            devices_list = json.loads(f.read())

        for device in devices_list:
            self.devices.append(Device(**device))

        api = self

        threading.Thread(target=self.start, daemon=True).start()

    def get_device_by_ip(self, ip) -> Device:
        for device in self.devices:
            if device.lan_ip == ip:
                return device
        return None

    def get_device_by_dsn(self, dsn) -> Device:
        dsn_upper = dsn.upper()
        for device in self.devices:
            if device.dsn.upper() == dsn_upper:
                return device
        return None

    def get_device_by_key_id(self, key_id) -> Device:
        for device in self.devices:
            if device.Lanip['lanip']['lanip_key_id'] == key_id:
                return device
        return None

    def start(self):
        self.server = ThreadedHTTPServer((self.ip, self.port), AylaAPIHttpServer)
        logging.info(f'[AylaAPI] Server listening on {self.ip}:{self.port}')
        self.server.serve_forever()

    def stop(self):
        logging.info('[AylaAPI] Stopping server')
        self.server.shutdown()
        self.server.server_close()
