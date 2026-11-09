"""Credential-free typed broker calls; legacy credentials are never a fallback."""
import os


def enabled():
    if 'HA_PG_CONFIG' not in os.environ:
        return False
    from ha_egress.client import configuration
    return configuration('car-care-worker') is not None


def send(text):
    from ha_egress.client import route
    result = route('car-care-worker', 'car.telegram.send', {'text': text})
    if (type(result) is not dict or set(result) != {'message_id'}
            or type(result['message_id']) is not int or result['message_id'] <= 0):
        raise OSError('telegram_broker_unavailable')
    return True


def poll(offset):
    from ha_egress.client import route
    from app.services.telegram import TelegramUpdate
    if offset is not None and (type(offset) is not int or not 0 <= offset <= 9223372036854775807):
        raise OSError('telegram_broker_unavailable')
    result = route('car-care-worker', 'car.telegram.poll', {'offset': offset})
    if (type(result) is not dict or set(result) != {'updates'}
            or type(result['updates']) is not list or len(result['updates']) > 100):
        raise OSError('telegram_broker_unavailable')
    updates = []
    previous = -1
    for item in result['updates']:
        if (type(item) is not dict or set(item) != {'chat_id', 'text', 'update_id'}
                or type(item['chat_id']) is not str or len(item['chat_id']) > 32
                or (item['text'] is not None and (type(item['text']) is not str or len(item['text']) > 4096))
                or type(item['update_id']) is not int or not previous < item['update_id'] <= 9223372036854775807
                or (offset is not None and item['update_id'] < offset)):
            raise OSError('telegram_broker_unavailable')
        updates.append(TelegramUpdate(item['chat_id'],item['text'],item['update_id']))
        previous = item['update_id']
    return updates
