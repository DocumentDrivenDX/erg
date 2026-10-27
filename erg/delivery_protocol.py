"""CONTRACT-006 v1. Pure, bounded wire encoding; no IO or agent imports."""
import base64
import hashlib
import json
import math
import re
import uuid

VERSION = 1
MAX_ITEMS = 500
MAX_BATCH = 2 * 1024 * 1024
CHUNK_BYTES = 512 * 1024
MAX_RECORD = 64 * 1024 * 1024
KINDS = frozenset(('session_event', 'inference_observation', 'usage_evidence',
                   'work_evidence', 'account_evidence', 'quota_evidence',
                   'host_metric', 'observed_resource', 'content_tombstone'))
IDENTITY = ('record_id', 'record_revision', 'record_kind')
ENROLLMENT = ('enrollment_id', 'installation_id', 'destination_generation')
HEX = re.compile(r'^[0-9a-f]{64}$')


class ProtocolError(ValueError):
    """Safe diagnostic: never includes user content or credentials."""


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError('invalid JSON value') from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError('duplicate JSON key')
        result[key] = value
    return result


def decode(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('non-finite JSON number')))
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid JSON encoding') from None
    # Reject overflowed finite literals (1e999), and NaN/Infinity via canonical check.
    def finite(v):
        if isinstance(v, float) and not math.isfinite(v):
            raise ProtocolError('non-finite JSON number')
        if isinstance(v, dict):
            for child in v.values(): finite(child)
        if isinstance(v, list):
            for child in v: finite(child)
    finite(value)
    return value


