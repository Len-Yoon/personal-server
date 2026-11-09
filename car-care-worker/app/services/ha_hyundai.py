"""App-side vehicle proxy: provider credentials and token state stay in broker."""
from datetime import datetime
import hashlib

from app.models import VehicleSnapshot
from app.services.hyundai import HyundaiFetchResult
from app.services.vehicle_types import SUPPORTED_WARNINGS


def _call(operation, payload, request_id=None):
    from ha_egress.client import route, BrokerUnavailable
    value = route('car-care-worker', operation, payload, request_id=request_id)
    if value is None:
        raise BrokerUnavailable()
    return value


class BrokerHyundaiClient:
    def begin_authorization(self):
        result = _call('car.hyundai.begin', {})
        if set(result) != {'url'} or type(result['url']) is not str or not result['url'].startswith('https://'):
            raise OSError('invalid_vehicle_broker_result')
        return result['url']

    def complete_authorization(self, code, state=None):
        if type(code) is not str or not code or type(state) is not str or not state:
            raise ValueError('invalid authorization state')
        request_id = hashlib.sha256((code + '\0' + state).encode()).hexdigest()
        result = _call('car.hyundai.complete', {'code': code, 'state': state}, request_id)
        if result != {'connected': True}:
            raise OSError('invalid_vehicle_broker_result')
        return '현대 차량 연결이 완료되었습니다. Telegram으로 돌아가세요.'

    def fetch_snapshot(self):
        result = _call('car.hyundai.snapshot', {})
        if result == {'status': 'disabled','snapshot':None}:
            return HyundaiFetchResult.disabled()
        if set(result) != {'status', 'snapshot'} or result['status'] != 'ok':
            raise OSError('invalid_vehicle_broker_result')
        value = result['snapshot']
        if type(value) is not dict or set(value) != {'observed_at','odometer_km','dte_km','warnings','unknown_warnings'}:
            raise OSError('invalid_vehicle_broker_result')
        try:
            observed = datetime.fromisoformat(value['observed_at'])
            if (observed.utcoffset() is None or type(value['odometer_km']) is not int or value['odometer_km'] < 0
                    or (value['dte_km'] is not None and (type(value['dte_km']) is not int or value['dte_km'] < 0))
                    or any(type(value[key]) is not list or any(type(item) is not str or item not in SUPPORTED_WARNINGS
                                                             for item in value[key])
                           for key in ('warnings','unknown_warnings'))):
                raise ValueError()
            return HyundaiFetchResult.success(VehicleSnapshot(observed, value['odometer_km'], value['dte_km'],
                frozenset(value['warnings']), frozenset(value['unknown_warnings'])))
        except (ValueError, TypeError, KeyError):
            raise OSError('invalid_vehicle_broker_result') from None
