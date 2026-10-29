"""The admin list must not pass a locally allocated submit ID off as a job ID."""
from app.task_identifiers import upstream_identifiers


def test_canvas_exposes_output_resources_but_never_promotes_local_submit_or_input_refs():
    task = {'execution_backend': 'canvas_cli', 'run_id': 'wrong-legacy-id', 'upstream_context': {
        'submit_id': 'local-submit', 'resources': {'images': ['input-reference']},
        'resource_ids': ['output-resource', 'output-resource'], 'profile': 'private-profile',
        'submit_error': {'message': 'private-secret'}}}
    assert upstream_identifiers(task) == {
        'task_id': '', 'submission_id': 'local-submit', 'resource_ids': ['output-resource']}


def test_unaccepted_canvas_has_only_the_explicitly_named_submission_identifier():
    assert upstream_identifiers({'execution_backend': 'canvas_cli',
        'upstream_context': {'submit_id': 'allocated-submit'}}) == {
            'task_id': '', 'submission_id': 'allocated-submit', 'resource_ids': []}


def test_browser_retains_its_existing_upstream_lookup_id():
    assert upstream_identifiers({'execution_backend': 'browser', 'run_id': 'upstream-task'}) == {
        'task_id': 'upstream-task', 'submission_id': '', 'resource_ids': []}


def test_identifier_projection_ignores_malformed_values_without_exposing_raw_context():
    assert upstream_identifiers({'execution_backend': 'canvas_cli', 'upstream_context': {
        'submit_id': 'https://secret.invalid?token=private',
        'resource_ids': [None, 12, {}, '<script>', 'good-id'], 'credential': 'private'}}) == {
            'task_id': '', 'submission_id': '', 'resource_ids': ['good-id']}
