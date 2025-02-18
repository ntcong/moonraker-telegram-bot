# Todo: class for printer states!
import configparser
import logging
import re
import time

import emoji
import requests
import urllib
from datetime import datetime, timedelta
from urllib.request import urlopen
from PIL import Image
from io import BytesIO

from power_device import PowerDevice

logger = logging.getLogger(__name__)


class Klippy:
    def __init__(self, config: configparser.ConfigParser, light_device: PowerDevice, psu_device: PowerDevice, logging_handler: logging.Handler = None, debug_logging: bool = False):
        self._host = config.get('bot', 'server', fallback='localhost')
        self._disabled_macros = [el.strip() for el in config.get('telegram_ui', 'disabled_macros').split(',')] if 'telegram_ui' in config and 'disabled_macros' in config['telegram_ui'] else list()
        self._eta_source: str = config.get('bot', 'eta_source', fallback='slicer')
        self._light_device = light_device
        self._psu_device = psu_device
        self._sensors_list: list = [el.strip() for el in config.get('bot', 'sensors').split(',')] if 'bot' in config and 'sensors' in config['bot'] else []
        self._heates_list: list = [el.strip() for el in config.get('bot', 'heaters').split(',')] if 'bot' in config and 'heaters' in config['bot'] else []
        self._sensors_dict: dict = self._prepare_sens_dict()
        self._sensors_query = '&' + '&'.join(self._sensors_dict.values())

        self.connected: bool = False
        self.printing: bool = False
        self.paused: bool = False
        self.printing_duration: float = 0.0
        self.printing_progress: float = 0.0
        self._printing_filename: str = ''
        self.file_estimated_time: float = 0.0
        self.file_print_start_time: float = 0.0
        self.vsd_progress: float = 0.0

        self.filament_used: float = 0.0
        self.filament_total: float = 0.0
        self.filament_weight: float = 0.0
        self._thumbnail_path = ''

        if logging_handler:
            logger.addHandler(logging_handler)
        if debug_logging:
            logger.setLevel(logging.DEBUG)

    def _prepare_sens_dict(self):
        sens_dict = {}
        for heat in self._heates_list:
            if heat in ['extruder', 'heater_bed']:
                sens_dict[heat] = heat
            else:
                sens_dict[heat] = f"heater_generic {heat}"

        for sens in self._sensors_list:
            sens_dict[sens] = f"temperature_sensor {sens}"
        return sens_dict

    @property
    def macros(self):
        return self._get_marco_list()

    @property
    def printing_filename(self):
        return self._printing_filename

    @property
    def printing_filename_with_time(self):
        return f"{self._printing_filename}_{datetime.fromtimestamp(self.file_print_start_time):%Y-%m-%d_%H-%M}"

    @property
    def moonraker_host(self):
        return self._host

    def _reset_file_info(self) -> None:
        self._printing_filename = ''
        self.file_estimated_time = 0.0
        self.file_print_start_time = 0.0
        self.filament_total = 0.0
        self.filament_weight = 0.0
        self._thumbnail_path = ''

    @printing_filename.setter
    def printing_filename(self, new_value: str):
        if not new_value:
            self._reset_file_info()
            return

        response = requests.get(f"http://{self._host}/server/files/metadata?filename={urllib.parse.quote(new_value)}")
        # Todo: add response status check!
        resp = response.json()['result']
        self._printing_filename = new_value
        self.file_estimated_time = resp['estimated_time']
        self.file_print_start_time = resp['print_start_time'] if resp['print_start_time'] else time.time()
        self.filament_total = resp['filament_total'] if 'filament_total' in resp else 0.0
        self.filament_weight = resp['filament_weight_total'] if 'filament_weight_total' in resp else 0.0

        if 'thumbnails' in resp:
            thumb = max(resp['thumbnails'], key=lambda el: el['size'])
            self._thumbnail_path = thumb['relative_path']

    def _get_marco_list(self) -> list:
        resp = requests.get(f'http://{self._host}/printer/objects/list')
        if not resp.ok:
            return list()
        macro_lines = list(filter(lambda it: 'gcode_macro' in it and ' _' not in it, resp.json()['result']['objects']))
        loaded_macros = list(map(lambda el: el.split(' ')[1], macro_lines))
        return [key for key in loaded_macros if key not in self._disabled_macros]

    def check_connection(self) -> bool:
        try:
            response = requests.get(f"http://{self._host}/printer/info", timeout=2)
            return True if response.ok else False
        except Exception:
            return False

    @staticmethod
    def sensor_message(sensor: str, sens_key: str, response) -> str:
        if sens_key not in response or not response[sens_key]:
            return ''

        sens_name = re.sub(r"([A-Z]|\d|_)", r" \1", sensor).replace('_', '')
        message = f"{sens_name.title()}: {round(response[sens_key]['temperature'])}"
        if 'target' in response[sens_key]:
            if response[sens_key]['target'] > 0.0:
                message += emoji.emojize(' :arrow_right: ', use_aliases=True) + f"{round(response[sens_key]['target'])}"
            if response[sens_key]['power'] > 0.0:
                message += emoji.emojize(' :fire: ', use_aliases=True)
        message += '\n'
        return message

    def get_status(self) -> str:
        response = requests.get(f"http://{self._host}/printer/objects/query?webhooks&print_stats&display_status{self._sensors_query}")
        resp = response.json()['result']['status']
        print_stats = resp['print_stats']
        webhook = resp['webhooks']
        message = emoji.emojize(':robot: Klipper status: ', use_aliases=True) + f"{webhook['state']}\n"

        if 'display_status' in resp and 'message' in resp['display_status']:
            msg = resp['display_status']['message']
            if msg and msg is not None:
                message += f"{msg}\n"
        if 'state_message' in webhook:
            message += f"State message: {webhook['state_message']}\n"

        message += emoji.emojize(':mechanical_arm: Printing process status: ', use_aliases=True) + f"{print_stats['state']} \n"

        if print_stats['state'] == 'printing':
            if not self.printing_filename:
                self.printing_filename = print_stats['filename']
            message += f"Printing filename: {self.printing_filename} \n"
        elif print_stats['state'] == 'paused':
            message += f"Printing paused\n"
        elif print_stats['state'] == 'complete':
            pass

        for sens, s_key in self._sensors_dict.items():
            message += self.sensor_message(sens, s_key, resp)

        if self._light_device:
            message += emoji.emojize(':flashlight: Light: ', use_aliases=True) + f"{'on' if self._light_device.device_state else 'off'}\n"
        if self._psu_device:
            message += emoji.emojize(':electric_plug: PSU: ', use_aliases=True) + f"{'on' if self._psu_device.device_state else 'off'}\n"

        return message

    def execute_command(self, command: str):
        data = {'commands': [f'{command}']}
        res = requests.post(f"http://{self._host}/api/printer/command", json=data)
        if not res.ok:
            logger.error(res.reason)

    def _get_eta(self) -> timedelta:
        if self._eta_source == 'slicer':
            eta = int(self.file_estimated_time - self.printing_duration)
        else:  # eta by file
            eta = int(self.printing_duration / self.vsd_progress - self.printing_duration)
        if eta < 0:
            eta = 0
        return timedelta(seconds=eta)

    def get_eta_message(self) -> str:
        eta = self._get_eta()
        return f"Estimated time left: {eta}\nFinish at {datetime.now() + eta:%Y-%m-%d %H:%M}\n"

    def get_file_info(self, message: str = '') -> (str, BytesIO):
        message += f"Printed {round(self.printing_progress * 100, 0)}%\n"
        if self.filament_total > 0.0:
            message += f"Filament: {round(self.filament_used / 1000, 2)}m / {round(self.filament_total / 1000, 2)}m,"
            if self.filament_weight > 0.0:
                message += f" weight: {self.filament_weight}g"
        message += '\n'

        message += self.get_eta_message()

        if self._thumbnail_path:
            img = Image.open(urlopen(f"http://{self._host}/server/files/gcodes/{urllib.parse.quote(self._thumbnail_path)}")).convert('RGB')

            bio = BytesIO()
            bio.name = f'{self.printing_filename}.webp'
            img.save(bio, 'WebP', quality=0, lossless=True)
            bio.seek(0)

            img.close()

            return message, bio
        else:
            return message, None

    def get_gcode_files(self):
        response = requests.get(f"http://{self._host}/server/files/list?root=gcodes")
        resp = response.json()
        files = sorted(resp['result'], key=lambda item: item['modified'], reverse=True)[:5]
        return files

    def upload_file(self, file: BytesIO) -> bool:
        response = requests.post(f"http://{self._host}/server/files/upload", files={'file': file})
        return response.ok

    def start_printing_file(self, filename: str) -> bool:
        response = requests.post(f"http://{self._host}/printer/print/start?filename={urllib.parse.quote(filename)}")
        return response.ok

    def stop_all(self):
        self._reset_file_info()