def _text(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ProtocolError('invalid identity')


def _integer(value, maximum=2**63 - 1, minimum=1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError('invalid integer')


def validate_payload(payload, item=None):
    if not isinstance(payload, dict):
        raise ProtocolError('payload must be an object')
    _text(payload.get('record_id'))
    _integer(payload.get('record_revision'))
    if payload.get('record_kind') not in KINDS:
        raise ProtocolError('unsupported record kind')
    if payload['record_kind'] == 'content_tombstone' and payload.get('body') is not None:
        raise ProtocolError('tombstone must not retain content')
    if item and any(payload[k] != item[k] for k in IDENTITY):
        raise ProtocolError('payload identity mismatch')
    return payload


def record_items(payload, first_sequence):
    """Allocate callers' sequences; chunk exact UTF-8 bytes, never truncate."""
    validate_payload(payload)
    raw = encode(payload)
    if len(raw) > MAX_RECORD:
        raise ProtocolError('record exceeds supported size')
    common = {k: payload[k] for k in IDENTITY}
    common['record_sha256'] = digest(raw)
    count = (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
    result = []
    for index in range(count):
        seq = first_sequence + index
        _integer(seq)
        item = dict(common, delivery_seq=seq)
        if count == 1:
            item['payload_json'] = raw.decode('utf-8')
        else:
            chunk = raw[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES]
            item.update(chunk_index=index, chunk_count=count, record_bytes=len(raw),
                        chunk_sha256=digest(chunk),
                        chunk_base64=base64.b64encode(chunk).decode('ascii'))
        result.append(item)
    return result


def validate_item(item):
    if not isinstance(item, dict):
        raise ProtocolError('item must be an object')
    _integer(item.get('delivery_seq'))
    _text(item.get('record_id'))
    _integer(item.get('record_revision'))
    if item.get('record_kind') not in KINDS or not HEX.fullmatch(str(item.get('record_sha256'))):
        raise ProtocolError('invalid record kind or digest')
    common = set(IDENTITY) | {'delivery_seq', 'record_sha256'}
    if 'payload_json' in item:
        if set(item) != common | {'payload_json'} or not isinstance(item['payload_json'], str):
            raise ProtocolError('invalid payload fields')
        try: raw = item['payload_json'].encode('utf-8')
        except UnicodeError: raise ProtocolError('invalid payload encoding') from None
        if len(raw) > CHUNK_BYTES or digest(raw) != item['record_sha256']:
            raise ProtocolError('invalid payload size or digest')
        validate_payload(decode(raw), item)
    else:
        fields = {'chunk_index', 'chunk_count', 'record_bytes', 'chunk_sha256', 'chunk_base64'}
        if set(item) != common | fields:
            raise ProtocolError('invalid chunk fields')
        _integer(item['record_bytes'], MAX_RECORD)
        expected = (item['record_bytes'] + CHUNK_BYTES - 1) // CHUNK_BYTES
        _integer(item['chunk_count'], MAX_RECORD // CHUNK_BYTES)
        _integer(item['chunk_index'], item['chunk_count'] - 1, 0)
        if item['chunk_count'] != expected or expected < 2:
            raise ProtocolError('invalid chunk manifest')
        try: chunk = base64.b64decode(item['chunk_base64'], validate=True)
        except (ValueError, TypeError): raise ProtocolError('invalid chunk encoding') from None
        size = min(CHUNK_BYTES, item['record_bytes'] - item['chunk_index'] * CHUNK_BYTES)
        if len(chunk) != size or digest(chunk) != item['chunk_sha256']:
            raise ProtocolError('invalid chunk size or digest')
    return item


def batch(enrollment, items, batch_id=None):
    value = dict(enrollment, protocol_version=VERSION,
                 batch_id=batch_id or str(uuid.uuid4()), items=items)
    raw = encode(value)
    validate_batch(raw, digest(raw))
    return raw


def validate_batch(raw, expected_digest):
    if not isinstance(raw, bytes) or len(raw) > MAX_BATCH:
        raise ProtocolError('batch exceeds supported size')
    if digest(raw) != expected_digest:
        raise ProtocolError('batch digest mismatch')
    value = decode(raw)
    if not isinstance(value, dict) or set(value) != set(ENROLLMENT) | {'protocol_version', 'batch_id', 'items'}:
        raise ProtocolError('invalid envelope fields')
    if type(value['protocol_version']) is not int or value['protocol_version'] != VERSION:
        raise ProtocolError('unsupported protocol version')
    for key in ENROLLMENT: _text(value[key])
    try:
        if str(uuid.UUID(value['batch_id'])) != value['batch_id']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ProtocolError('invalid batch ID') from None
    items = value['items']
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ProtocolError('invalid batch item count')
    last = 0
    for item in items:
        validate_item(item)
        if item['delivery_seq'] <= last:
            raise ProtocolError('unordered or repeated sequence')
        last = item['delivery_seq']
    return value


def reconstruct(items, as_bytes=False):
    """Return only a complete, verified record; partial chunks remain a gap."""
    if not items:
        raise ProtocolError('incomplete record')
    for item in items: validate_item(item)
    first = items[0]
    if any(any(i[k] != first[k] for k in IDENTITY + ('record_sha256',)) for i in items):
        raise ProtocolError('conflicting record identity')
    if 'payload_json' in first:
        if any(i.get('payload_json') != first['payload_json'] for i in items):
            raise ProtocolError('conflicting payloads')
        raw = first['payload_json'].encode('utf-8')
        payload = validate_payload(decode(raw), first)
        return raw if as_bytes else payload
    chunks = {}
    for item in items:
        if any(item.get(k) != first[k] for k in ('chunk_count', 'record_bytes')):
            raise ProtocolError('conflicting chunk manifest')
        index = item['chunk_index']
        chunk = base64.b64decode(item['chunk_base64'])
        if index in chunks and chunks[index] != chunk:
            raise ProtocolError('conflicting chunk')
        chunks[index] = chunk
    if len(chunks) != first['chunk_count']:
        raise ProtocolError('incomplete record')
    raw = b''.join(chunks[i] for i in range(first['chunk_count']))
    if len(raw) != first['record_bytes'] or digest(raw) != first['record_sha256']:
        raise ProtocolError('reconstructed digest mismatch')
    payload = validate_payload(decode(raw), first)
    return raw if as_bytes else payload
