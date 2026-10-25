"""One exact prose replacement. Owned structures and unrelated bytes stay fixed."""
from .authoring_stages import encoded, sha
from .authoring_v2 import strict_json
from .output_refs import resolve_output_ref
from .quality_host import exact_replacement


def repair_candidate(data, proposal, binding, issue, allowed_ref, root, sources, plan):
    value = strict_json(proposal) if isinstance(proposal, bytes) else strict_json(encoded(proposal))
    if (not isinstance(value, dict) or set(value) != set(binding) | {'issue_id', 'output_ref', 'replacement'}
            or any(value[k] != v or type(value[k]) is not type(v) for k, v in binding.items())
            or value['issue_id'] != issue['id'] or value['output_ref'] != allowed_ref
            or not isinstance(value['replacement'], str) or len(value['replacement']) > 65536):
        raise ValueError('REPAIR_SCOPE_INVALID')
    loc = resolve_output_ref(data, allowed_ref, root=root, sources=sources, mode=binding['output_mode'], plan=plan)
    text = data.decode('utf-8')
    changed_data, changed_ref = exact_replacement(data, allowed_ref, value['replacement'])
    changed = changed_data.decode('utf-8')
    manifest = None
    raw = changed
    if sources.get('composer'):
        from .composition_store import checked_composition
        from .composer import compose_with_manifest
        old = checked_composition(root, sources, data, binding['output_mode'], plan)
        edits = [(loc.start, loc.end, value['replacement'])]
        for obj in old['objects']:
            parts = ((obj['open_span'], '[[details:' + obj['id'] + ']]'),
                     (obj['close_span'], '[[/details:' + obj['id'] + ']]')) if obj['kind'] == 'details' else (
                         (obj['output_span'], '[[' + obj['kind'] + ':' + obj['id'] + ']]'),)
            for (start, end), marker in parts:
                # Empty insertion anchors inside owned blocks are protected too.
                if (loc.start < end and loc.end > start) or (loc.start == loc.end and start <= loc.start < end):
                    raise ValueError('REPAIR_COMPOSER_OWNERSHIP')
                edits.append((start, end, marker))
        raw = text
        for start, end, replacement in sorted(edits, reverse=True):
            raw = raw[:start] + replacement + raw[end:]
        recomposed, manifest = compose_with_manifest(raw, root, sources, plan['objects'],
            binding['output_mode'], plan_revision=plan['revision'], plan_hash=plan['plan_hash'])
        if recomposed != changed:
            raise ValueError('REPAIR_RECOMPOSITION_CHANGED_UNRELATED_BYTES')
    return changed_data, raw.encode('utf-8'), manifest, changed_ref
