import os
import requests
import argparse
import logging
import jsonpickle

APP_ID = 'schneider-5w-id'
APP_SECRET = 'schneider-4p5If6sO_QS9F0mQLJmOoCxswng'


class Device:
    def __init__(self, access_token, name, dsn, lan_ip, key, lan_enabled) -> None:
        self.name = name
        self.dsn = dsn
        self.lan_ip = lan_ip
        self.key = key
        self.lan_enabled = lan_enabled
        self.properties = getProperties(access_token, dsn)

        if lan_enabled:
            self.Lanip = getLanip(access_token, key)


def login(email, password):
    url = 'https://user-field.aylanetworks.com/users/sign_in.json'
    data = {'user': {'email': email, 'application': {'app_id': APP_ID, 'app_secret': APP_SECRET}, 'password': password}}
    response = requests.post(url, json=data)
    respjson = response.json()

    if response.status_code != 200:
        logging.error('Login failed with error: {}'.format(respjson['error']))
        exit(1)

    return respjson['access_token']


def getDevices(access_token):
    url = 'https://ads-field.aylanetworks.com/apiv1/devices.json'
    headers = {'authorization': 'auth_token {}'.format(access_token)}
    response = requests.get(url, headers=headers)
    respjson = response.json()

    if response.status_code != 200:
        logging.error('Failed to get devices with error: {}'.format(respjson['error']))
        exit(1)

    devices = []
    for device in respjson:
        d = device['device']
        devices.append(Device(access_token, d['product_name'], d['dsn'], d['lan_ip'], d['key'], d['lan_enabled']))

    return devices


def getLanip(access_token, device_id):
    url = f'https://ads-field.aylanetworks.com/apiv1/devices/{device_id}/lan.json'
    headers = {'authorization': 'auth_token {}'.format(access_token)}
    response = requests.get(url, headers=headers)
    respjson = response.json()

    if response.status_code != 200:
        logging.error('Failed to get lanip for device {} with error: {}'.format(device_id, respjson['error']))
        exit(1)

    return respjson


def getProperties(access_token, device_serial_number):
    url = f'https://ads-field.aylanetworks.com/apiv1/dsns/{device_serial_number}/properties.json'
    headers = {'authorization': 'auth_token {}'.format(access_token)}
    response = requests.get(url, headers=headers)
    respjson = response.json()

    if response.status_code != 200:
        logging.error('Failed to get properties for device {} with error: {}'.format(device_serial_number, respjson['error']))
        exit(1)

    return respjson


def fetch_and_save(email, password, out_path):
    """Authenticate with Ayla cloud and write devices.json. Returns True on success."""
    try:
        access_token = login(email, password)
        devices = getDevices(access_token)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'w') as f:
            f.write(jsonpickle.encode(devices, indent=4, unpicklable=False))
        logging.info(f'[GetDevices] Retrieved {len(devices)} device(s) — written to {out_path}')
        return True
    except Exception as e:
        logging.error(f'[GetDevices] Failed to fetch devices: {e}')
        return False


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument('email', help='Email for the Ayla API', type=str)
    parser.add_argument('password', help='Password for the Ayla API', type=str)
    args = parser.parse_args()

    out_path = os.path.join(os.path.dirname(__file__), '..', 'json', 'devices.json')
    fetch_and_save(args.email, args.password, out_path)
