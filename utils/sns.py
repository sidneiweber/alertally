"""Amazon SNS message signatures.

https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
"""
import base64
import logging
import re
from functools import lru_cache
from urllib.parse import urlparse

import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

# Legitimate SNS endpoints: https://sns.<region>.amazonaws.com[.cn]/...
SNS_HOST = re.compile(r'^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$')

_CONFIRMATION_FIELDS = ('Message', 'MessageId', 'SubscribeURL', 'Timestamp', 'Token', 'TopicArn', 'Type')
# Fields signed by SNS, in the order they are signed.
_SIGNED_FIELDS = {
    'Notification': ('Message', 'MessageId', 'Subject', 'Timestamp', 'TopicArn', 'Type'),
    'SubscriptionConfirmation': _CONFIRMATION_FIELDS,
    'UnsubscribeConfirmation': _CONFIRMATION_FIELDS,
}
_OPTIONAL_FIELDS = {'Subject'}
_HASHES = {'1': hashes.SHA1, '2': hashes.SHA256}


def is_sns_url(url, suffix=''):
    try:
        parsed = urlparse(url or '')
    except ValueError:
        return False
    return (parsed.scheme == 'https' and bool(SNS_HOST.match(parsed.hostname or ''))
            and parsed.path.endswith(suffix))


def _string_to_sign(envelope):
    fields = _SIGNED_FIELDS.get(envelope.get('Type'))
    if not fields:
        return None
    parts = []
    for name in fields:
        value = envelope.get(name)
        if value is None and name in _OPTIONAL_FIELDS:
            continue
        if not isinstance(value, str):
            return None
        parts.append(f'{name}\n{value}\n')
    return ''.join(parts)


@lru_cache(maxsize=16)
def _certificate(url):
    """SNS signing certificates rarely change, so each URL is fetched once per process."""
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return x509.load_pem_x509_certificate(response.content)


def verify(envelope):
    """True when the envelope carries a valid SNS signature."""
    text = _string_to_sign(envelope)
    algorithm = _HASHES.get(str(envelope.get('SignatureVersion')))
    url = envelope.get('SigningCertURL')
    if text is None or algorithm is None or not is_sns_url(url, suffix='.pem'):
        return False
    try:
        signature = base64.b64decode(envelope.get('Signature') or '', validate=True)
        _certificate(url).public_key().verify(signature, text.encode(), padding.PKCS1v15(), algorithm())
    except InvalidSignature:
        return False
    except (ValueError, TypeError, requests.RequestException) as error:
        logging.warning('Could not check the SNS signature: %s', error)
        return False
    return True
