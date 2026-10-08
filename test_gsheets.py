import sys
from config_loader import load_config
from main import _update_google_sheets

config = load_config('config.yaml')
_update_google_sheets('http://fake_url.com', config)
