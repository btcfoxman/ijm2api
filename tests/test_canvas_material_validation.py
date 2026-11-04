"""Recorded rejection plus boundaries that must not become input failures."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.canvas_validation import (
    MATERIAL_REQUIREMENTS_ERROR, MATERIAL_REQUIREMENTS_MESSAGE,
    material_validation_from_quote,
)

NODE = 'node_vvnj2erj5g'
DETAIL = ('The reference image at materials[6] is 170 x 217 px; '
          'its width must be at least 300 px.')


def rejected(*, path='materials[6].width_px', detail=DETAIL, reason='MATERIAL_RULE_VIOLATED'):
    return {'confirmable': False, 'confirmationRequired': False, 'totalMaxCredits': 0,
            'items': [{'nodeId': NODE, 'error': {
                'serviceCode': 2, 'message': 'The service rejected the request as invalid.',
                'validation': {'fieldPath': path, 'reasonCode': reason, 'detail': detail},
            }}]}


def test_recorded_seventh_reference_width_is_preserved_and_quote_is_immutable():
    quote = rejected()
    original = deepcopy(quote)
    facts = material_validation_from_quote(quote, NODE)
    assert facts == {
        'version': 1, 'source': 'node.quote', 'reason_code': 'MATERIAL_RULE_VIOLATED',
        'field_path': 'materials[6].width_px', 'material_index': 6,
        'category': 'dimensions', 'service_code': 2,
        'actual_width_px': 170, 'actual_height_px': 217, 'minimum_width_px': 300,
        'detail_sha256': hashlib.sha256(DETAIL.encode()).hexdigest(),
        'material_type': 'image', 'detail': DETAIL,
    }
    assert quote == original
    assert MATERIAL_REQUIREMENTS_ERROR == 'MEDIA_REQUIREMENTS_INVALID'
    assert MATERIAL_REQUIREMENTS_MESSAGE == '\u7d20\u6750\u4e0d\u7b26\u5408\u5c3a\u5bf8\u3001\u683c\u5f0f\u3001\u65f6\u957f\u7b49\u8981\u6c42\uff0c\u8bf7\u8c03\u6574\u91cd\u8bd5~'


@pytest.mark.parametrize('quote,node', [
    (None, NODE), ({}, NODE), (rejected(), ''), (rejected(), ' '), (rejected(), 'node_other'),
    ({**rejected(), 'confirmable': True}, NODE),
    ({'items': []}, NODE), ({'items': [None]}, NODE),
    ({'items': [rejected()['items'][0], rejected()['items'][0]]}, NODE),
    ({'confirmable': True, 'totalMaxCredits': 0, 'items': [{'nodeId': NODE}]}, NODE),
    ({'items': [{'nodeId': NODE, 'error': {'serviceCode': 2}}]}, NODE),
    ({'items': [{'nodeId': NODE, 'error': {'serviceCode': 2, 'message': DETAIL}}]}, NODE),
    (rejected(reason='UNKNOWN_REASON'), NODE),
    (rejected(reason='CONTENT_REVIEW_FAILED'), NODE),
    (rejected(path='generation.duration'), NODE),
    (rejected(path='materials[6].content'), NODE),
    (rejected(path='materials[06].width_px'), NODE),
    (rejected(path='materials[-1].width_px'), NODE),
    (rejected(path='materials[1000].width_px'), NODE),
    (rejected(path='materials[6].width_px\nsecret'), NODE),
])
def test_generic_transport_prices_unknown_fields_and_foreign_nodes_are_not_material_validation(quote, node):
    assert material_validation_from_quote(quote, node) is None


@pytest.mark.parametrize('path,category', [
    ('materials[0].height_px', 'dimensions'),
    ('materials[0].aspect_ratio', 'dimensions'),
    ('materials[0].duration_ms', 'duration'),
    ('materials[0].mime_type', 'format'),
    ('materials[0].codec', 'format'),
    ('materials[0].file_size_bytes', 'size'),
])
def test_typed_non_image_constraints_keep_reference_identity_without_guessing_numeric_limits(path, category):
    facts = material_validation_from_quote(rejected(path=path, detail='Model rejected this material.'), NODE)
    assert facts['category'] == category and facts['material_index'] == 0
    assert facts['field_path'] == path
    assert 'actual_width_px' not in facts and 'detail' not in facts


def test_untrusted_arbitrary_detail_is_hashed_and_never_persisted_as_text():
    detail = 'Original private prompt: a cat; https://user:password@host/media?token=secret; authorization=secret'
    facts = material_validation_from_quote(rejected(detail=detail), NODE)
    encoded = json.dumps(facts)
    assert 'Original private prompt' not in encoded and 'password' not in encoded and 'authorization' not in encoded
    assert facts['detail_sha256'] == hashlib.sha256(detail.encode()).hexdigest()
    assert facts['field_path'] == 'materials[6].width_px'


@pytest.mark.parametrize('detail', [
    DETAIL.replace('materials[6]', 'materials[5]'),
    DETAIL.replace('its width', 'its height'),
    DETAIL.replace('170 x', '0 x'),
    DETAIL.replace('170 x', '9999999 x'),
    DETAIL.replace('170 x', '300 x'),
    DETAIL.replace('170 x', '400 x'),
])
def test_conflicting_or_impossible_numeric_dimension_evidence_is_refused(detail):
    assert material_validation_from_quote(rejected(detail=detail), NODE) is None


def test_height_maximum_sentence_keeps_original_reference_and_upper_bound():
    detail = 'The reference image at materials[0] is 1000 x 5000 px; its height must be at most 4096 px.'
    facts = material_validation_from_quote(rejected(path='materials[0].height_px', detail=detail), NODE)
    assert facts['actual_width_px'] == 1000 and facts['actual_height_px'] == 5000
    assert facts['maximum_height_px'] == 4096 and facts['material_index'] == 0


def test_conflicting_structured_constraints_and_bool_numeric_values_are_not_silently_replaced():
    quote = rejected()
    quote['items'][0]['error']['validation'].update(width=True, minWidth=300)
    assert material_validation_from_quote(quote, NODE)['actual_width_px'] == 170
    quote['items'][0]['error']['validation']['width'] = 200
    assert material_validation_from_quote(quote, NODE) is None


def test_sensitive_top_level_quote_extras_never_enter_bounded_facts():
    quote = rejected()
    quote['creditConfirmationToken'] = 'private-secret'
    quote['items'][0]['error']['validation']['authorization'] = 'private-secret'
    facts = material_validation_from_quote(quote, NODE)
    assert 'private-secret' not in json.dumps(facts)
