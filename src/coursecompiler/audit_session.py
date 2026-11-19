"""Immutable audit attempts and frozen policy; report.json is only a latest index."""
from .authoring_stages import encoded, sha
from .authoring_v2 import strict_json
from .invocation_receipts import validate_observation
from .safe_io import publish_bytes, read_bytes, safe_path


class BudgetExhausted(ValueError):
    pass


class AuditSession:
    def __init__(self, root, base, plan, *, continuation=False):
        self.root, self.base = root, base
        directory = safe_path(root, base, missing=True)
        frozen = dict(schema_version=1, revision=1, **plan)
        frozen['audit_plan_hash'] = sha(encoded(frozen))
        if continuation:
            previous = self.read('audit-plan.json')
            if encoded(previous) != encoded(frozen):
                raise ValueError('AUDIT_CONTINUATION_BINDING_CHANGED')
        else:
            directory.mkdir(parents=True, exist_ok=False)
            self.store('audit-plan.json', frozen)
        self.plan = frozen
        self.ledger = []
        sealed = set()
        previous = frozen['audit_plan_hash']
        for path in sorted(directory.glob('ledger-*.json')):
            if path.name != f'ledger-{len(self.ledger) + 1:04d}.json':
                raise ValueError('AUDIT_LEDGER_SEQUENCE_CHANGED')
            entry = self.read(path.name)
            if entry['previous_sha256'] != previous:
                raise ValueError('AUDIT_LEDGER_CHAIN_CHANGED')
            for name, digest in entry['artifacts'].items():
                if sha(read_bytes(root, base + '/' + name)) != digest:
                    raise ValueError('AUDIT_LEDGER_ARTIFACT_CHANGED')
                sealed.add(name)
            previous = sha(encoded(entry))
            self.ledger.append(entry)
        self.sealed = sealed
        self.calls, self.attempts = [], []
        self.run_calls = 0
        self.reservations = []
        for path in sorted(directory.glob('attempt-*.json')):
            number = len(self.attempts) + 1
            if path.name != f'attempt-{number:04d}.json':
                raise ValueError('AUDIT_ATTEMPT_SEQUENCE_INVALID')
            attempt = self.read(path.name)
            if path.name not in sealed:
                raise ValueError('AUDIT_ATTEMPT_UNSEALED')
            self.attempts.append(attempt)
            call_path = directory / f'call-{number:04d}.json'
            if call_path.exists():
                if call_path.name not in sealed:
                    raise ValueError('AUDIT_CALL_UNSEALED')
                call = self.read(call_path.name)
                self._check_call(number, attempt, call)
            else:
                call = dict(stage=attempt['stage'], job_id=attempt['job_id'], returncode=None,
                            errors=['AUDIT_INTERRUPTED_ATTEMPT'])
            self.calls.append(call)
        for path in sorted(directory.glob('repair-reservation-*.json')):
            if path.name not in sealed:
                raise ValueError('AUDIT_REPAIR_RESERVATION_UNSEALED')
            self.reservations.append(self.read(path.name))
        self.report_revision = len(list(directory.glob('report-[0-9]*.json'))) + 1

    def seal(self, names):
        previous = sha(encoded(self.ledger[-1])) if self.ledger else self.plan['audit_plan_hash']
        entry = dict(previous_sha256=previous, artifacts={name: sha(read_bytes(self.root, self.base + '/' + name)) for name in names})
        self.store(f'ledger-{len(self.ledger) + 1:04d}.json', entry)
        self.ledger.append(entry)

    def read(self, name):
        return strict_json(read_bytes(self.root, self.base + '/' + name))

    def store(self, name, value):
        data = encoded(value)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError('AUDIT_ARTIFACT_TOO_LARGE')
        path = safe_path(self.root, self.base + '/' + name, missing=True)
        if path.exists():
            if read_bytes(self.root, self.base + '/' + name) != data:
                raise ValueError('AUDIT_ARTIFACT_CONFLICT')
            return
        publish_bytes(self.root, self.base + '/' + name, data)

    def _check_call(self, number, attempt, call):
        from .invocation_receipts import validate_stored_call
        validate_stored_call(self.root, self.base, number, call)
        if call.get('job_id') != attempt['job_id'] or call.get('stage') != attempt['stage']:
            raise ValueError('AUDIT_CALL_BINDING_CHANGED')
        receipt = call.get('reviewer_provenance')
        raw_path = call.get('raw_result_path')
        raw = None
        if raw_path:
            if raw_path != self.base + f'/raw-result-{number:04d}.json':
                raise ValueError('AUDIT_RESULT_PATH_CHANGED')
            raw = read_bytes(self.root, raw_path)
        expected_input = sha(encoded({'files': attempt['visible_files'],
                                      'prompt_sha256': attempt['prompt_sha256']}))
        if receipt:
            observed = validate_observation(receipt, input_sha256=expected_input,
                result_sha256=sha(raw) if raw is not None else None)
            dispositions = {'discarded_credentials': 'AUDIT_RESULT_CREDENTIALS',
                            'discarded_invalid_encoding': 'AUDIT_RESULT_ENCODING'}
            discarded = dispositions.get(call.get('result_disposition')) in call.get('errors', [])
            if observed['process_returncode'] != call['returncode'] or (
                    observed.get('result_sha256') is not None and raw is None and not discarded):
                raise ValueError('AUDIT_CALL_RESULT_CHANGED')
        elif call.get('returncode') is not None:
            raise ValueError('AUDIT_CALL_PROVENANCE_MISSING')
        chunk_path = safe_path(self.root, self.base + f'/chunk-{number:04d}.json', missing=True)
        if chunk_path.exists() and self.read(chunk_path.name).get('call_sha256') != sha(encoded(call)):
            raise ValueError('AUDIT_CHUNK_CALL_CHANGED')

    def reserve(self, attempt):
        policy = self.plan['budget']
        total_bytes = sum(a['input_bytes'] for a in self.attempts)
        if (self.run_calls >= policy['max_calls_per_run'] or len(self.attempts) >= policy['max_calls_total']
                or total_bytes + attempt['input_bytes'] > policy['max_total_bytes']
                or attempt['input_bytes'] > policy['max_input_bytes']):
            raise BudgetExhausted('AUDIT_BUDGET_EXHAUSTED')
        number = len(self.attempts) + 1
        self.store(f'attempt-{number:04d}.json', attempt)
        self.seal([f'attempt-{number:04d}.json'])
        self.attempts.append(attempt)
        self.run_calls += 1
        return number

    def save_report(self, report):
        report['audit_plan_hash'] = self.plan['audit_plan_hash']
        report['budget_used'] = {'calls': len(self.attempts),
            'input_bytes': sum(a['input_bytes'] for a in self.attempts), 'calls_this_run': self.run_calls}
        report['report_path'] = self.base + '/report.json'
        report['ledger_tip_sha256'] = sha(encoded(self.ledger[-1])) if self.ledger else self.plan['audit_plan_hash']
        self.store(f'report-{self.report_revision:04d}.json', report)
        self.seal([f'report-{self.report_revision:04d}.json'])
        publish_bytes(self.root, report['report_path'], encoded(report), replace=True)

    def reserve_repair(self, issue_id):
        if any(item['issue_id'] == issue_id for item in self.reservations):
            return False
        if len(self.reservations) >= self.plan['repair_policy']['max_repairs']:
            return False
        item = dict(issue_id=issue_id, output_sha256=self.plan['binding']['output_sha256'])
        name = f'repair-reservation-{len(self.reservations) + 1:04d}.json'
        self.store(name, item)
        self.seal([name])
        self.reservations.append(item)
        return True


