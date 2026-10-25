"""Offline authoring evidence. No model invocation or automatic semantic judge."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .safe_io import publish_bytes, read_bytes

MODES = {'verbatim', 'full', 'deep', 'summary', 'complete'}
KINDS = {'manual_sample', 'synthetic_contract', 'fake_agent_integration', 'live_model_import', 'replay'}
STATES = {'retained', 'omitted', 'distorted', 'uncertainty_preserved', 'not_assessed', 'not_required'}
METRICS = {'elapsed_ms_measured', 'input_tokens_measured', 'output_tokens_measured',
           'invocations_measured', 'cost_measured'}
CONFIGURATION = {'actual_code_commit', 'actual_dirty_diff_sha256', 'rules_version',
                 'final_prompt', 'actual_modules', 'cli_requested', 'cli_observed_version',
                 'model_requested', 'model_resolved', 'permissions_description',
                 'format_profile', 'use_slides_as_source', 'embed_slides', 'stage',
                 'returncode', 'cost_currency', 'interpreter_version', 'estimates',
                 'started_at', 'completed_at', 'authorization_reference'}
LEGACY_RUN_FIELDS = {'schema_version', 'record_kind', 'run_id', 'run_kind', 'recorded_at', 'status',
              'case_id', 'mode', 'source_snapshot', 'source_sha256', 'rules_snapshot', 'rules_sha256',
              'configuration', 'cache_key_parts', 'cache_key', 'source_run_id', 'independent_model_sample',
              'provenance_note', 'measured', 'artifact', 'semantic_review', 'review_output_sha256',
              'evaluator_gold_snapshot', 'release_eligible', 'default_change_authorized'}
RUN_FIELDS = (LEGACY_RUN_FIELDS - {'status'}) | {'execution_status', 'artifact_status'}


def execution_state(returncode, requested=None) -> str:
    """Only an explicit integer exit code establishes execution success/failure."""
    if returncode is not None and type(returncode) is not int:
        raise ValueError('INVALID_RETURNCODE')
    observed = ('succeeded' if returncode == 0 else 'failed') if returncode is not None else None
    if requested is not None and (not isinstance(requested, str) or
                                  requested not in {'not_run', 'succeeded', 'failed', 'unknown'}):
        raise ValueError('EXECUTION_STATUS')
    if observed:
        if requested is not None and requested != observed:
            raise ValueError('EXECUTION_STATUS')
        return observed
    if requested not in {None, 'unknown', 'not_run'}:
        raise ValueError('EXECUTION_EVIDENCE_REQUIRED')
    return requested or 'unknown'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def load_json(path: Path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('NONFINITE_JSON')
    data = read_bytes(path.parent, path.name)
    if len(data) > 512 * 1024:
        raise ValueError('RECORD_TOO_LARGE')
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)


def writer_input(case: dict) -> dict:
    """Explicit allowlist boundary: never return evaluator-only data to a writer."""
    source = case['input_to_writer']
    allowed = {'source_id', 'transcript_origin', 'origin_note', 'course_title', 'course_date',
               'encoding', 'line_separator', 'text', 'source_bytes_sha256', 'line_index',
               'segments', 'slides', 'time_metadata'}
    if not isinstance(source, dict) or set(source) - allowed:
        raise ValueError('WRITER_SOURCE_FIELDS')
    if not isinstance(source.get('text'), str):
        raise ValueError('WRITER_SOURCE_TEXT')
    if digest(source['text'].encode('utf-8')) != source.get('source_bytes_sha256'):
        raise ValueError('SOURCE_CHANGED')
    return deepcopy(source)


def snapshot(root: Path, paths: list[str]) -> list[dict]:
    """Store exact bytes, not only mutable paths. Inputs here must be public rules/synthetic."""
    if len(paths) != len(set(paths)):
        raise ValueError('DUPLICATE_SNAPSHOT_PATH')
    return [{'path': path, 'sha256': digest(data), 'text': data.decode('utf-8')}
            for path in sorted(paths) for data in [read_bytes(root, path)]]


def validate_snapshot(entries: list[dict]) -> None:
    paths = set()
    for entry in entries:
        if set(entry) != {'path', 'sha256', 'text'} or entry['path'] in paths:
            raise ValueError('SNAPSHOT_FIELDS')
        paths.add(entry['path'])
        if digest(entry['text'].encode('utf-8')) != entry['sha256']:
            raise ValueError('SNAPSHOT_CHANGED')


def review_table(case: dict, mode: str, reviews: list[dict] | None = None,
                 reviewer: dict | None = None) -> dict:
    if mode not in MODES:
        raise ValueError('UNKNOWN_MODE')
    if reviewer is not None and (set(reviewer) != {'kind', 'id'} or
                                reviewer['kind'] not in {'human', 'model', 'synthetic_fixture'} or
                                not isinstance(reviewer['id'], str) or not reviewer['id'].strip()):
        raise ValueError('REVIEWER_REQUIRED')
    gold = case['evaluator_only']['gold_items']
    ids = [item['id'] for item in gold]
    if len(ids) != len(set(ids)):
        raise ValueError('DUPLICATE_GOLD_ID')
    supplied = {}
    for row in reviews or []:
        if set(row) != {'id', 'status', 'source_location', 'output_location', 'reason'}:
            raise ValueError('REVIEW_FIELDS')
        if row['id'] not in ids or row['id'] in supplied or row['status'] not in STATES:
            raise ValueError('REVIEW_ITEM')
        if row['status'] != 'not_assessed':
            if not reviewer or not reviewer.get('kind') or not reviewer.get('id'):
                raise ValueError('REVIEWER_REQUIRED')
            if not all(isinstance(row[key], str) and row[key].strip()
                       for key in ('source_location', 'output_location', 'reason')):
                raise ValueError('REVIEW_EVIDENCE_REQUIRED')
        supplied[row['id']] = row
    rows = []
    for item in gold:
        required = mode in item['required_modes']
        # Conditional/optional items still need explicit review, never implicit success.
        row = supplied.get(item['id'], {'id': item['id'], 'status': 'not_assessed',
                                       'source_location': None, 'output_location': None, 'reason': None})
        if required and row['status'] == 'not_required':
            raise ValueError('REQUIRED_ITEM_WAIVED')
        rows.append({**row, 'required': required})
    counts = {state: sum(row['status'] == state for row in rows) for state in sorted(STATES)}
    required_rows = [row for row in rows if row['required']]
    assessed = sum(row['status'] != 'not_assessed' for row in required_rows)
    retained = sum(row['status'] in {'retained', 'uncertainty_preserved'} for row in required_rows)
    total = len(required_rows)
    return {'status': 'not_assessed' if all(row['status'] == 'not_assessed' for row in rows)
            else ('partial' if counts['not_assessed'] else 'reviewed'),
            'reviewer': reviewer, 'items': rows, 'counts': counts, 'required_items': total,
            'assessed_required_items': assessed, 'not_assessed_required_items': total - assessed,
            'review_coverage': assessed / total if total else None,
            'assessed_retention': retained / assessed if assessed else None,
            'retention_lower_bound': retained / total if total else None,
            'critical_errors_confirmed': None}


def build_run(*, run_id: str, case: dict, mode: str, run_kind: str,
              output: Path | None, rules: list[dict], configuration: dict | None = None,
              reviews: list[dict] | None = None, reviewer: dict | None = None,
              source_run_id: str | None = None, measured: dict | None = None,
              execution_status: str | None = None) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', run_id):
        raise ValueError('INVALID_RUN_ID')
    if run_kind not in KINDS or (run_kind == 'replay') != bool(source_run_id):
        raise ValueError('RUN_IDENTITY')
    if mode not in MODES:
        raise ValueError('UNKNOWN_MODE')
    validate_snapshot(rules)
    source = writer_input(case)
    measured = measured or {}
    if set(measured) - METRICS:
        raise ValueError('UNKNOWN_METRIC')
    for value in measured.values():
        if value is not None and (type(value) not in (int, float) or value < 0):
            raise ValueError('INVALID_METRIC')
    # JSON encoding also rejects NaN/Infinity, even when supplied through Python.
    encoded(measured)
    status, artifact = 'unknown', None
    if output is not None:
        try:
            data = read_bytes(output.parent, output.name)
        except FileNotFoundError:
            status = 'missing'
        else:
            status = 'empty' if not data.strip() else 'available'
            artifact = {'sha256': digest(data), 'bytes': len(data), 'text': data.decode('utf-8')}
    if status != 'available' and reviews:
        raise ValueError('OUTPUT_REQUIRED_FOR_REVIEW')
    if set(configuration or {}) - CONFIGURATION:
        raise ValueError('UNKNOWN_CONFIGURATION')
    config = {key: deepcopy((configuration or {}).get(key)) for key in sorted(CONFIGURATION)}
    execution = execution_state(config['returncode'], execution_status)
    key_parts = {'source_sha256': digest(encoded(source)), 'rules_sha256': digest(encoded(rules)),
                 'configuration': config, 'mode': mode}
    return {'schema_version': 2, 'record_kind': 'offline_authoring_run', 'run_id': run_id,
            'run_kind': run_kind, 'recorded_at': datetime.now(timezone.utc).isoformat(),
            'execution_status': execution, 'artifact_status': status,
            'case_id': case['case_id'], 'mode': mode,
            'source_snapshot': source, 'source_sha256': key_parts['source_sha256'],
            'rules_snapshot': rules, 'rules_sha256': key_parts['rules_sha256'],
            'configuration': config, 'cache_key_parts': key_parts, 'cache_key': digest(encoded(key_parts)),
            'source_run_id': source_run_id, 'independent_model_sample': False,
            'provenance_note': 'Offline import; run identity is supplied, not an attestation of invocation.',
            'measured': {name: measured.get(name) for name in sorted(METRICS)},
            'artifact': artifact, 'semantic_review': review_table(case, mode, reviews, reviewer),
            'review_output_sha256': artifact['sha256'] if artifact else None,
            'evaluator_gold_snapshot': deepcopy(case['evaluator_only']['gold_items']),
            'release_eligible': None, 'default_change_authorized': False}


def validate_run(run: dict) -> None:
    if not isinstance(run, dict) or run.get('record_kind') != 'offline_authoring_run':
        raise ValueError('NOT_A_RUN')
    version = run.get('schema_version')
    if type(version) is not int or version not in {1, 2}:
        raise ValueError('NOT_A_RUN')
    if set(run) != (LEGACY_RUN_FIELDS if version == 1 else RUN_FIELDS):
        raise ValueError('RUN_FIELDS')
    if len(encoded(run)) > 512 * 1024:
        raise ValueError('RECORD_TOO_LARGE')
    if (not isinstance(run['mode'], str) or run['mode'] not in MODES
            or not isinstance(run['run_kind'], str)
            or not isinstance(run['case_id'], str) or not run['case_id']
            or not isinstance(run['recorded_at'], str)
            or (run['source_run_id'] is not None and (not isinstance(run['source_run_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', run['source_run_id'])))):
        raise ValueError('RUN_IDENTITY')
    if (not isinstance(run.get('run_id'), str) or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', run['run_id'])):
        raise ValueError('INVALID_RUN_ID')
    if (run['run_kind'] not in KINDS or
            (run['run_kind'] == 'replay') != bool(run['source_run_id']) or
            run['independent_model_sample'] is not False):
        raise ValueError('RUN_IDENTITY')
    if set(run['measured']) != METRICS:
        raise ValueError('UNKNOWN_METRIC')
    if set(run['configuration']) != CONFIGURATION:
        raise ValueError('UNKNOWN_CONFIGURATION')
    if version == 2:
        execution_state(run['configuration']['returncode'], run['execution_status'])
        if run['execution_status'] is None:
            raise ValueError('EXECUTION_STATUS')
    for value in run['measured'].values():
        if value is not None and (type(value) not in (int, float) or value < 0):
            raise ValueError('INVALID_METRIC')
    encoded(run['measured'])
    writer_input({'input_to_writer': run['source_snapshot']})
    validate_snapshot(run['rules_snapshot'])
    for name, value in [('source', run['source_snapshot']), ('rules', run['rules_snapshot'])]:
        if digest(encoded(value)) != run[name + '_sha256']:
            raise ValueError('SNAPSHOT_CHANGED')
    parts = {'source_sha256': run['source_sha256'], 'rules_sha256': run['rules_sha256'],
             'configuration': run['configuration'], 'mode': run['mode']}
    if parts != run['cache_key_parts'] or digest(encoded(parts)) != run['cache_key']:
        raise ValueError('CONFIGURATION_CHANGED')
    if run['artifact'] and digest(run['artifact']['text'].encode('utf-8')) != run['artifact']['sha256']:
        raise ValueError('OUTPUT_CHANGED')
    artifact = run['artifact']
    status = run['status'] if version == 1 else run['artifact_status']
    if artifact:
        expected_status = 'available' if artifact['text'].strip() else ('empty_output' if version == 1 else 'empty')
        if status != expected_status or artifact['bytes'] != len(artifact['text'].encode('utf-8')):
            raise ValueError('OUTPUT_STATUS')
    elif status not in ({'missing_output', 'not_run'} if version == 1 else {'missing', 'unknown'}):
        raise ValueError('OUTPUT_STATUS')
    if run['review_output_sha256'] != (artifact['sha256'] if artifact else None):
        raise ValueError('REVIEW_OUTPUT_CHANGED')
    review = run['semantic_review']
    rows = [{key: value for key, value in row.items() if key != 'required'} for row in review['items']]
    case = {'evaluator_only': {'gold_items': run['evaluator_gold_snapshot']}}
    if review_table(case, run['mode'], rows, review['reviewer']) != review:
        raise ValueError('REVIEW_CHANGED')
    if status != 'available' and review['status'] != 'not_assessed':
        raise ValueError('OUTPUT_REQUIRED_FOR_REVIEW')
    if run['release_eligible'] is not None or run['default_change_authorized'] is not False:
        raise ValueError('OFFLINE_RELEASE_CLAIM')


def save_run(root: Path, run: dict) -> Path:
    validate_run(run)
    return publish_bytes(root, run['run_id'] + '.json', encoded(run))


def compare_runs(runs: list[dict]) -> dict:
    if not runs or len({run['run_id'] for run in runs}) != len(runs):
        raise ValueError('EMPTY_OR_DUPLICATE_RUNS')
    for run in runs:
        validate_run(run)
    first = runs[0]
    fields = ('case_id', 'source_sha256', 'rules_sha256', 'mode', 'configuration', 'evaluator_gold_snapshot')
    differences = {run['run_id']: [key for key in fields if run[key] != first[key]] for run in runs}
    states = []
    for run in runs:
        legacy = run['schema_version'] == 1
        # A legacy artifact status was never an execution attestation. Preserve the
        # original record and surface that limitation instead of migrating it.
        execution = 'unknown' if legacy else run['execution_status']
        artifact = ({'not_run': 'unknown', 'missing_output': 'missing', 'empty_output': 'empty',
                     'available': 'available'}[run['status']] if legacy else run['artifact_status'])
        states.append({'run_id': run['run_id'], 'run_kind': run['run_kind'],
                       'source_schema_version': run['schema_version'],
                       'legacy_status': run['status'] if legacy else None,
                       'execution_status': execution, 'artifact_status': artifact,
                       'successful_execution': execution == 'succeeded',
                       'source_run_id': run['source_run_id'], 'semantic_review': run['semantic_review'],
                       'measured': run['measured']})
    return {'schema_version': 2, 'record_kind': 'offline_comparison', 'reference_run_id': first['run_id'],
            'execution_differences': {row['run_id']: row['execution_status'] != states[0]['execution_status']
                                      for row in states},
            'configuration_differences': differences,
            'identical_inputs': not any(differences.values()),
            'quality_improvement': None, 'default_change_authorized': False,
            'runs': states}


def save_report(path: Path, runs: list[dict]) -> Path:
    return publish_bytes(path.parent, path.name, encoded(compare_runs(runs)))
