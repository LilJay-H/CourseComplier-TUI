"""Deterministic Host aggregation of validated local semantic judgments."""
from copy import deepcopy

from .authoring_stages import encoded, sha


GLOBAL_NEGATIVE = {'omission', 'unsupported_attribution'}


def _key(value):
    return encoded(value).decode('utf-8')


def _unique(values):
    result = {}
    for value in values:
        result[_key(value)] = deepcopy(value)
    return [result[key] for key in sorted(result)]


def normalize_local_issue_ids(audit):
    """Replace Agent-local IDs deterministically and keep local links intact."""
    value = deepcopy(audit)
    mappings = []
    id_map = {}
    for issue in value['issues']:
        body = {key: part for key, part in issue.items()
                if key not in {'id', 'reviewer', 'status', 'detection_method'}}
        host_id = 'issue-' + sha(encoded(body))[:24]
        id_map[issue['id']] = host_id
        mappings.append(dict(agent_issue_id=issue['id'], host_issue_id=host_id,
            direction=value['direction'], source_refs=deepcopy(issue['source_refs']),
            output_refs=deepcopy(issue['output_refs'])))
        issue['id'] = host_id
    for assessment in value['local_assessments']:
        if assessment['issue_id'] is not None:
            assessment['issue_id'] = id_map[assessment['issue_id']]
    return value, mappings


def _global_issue(direction, identity, status, claims, *, source_refs, output_refs, binding):
    """Use reviewer-provided issue attributes; Host supplies only the logical rule."""
    linked = [claim['issue'] for claim in claims if claim.get('issue')]
    if not linked:
        return None, 'missing_local_issue_claim'
    semantic = {(item['category'], item['severity'], tuple(item['affected_modes']),
                 tuple(item['affected_layers']), item['status']) for item in linked}
    expected_category = 'omission' if direction == 'source_to_output' else 'unsupported_attribution'
    if len(semantic) != 1 or next(iter(semantic))[0] != expected_category:
        return None, 'conflicting_local_issue_claims'
    category, severity, modes, layers, issue_status = next(iter(semantic))
    if issue_status != 'open':
        return None, 'local_issue_not_open'
    body = dict(direction=direction, identity=identity, category=category,
                source_refs=source_refs, output_refs=output_refs,
                affected_modes=list(modes), affected_layers=list(layers))
    issue = dict(id='issue-' + sha(encoded(body))[:24], category=category, severity=severity,
        source_refs=deepcopy(source_refs), output_refs=deepcopy(output_refs),
        affected_modes=list(modes), affected_layers=list(layers),
        detection_method='host_global_adjudication', status='open',
        reason=('All required output chunks were locally assessed not_found.' if status == 'omitted'
                else 'All required source chunks were locally assessed no_support_found.'),
        reviewer={'kind': 'model', 'id': 'aggregate:see-reviewer-claims'})
    return issue, None


