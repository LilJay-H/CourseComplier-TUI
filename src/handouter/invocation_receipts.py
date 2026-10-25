"""Host process observations. Claims returned by an Agent never populate these."""
import hashlib
import json
from datetime import datetime, timezone
import uuid
import re
from contextlib import contextmanager
from contextvars import ContextVar

_CAPTURE = ContextVar('handouter_host_invocations', default=None)
_BUDGET = ContextVar('handouter_host_invocation_budget', default=None)


@contextmanager
def capture_observations(*, max_calls=None, max_input_bytes=8 * 1024 * 1024):
    """Host-only in-memory collection; no files mutate during Agent inventory checks."""
    if _CAPTURE.get() is not None:
        raise ValueError('INVOCATION_CAPTURE_ALREADY_ACTIVE')
    items = []
    token = _CAPTURE.set(items)
    budget_token = _BUDGET.set({'max_calls': max_calls, 'max_input_bytes': max_input_bytes})
    try:
        yield items
    finally:
        _CAPTURE.reset(token)
        _BUDGET.reset(budget_token)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def input_identity(snapshot, prompt):
    return sha(encoded({'files': {p: sha(data) for p, data in sorted(snapshot.items())},
                        'prompt_sha256': sha(prompt.encode('utf-8'))}))


def begin_observation(command, snapshot, prompt, transport, model):
    captured = _CAPTURE.get()
    size = sum(len(data) for data in snapshot.values()) + len(prompt.encode('utf-8'))
    if captured is not None:
        budget = _BUDGET.get()
        if budget['max_calls'] is not None and len(captured) >= budget['max_calls']:
            raise ValueError('INVOCATION_CALL_BUDGET_EXHAUSTED')
        if size > budget['max_input_bytes']:
            raise ValueError('INVOCATION_INPUT_BUDGET_EXHAUSTED')
    started = dict(schema_version=1, invocation_id=uuid.uuid4().hex, transport=transport,
                agent_executable=command[0], command_identity=sha(encoded(command)),
                cli_requested=transport, cli_observed_version=None, model_requested=model,
                model_resolved=None, started_at=datetime.now(timezone.utc).isoformat(),
                input_snapshot_sha256=input_identity(snapshot, prompt), replay=False,
                attestation_level='host_invocation_observed')
    if captured is not None:
        captured.append({'started': dict(started), 'command': list(command),
                         'input_files': {path: sha(data) for path, data in sorted(snapshot.items())},
                         'prompt_sha256': sha(prompt.encode('utf-8')), 'input_bytes': size, 'receipt': None})
    return started


def finish_observation(started, returncode, result, errors):
    body = dict(started, completed_at=datetime.now(timezone.utc).isoformat(),
                process_returncode=returncode, result_sha256=sha(result) if result is not None else None,
                integrity_errors=list(errors))
    receipt = {'observation': body, 'sha256': sha(encoded(body))}
    captured = _CAPTURE.get()
    if captured is not None:
        matches = [item for item in captured if item['started']['invocation_id'] == started['invocation_id']]
        if len(matches) != 1 or matches[0]['receipt'] is not None:
            raise ValueError('INVOCATION_CAPTURE_BINDING_CHANGED')
        matches[0]['receipt'] = receipt
        try:
            from .authoring_v2 import strict_json
            matches[0]['result_canonical_sha256'] = sha(encoded(strict_json(result))) if result is not None else None
        except (ValueError, TypeError):
            matches[0]['result_canonical_sha256'] = None
    return receipt


def validate_observation(receipt, *, expected_sha256=None, input_sha256=None, result_sha256=None):
    """The expected digest must come from Host storage, never an Agent claim."""
    if not isinstance(receipt, dict) or set(receipt) != {'observation', 'sha256'}:
        raise ValueError('INVOCATION_RECEIPT_INVALID')
    body = receipt['observation']
    fields = {'schema_version', 'invocation_id', 'transport', 'agent_executable', 'command_identity',
              'cli_requested', 'cli_observed_version', 'model_requested', 'model_resolved', 'started_at',
              'input_snapshot_sha256', 'replay', 'attestation_level', 'completed_at',
              'process_returncode', 'result_sha256', 'integrity_errors'}
    if (not isinstance(body, dict) or set(body) != fields or type(body['schema_version']) is not int
            or body['schema_version'] != 1 or body['replay'] is not False
            or body['attestation_level'] != 'host_invocation_observed'
            or type(body['process_returncode']) is not int
            or not isinstance(body['invocation_id'], str) or not re.fullmatch('[0-9a-f]{32}', body['invocation_id'])
            or any(not isinstance(body[key], str) or not re.fullmatch('[0-9a-f]{64}', body[key])
                   for key in ('command_identity', 'input_snapshot_sha256'))
            or not isinstance(body['integrity_errors'], list)
            or any(not isinstance(item, str) for item in body['integrity_errors'])
            or sha(encoded(body)) != receipt['sha256']
            or expected_sha256 is not None and receipt['sha256'] != expected_sha256
            or input_sha256 is not None and body.get('input_snapshot_sha256') != input_sha256
            or result_sha256 is not None and body.get('result_sha256') != result_sha256):
        raise ValueError('INVOCATION_RECEIPT_CHANGED')
    return body


def validate_stored_call(root, base, number, call):
    """Re-read Host call/result artifacts rather than trusting a summary alone."""
    from .safe_io import read_bytes
    from .authoring_v2 import strict_json
    stored = strict_json(read_bytes(root, base + f'/call-{number:04d}.json'))
    if encoded(stored) != encoded(call):
        raise ValueError('INVOCATION_CALL_CHANGED')
    receipt = call.get('reviewer_provenance')
    if receipt is None:
        # Launch failures may have no completed process observation.
        if call.get('returncode') is not None:
            raise ValueError('INVOCATION_RECEIPT_MISSING')
        return None
    body = validate_observation(receipt)
    attempt = strict_json(read_bytes(root, base + f'/attempt-{number:04d}.json'))
    expected_input = sha(encoded({'files': attempt['visible_files'], 'prompt_sha256': attempt['prompt_sha256']}))
    if (body['input_snapshot_sha256'] != expected_input
            or body['model_requested'] != attempt['model_requested']):
        raise ValueError('INVOCATION_INPUT_CHANGED')
    if body['process_returncode'] != call['returncode']:
        raise ValueError('INVOCATION_CALL_CHANGED')
    if call.get('raw_result_path'):
        expected_path = base + f'/raw-result-{number:04d}.json'
        if call['raw_result_path'] != expected_path or sha(read_bytes(root, expected_path)) != body['result_sha256']:
            raise ValueError('INVOCATION_RESULT_CHANGED')
    return body
