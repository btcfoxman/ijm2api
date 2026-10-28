from app.errors import public_error


def test_explicit_manual_content_message_is_preserved_for_the_terminated_task():
    message = '检测到内容可能涉及版权或受限内容，请修改后重试，额度已返还~~'
    assert public_error('OPERATOR_CONTENT_RESTRICTED', message) == {
        'code': 'OPERATOR_CONTENT_RESTRICTED', 'message': message}


def test_manual_error_code_does_not_allow_arbitrary_raw_provider_errors():
    result = public_error('OPERATOR_CONTENT_RESTRICTED', 'internal upstream secret diagnostic')
    assert result['message'] != 'internal upstream secret diagnostic'
