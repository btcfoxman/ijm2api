"""Typed material constraints from an authoritative original-node quote.

Generic service errors and zero-credit quotes do not identify invalid inputs.
Only a known material-validation reason and a material field establish this
classification. Arbitrary upstream prose is never retained as diagnostic text.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

MATERIAL_REQUIREMENTS_ERROR = 'MEDIA_REQUIREMENTS_INVALID'
MATERIAL_REQUIREMENTS_MESSAGE = (
    '\u7d20\u6750\u4e0d\u7b26\u5408\u5c3a\u5bf8\u3001\u683c\u5f0f\u3001\u65f6\u957f\u7b49\u8981\u6c42\uff0c'
    '\u8bf7\u8c03\u6574\u91cd\u8bd5~'
)

_FIELDS = {
    'width_px': 'dimensions', 'height_px': 'dimensions',
    'width': 'dimensions', 'height': 'dimensions',
    'aspect_ratio': 'dimensions', 'pixel_count': 'dimensions',
    'duration_ms': 'duration', 'duration_s': 'duration', 'duration': 'duration',
    'format': 'format', 'mime_type': 'format', 'file_type': 'format', 'codec': 'format',
    'size_bytes': 'size', 'file_size_bytes': 'size', 'file_size_mb': 'size',
    'fps': 'format', 'sample_rate': 'format',
}
_FIELD = re.compile(r'materials\[(0|[1-9][0-9]{0,2})\]\.([a-z_]{1,40})\Z')
_IMAGE_DIMENSIONS = re.compile(
    r'The reference image at materials\[(0|[1-9][0-9]{0,2})\] is '
    r'([0-9]{1,7}) x ([0-9]{1,7}) px; its (width|height) must be '
    r'at (least|most) ([0-9]{1,7}) px\.?\Z', re.IGNORECASE,
)
_STRUCTURED_DIMENSIONS = {
    'width': 'actual_width_px', 'width_px': 'actual_width_px',
    'height': 'actual_height_px', 'height_px': 'actual_height_px',
    'minWidth': 'minimum_width_px', 'minWidthPx': 'minimum_width_px',
    'min_width_px': 'minimum_width_px',
    'maxWidth': 'maximum_width_px', 'maxWidthPx': 'maximum_width_px',
    'max_width_px': 'maximum_width_px',
    'minHeight': 'minimum_height_px', 'minHeightPx': 'minimum_height_px',
    'min_height_px': 'minimum_height_px',
    'maxHeight': 'maximum_height_px', 'maxHeightPx': 'maximum_height_px',
    'max_height_px': 'maximum_height_px',
}


def material_validation_from_quote(quote: Any, node_id: str) -> dict[str, Any] | None:
    """Return bounded typed facts for exactly this node, without changing quote.

The returned field and zero-based index identify the offending reference. Only
the observed, numeric image-dimension sentence is retained verbatim; other
detail strings receive a hash, preserving evidence without storing prompts,
credentials, arbitrary URLs, or user-controlled diagnostic prose.
"""
    if (not isinstance(quote, dict) or not isinstance(node_id, str) or not node_id.strip()
            or quote.get('confirmable') is True):
        return None
    items = quote.get('items')
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or items[0].get('nodeId') != node_id):
        return None
    error = items[0].get('error')
    validation = error.get('validation') if isinstance(error, dict) else None
    if (not isinstance(validation, dict)
            or validation.get('reasonCode') != 'MATERIAL_RULE_VIOLATED'):
        return None
    path = validation.get('fieldPath')
    match = _FIELD.fullmatch(path) if isinstance(path, str) else None
    if match is None or match[2] not in _FIELDS:
        return None
    index, field = int(match[1]), match[2]
    facts: dict[str, Any] = {
        'version': 1, 'source': 'node.quote', 'reason_code': 'MATERIAL_RULE_VIOLATED',
        'field_path': path, 'material_index': index, 'category': _FIELDS[field],
    }
    service_code = error.get('serviceCode')
    if type(service_code) is int and -(2 ** 31) <= service_code < 2 ** 31:
        facts['service_code'] = service_code
    for key, target in _STRUCTURED_DIMENSIONS.items():
        number = validation.get(key)
        if type(number) is int and 0 < number <= 1_000_000:
            if target in facts and facts[target] != number:
                return None
            facts[target] = number
    detail = validation.get('detail')
    if isinstance(detail, str):
        facts['detail_sha256'] = hashlib.sha256(detail.encode('utf-8')).hexdigest()
        sentence = _IMAGE_DIMENSIONS.fullmatch(detail.strip()) if len(detail) <= 600 else None
        if sentence is not None:
            if int(sentence[1]) != index or field not in {'width_px', 'height_px', 'width', 'height'}:
                return None
            axis, bound = sentence[4].lower(), sentence[5].lower()
            if field.split('_')[0] != axis:
                return None
            width, height, limit = int(sentence[2]), int(sentence[3]), int(sentence[6])
            if not all(0 < value <= 1_000_000 for value in (width, height, limit)):
                return None
            actual = width if axis == 'width' else height
            if bound == 'least' and actual >= limit or bound == 'most' and actual <= limit:
                return None
            numbers = {'actual_width_px': width, 'actual_height_px': height,
                       ('minimum_' if bound == 'least' else 'maximum_') + axis + '_px': limit}
            if any(key in facts and facts[key] != value for key, value in numbers.items()):
                return None
            facts.update(numbers)
            facts['material_type'] = 'image'
            facts['detail'] = (f'The reference image at materials[{index}] is {width} x {height} px; '
                               f'its {axis} must be at {bound} {limit} px.')
    return facts