def adjudicate(report, source_chunks, output_chunks, binding):
    """Return global results without making new semantic judgments.

    Local ``present``/``supported``/negative/uncertain labels come from reviewers.
    The Host only checks required-job completion and applies existential/all-negative
    logic. Missing local judgments remain not_assessed.
    """
    issues_by_job = {}
    local_issues = []
    local_assessments = []
    direct = {}
    candidate_required = {}
    candidate_info = {}
    claim_groups = {}

    for job in report.get('audit_jobs', []):
        audit = report['audits'][job['audit_index']]
        issue_map = {item['id']: item for item in audit['issues']}
        issues_by_job[job['job_id']] = issue_map
        for item in audit['issues']:
            record = dict(job_id=job['job_id'], direction=job['direction'],
                          source_chunk_id=job['source_chunk_id'],
                          output_chunk_index=job['output_chunk_index'], issue=deepcopy(item))
            local_issues.append(record)
            if item['category'] not in GLOBAL_NEGATIVE:
                direct.setdefault(item['id'], []).append(record)
        for key, candidate in job.get('candidates', {}).items():
            candidate_required.setdefault(key, set()).add(job['job_id'])
            candidate_info[key] = candidate
        for item in audit['local_assessments']:
            record = dict(job_id=job['job_id'], direction=job['direction'],
                source_chunk_id=job['source_chunk_id'], output_chunk_index=job['output_chunk_index'],
                reviewer=deepcopy(audit['reviewer']), assessment=item['assessment'],
                reason=item['reason'], issue_id=item['issue_id'],
                issue=deepcopy(issue_map.get(item['issue_id'])) if item['issue_id'] else None)
            if item['kind'] == 'candidate':
                key = job['candidate_ids'][item['candidate_id']]
                record.update(kind='candidate', candidate_key=key,
                              candidate_id=candidate_info[key]['id'])
            else:
                claim_key = _key(item['claim_ref'])
                record.update(kind='claim', claim_key=claim_key,
                              claim_ref=deepcopy(item['claim_ref']))
                claim_groups.setdefault(claim_key, []).append(record)
            local_assessments.append(record)

    completed = {chunk['job_id'] for chunk in report.get('chunks', [])
                 if chunk['status'] == 'completed'}
    local_assessments.sort(key=lambda item: (item['job_id'], item['kind'],
        item.get('candidate_id', item.get('claim_key', ''))))
    global_results = []
    final_issues = []
    provenance = []

    for key in sorted(candidate_required):
        required = sorted(candidate_required[key])
        claims = [item for item in local_assessments
                  if item['kind'] == 'candidate' and item['candidate_key'] == key]
        by_job = {item['job_id']: item for item in claims}
        values = [item['assessment'] for item in claims]
        if 'contradictory' in values:
            status, rule = 'conflicting', 'explicit_local_contradiction'
        elif 'present' in values:
            status, rule = 'retained', 'any_required_chunk_present'
        elif (set(required).issubset(completed) and set(by_job) == set(required)
              and values and set(values) == {'not_found'}):
            status, rule = 'omitted', 'all_required_chunks_negative'
        else:
            status, rule = 'not_assessed', 'required_chunk_incomplete_or_uncertain'
        candidate = candidate_info[key]
        result = dict(direction='source_to_output', identity=candidate['id'],
            global_status=status, required_jobs=required,
            completed_jobs=sorted(set(required) & completed),
            supporting_local_jobs=sorted(by_job), source_refs=deepcopy(candidate['source_refs']),
            output_refs=[], reviewer_claims=[{k: deepcopy(item[k]) for k in
                ('job_id', 'reviewer', 'assessment', 'reason', 'issue_id')} for item in claims],
            host_adjudication_rule=rule, issue_id=None)
        if status == 'omitted':
            issue, error = _global_issue('source_to_output', candidate['id'], status, claims,
                source_refs=candidate['source_refs'], output_refs=[], binding=binding)
            if error:
                result.update(global_status='conflicting', host_adjudication_rule=error)
            else:
                result['issue_id'] = issue['id']
                final_issues.append(issue)
                provenance.append(dict(issue_id=issue['id'], global_status='omitted',
                    supporting_local_jobs=sorted(by_job), reviewer_claims=result['reviewer_claims'],
                    host_adjudication_rule='all_required_chunks_negative'))
        global_results.append(result)

    for claim_key in sorted(claim_groups):
        claims = sorted(claim_groups[claim_key], key=lambda item: item['job_id'])
        output_index = claims[0]['output_chunk_index']
        required = [f'output_to_source:{index}:{output_index}:0'
                    for index in range(len(source_chunks))]
        by_job = {item['job_id']: item for item in claims}
        values = [item['assessment'] for item in claims]
        if 'contradictory' in values:
            status, rule = 'conflicting', 'explicit_local_contradiction'
        elif 'supported' in values:
            status, rule = 'supported', 'any_required_chunk_supported'
        elif (set(required).issubset(completed) and set(by_job) == set(required)
              and values and set(values) == {'no_support_found'}):
            status, rule = 'unsupported', 'all_required_chunks_negative'
        else:
            status, rule = 'not_assessed', 'required_chunk_incomplete_or_uncertain'
        claim_ref = claims[0]['claim_ref']
        result = dict(direction='output_to_source', identity=claim_key,
            global_status=status, required_jobs=required,
            completed_jobs=sorted(set(required) & completed),
            supporting_local_jobs=sorted(by_job), source_refs=[], output_refs=[deepcopy(claim_ref)],
            reviewer_claims=[{k: deepcopy(item[k]) for k in
                ('job_id', 'reviewer', 'assessment', 'reason', 'issue_id')} for item in claims],
            host_adjudication_rule=rule, issue_id=None)
        if status == 'unsupported':
            issue, error = _global_issue('output_to_source', claim_key, status, claims,
                source_refs=[], output_refs=[claim_ref], binding=binding)
            if error:
                result.update(global_status='conflicting', host_adjudication_rule=error)
            else:
                result['issue_id'] = issue['id']
                final_issues.append(issue)
                provenance.append(dict(issue_id=issue['id'], global_status='unsupported',
                    supporting_local_jobs=sorted(by_job), reviewer_claims=result['reviewer_claims'],
                    host_adjudication_rule='all_required_chunks_negative'))
        global_results.append(result)

    for issue_id in sorted(direct):
        records = direct[issue_id]
        issue = deepcopy(records[0]['issue'])
        if any(encoded(record['issue']) != encoded(issue) for record in records[1:]):
            issue['status'] = 'needs_manual_review'
        final_issues.append(issue)
        provenance.append(dict(issue_id=issue_id, global_status='local_scope_issue',
            supporting_local_jobs=sorted(record['job_id'] for record in records),
            reviewer_claims=[dict(job_id=record['job_id'], reviewer=record['issue']['reviewer'],
                                  reason=record['issue']['reason']) for record in records],
            host_adjudication_rule='exact_local_issue'))

    final_issues.sort(key=lambda item: item['id'])
    global_results.sort(key=lambda item: (item['direction'], item['identity']))
    local_issues.sort(key=lambda item: (item['job_id'], item['issue']['id']))
    provenance.sort(key=lambda item: item['issue_id'])
    public_assessments = []
    for item in local_assessments:
        public = {key: deepcopy(item[key]) for key in
                  ('job_id', 'direction', 'source_chunk_id', 'output_chunk_index',
                   'reviewer', 'kind', 'assessment', 'reason', 'issue_id')}
        if item['kind'] == 'candidate':
            public['candidate_id'] = item['candidate_id']
        else:
            public['claim_ref'] = deepcopy(item['claim_ref'])
        public_assessments.append(public)
    return dict(local_assessments=public_assessments, local_issues=local_issues,
                global_adjudications=global_results, issues=final_issues,
                issue_provenance=provenance)