def verify_report(root, base):
    """Read-only replay of sealed Host evidence; the mutable index is not trust."""
    frozen = strict_json(read_bytes(root, base + '/audit-plan.json'))
    body = {key: value for key, value in frozen.items() if key != 'audit_plan_hash'}
    if frozen.get('audit_plan_hash') != sha(encoded(body)):
        raise ValueError('AUDIT_PLAN_CHANGED')
    session = AuditSession(root, base, {key: value for key, value in body.items()
        if key not in {'schema_version', 'revision'}}, continuation=True)
    number = session.report_revision - 1
    if number < 1:
        raise ValueError('AUDIT_REPORT_MISSING')
    latest = read_bytes(root, base + f'/report-{number:04d}.json')
    if f'report-{number:04d}.json' not in session.sealed:
        raise ValueError('AUDIT_REPORT_UNSEALED')
    if read_bytes(root, base + '/report.json') != latest:
        raise ValueError('AUDIT_REPORT_INDEX_CHANGED')
    report = strict_json(latest)
    if encoded(report['binding']) != encoded(frozen['binding']):
        raise ValueError('AUDIT_REPORT_BINDING_CHANGED')
    from .agents.base import read_handoff
    from .authoring_stages import latest_plan
    from .authoring_audit import validate_scan, validate_audit, binding, source_scope
    _, sources, _ = read_handoff(root)
    plan = latest_plan(root, sources)
    mode = frozen['binding']['output_mode']
    data = read_bytes(root, sources['expected_outputs'][mode], role='note')
    if encoded(binding(root, sources, plan, data, mode)) != encoded(frozen['binding']):
        raise ValueError('AUDIT_REPORT_INPUT_CHANGED')
    completed = set()
    validated_audits = {}
    validated_scans = {}
    for chunk in report['chunks']:
        if chunk['status'] not in {'completed', 'partial'} or 'call' not in chunk:
            continue
        index = chunk['call'] - 1
        call, attempt = session.calls[index], session.attempts[index]
        req = attempt['request']
        if call['job_id'] != chunk['job_id'] or call.get('returncode') != 0 or call.get('errors'):
            raise ValueError('AUDIT_CHUNK_CALL_CHANGED')
        raw = strict_json(read_bytes(root, call['raw_result_path']))
        if chunk['stage'] == 'source_scan':
            value = validate_scan(raw, frozen['binding'], root, sources, req['source_scope'])
            validated_scans[int(chunk['job_id'].split(':')[1])] = value
        else:
            value = validate_audit(raw, frozen['binding'], root, sources, data, plan,
                scan=req.get('scan'), expected_source_scope=req['source_scope'], expected_output_scope=[req['output_ref']])
            if value['direction'] != chunk['stage']:
                raise ValueError('AUDIT_DIRECTION_CHANGED')
            from .audit_adjudication import normalize_local_issue_ids
            validated_audits[chunk['job_id']] = normalize_local_issue_ids(value)[0]
        if value['status'] != chunk['status'] or value['reviewer']['kind'] != 'model':
            raise ValueError('AUDIT_CHUNK_FALSE_COMPLETION')
        if value['status'] == 'completed':
            completed.add(chunk['job_id'])
    from .audit_chunks import merge_scans
    expected_scans = []
    accepted_scans = []
    for source_index, part in enumerate(frozen['source_chunks']):
        history = []
        for call_index, (attempt, call) in enumerate(zip(session.attempts, session.calls), 1):
            if (call.get('job_id') != f'scan:{source_index}' or call.get('returncode') != 0
                    or call.get('errors') or not call.get('raw_result_path')):
                continue
            value = validate_scan(strict_json(read_bytes(root, call['raw_result_path'])),
                                  frozen['binding'], root, sources, part['source_refs'])
            history.append(value)
        if not history:
            accepted_scans.append(None)
            continue
        value = history[-1]
        if len(history) >= 2:
            merged = merge_scans(history, frozen['binding'], part['source_refs'], root, sources)
            merged.update(status=value['status'], reviewer=value['reviewer'])
            value = merged
        accepted_scans.append(value)
        expected_scans.append(value)
    if expected_scans:
        expected_scan = (expected_scans[0] if len(frozen['source_chunks']) == 1 else
            merge_scans(accepted_scans, frozen['binding'], source_scope(root, sources), root, sources))
        if encoded(report.get('scans')) != encoded(expected_scans) or encoded(report.get('scan')) != encoded(expected_scan):
            raise ValueError('AUDIT_SCAN_AGGREGATE_CHANGED')
    from .audit_stages import _candidate_batches
    expected_chunk_ids = {f'scan:{index}' for index in range(len(frozen['source_chunks']))}
    expected_audit_ids = set()
    expected_candidate_jobs = {}
    chunk_bytes = frozen['budget']['chunk_bytes']
    for source_index, scan in enumerate(accepted_scans):
        if scan is None or scan['status'] != 'completed':
            continue
        for direction in ('source_to_output', 'output_to_source'):
            batches = _candidate_batches(scan, chunk_bytes) if direction == 'source_to_output' else [None]
            for output_index, _ in enumerate(frozen['output_chunks']):
                for batch_index, _ in enumerate(batches):
                    identity = f'{direction}:{source_index}:{output_index}:{batch_index}'
                    expected_audit_ids.add(identity)
                    if direction == 'source_to_output':
                        expected_candidate_jobs[identity] = batches[batch_index]['candidates']
    expected_chunk_ids.update(expected_audit_ids)
    actual_chunk_ids = [chunk.get('job_id') for chunk in report['chunks']]
    if len(actual_chunk_ids) != len(set(actual_chunk_ids)) or set(actual_chunk_ids) != expected_chunk_ids:
        raise ValueError('AUDIT_EXPECTED_JOB_SET_CHANGED')
    jobs = report.get('audit_jobs')
    if not isinstance(jobs, list) or len(jobs) != len(report.get('audits', [])):
        raise ValueError('AUDIT_ADJUDICATION_JOBS_CHANGED')
    from .audit_stages import _candidate_key
    global_candidates = {_candidate_key(item): item for item in report['scan']['candidates']} if report.get('scan') else {}
    for index, job in enumerate(jobs):
        if job.get('audit_index') != index or job.get('job_id') not in validated_audits:
            raise ValueError('AUDIT_ADJUDICATION_JOBS_CHANGED')
        attempt = session.attempts[report['chunks'][next(i for i, chunk in enumerate(report['chunks'])
            if chunk['job_id'] == job['job_id'] and 'call' in chunk)]['call'] - 1]
        req = attempt['request']
        batch = req.get('scan', {}).get('candidates', [])
        expected_ids = {item['id']: _candidate_key(item) for item in batch}
        expected_candidates = {key: global_candidates[key] for key in expected_ids.values()}
        expected_meta = dict(job_id=job['job_id'], direction=req['stage'],
            source_chunk_id=req['source_chunk_id'], source_chunk_index=req['source_chunk_index'],
            output_chunk_index=req['output_chunk_index'], audit_index=index,
            candidate_ids=expected_ids, candidates=expected_candidates)
        if encoded(job) != encoded(expected_meta) or encoded(report['audits'][index]) != encoded(validated_audits[job['job_id']]):
            raise ValueError('AUDIT_ADJUDICATION_JOBS_CHANGED')
    from .audit_adjudication import adjudicate
    derived = adjudicate(report, frozen['source_chunks'], frozen['output_chunks'], frozen['binding'])
    for key, value in derived.items():
        if encoded(report.get(key)) != encoded(value):
            raise ValueError('AUDIT_GLOBAL_ADJUDICATION_CHANGED')
    candidate_records = {_candidate_key(candidate): dict(candidate, required_jobs=[], assessed_jobs=[])
                         for candidate in (report.get('scan') or {}).get('candidates', [])}
    for job_id, candidates in expected_candidate_jobs.items():
        audit = validated_audits.get(job_id)
        assessed = ({item['id'] for item in audit['candidate_assessments'] if item['status'] == 'assessed'}
                    if audit is not None and audit['status'] == 'completed' else set())
        for candidate in candidates:
            key = _candidate_key(candidate)
            candidate_records[key]['required_jobs'].append(job_id)
            if candidate['id'] in assessed:
                candidate_records[key]['assessed_jobs'].append(job_id)
    global_candidates = {item['identity']: item for item in derived['global_adjudications']
                         if item['direction'] == 'source_to_output'}
    expected_candidates = [dict(id=item['id'], required_jobs=item['required_jobs'],
        assessed_jobs=item['assessed_jobs'], status=('not_assessed' if
            global_candidates.get(item['id'], {}).get('global_status') == 'not_assessed' else 'assessed'),
        global_status=global_candidates.get(item['id'], {}).get('global_status', 'not_assessed'))
        for item in candidate_records.values()]
    if encoded(report.get('candidate_assessments')) != encoded(expected_candidates):
        raise ValueError('AUDIT_CANDIDATE_ASSESSMENTS_CHANGED')
    coverage_report = dict(audits=report['audits'], chunks=report['chunks'],
                           pending_jobs=report['pending_jobs'], coverage=[])
    from .audit_stages import _aggregate_coverage
    _aggregate_coverage(coverage_report, frozen['binding'], source_scope(root, sources),
                        root, sources, data, plan)
    if encoded(report.get('coverage')) != encoded(coverage_report['coverage']):
        raise ValueError('AUDIT_COVERAGE_CHANGED')
    if report['status'] == 'completed':
        if report['pending_jobs'] or any(chunk['status'] != 'completed' for chunk in report['chunks']):
            raise ValueError('AUDIT_REPORT_FALSE_COMPLETION')
        for si, source in enumerate(frozen['source_chunks']):
            if f'scan:{si}' not in completed:
                raise ValueError('AUDIT_REPORT_FALSE_COMPLETION')
            for oi, _ in enumerate(frozen['output_chunks']):
                for direction in ('source_to_output', 'output_to_source'):
                    required = {key for key in expected_audit_ids if key.startswith(f'{direction}:{si}:{oi}:')}
                    if not required or not required.issubset(completed):
                        raise ValueError('AUDIT_REPORT_FALSE_COMPLETION')
    return report
