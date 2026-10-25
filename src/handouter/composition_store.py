"""Host-only immutable composition evidence, separate from Agent declarations."""
import re

from .authoring_stages import encoded, prefix, read_object, sha
from .safe_io import publish_bytes, read_bytes, safe_path


def manifest_path(sources, digest, mode='complete'):
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('COMPOSITION_HASH_INVALID')
    if mode not in sources['authoring']['task_modes']:
        raise ValueError('COMPOSITION_MODE_INVALID')
    return prefix(sources) + '/composition-' + mode + '-' + digest + '.json'


def save_composition(root, sources, raw, manifest):
    directory = safe_path(root, prefix(sources), missing=True)
    directory.mkdir(parents=True, exist_ok=True)
    if sha(raw) != manifest['input_draft_sha256']:
        raise ValueError('COMPOSITION_DRAFT_CHANGED')
    for path, data in ((prefix(sources) + '/draft-' + sha(raw) + '.txt', raw),
                       (manifest_path(sources, manifest['output_sha256'], manifest['mode']), encoded(manifest))):
        if len(data) > (4 * 1024 * 1024 if path.endswith('.txt') else 512 * 1024):
            raise ValueError('COMPOSITION_TOO_LARGE')
        try:
            publish_bytes(root, path, data)
        except FileExistsError:
            if read_bytes(root, path) != data:
                raise ValueError('COMPOSITION_ARTIFACT_CONFLICT')


def checked_composition(root, sources, data, mode, plan):
    from .composer import validate_composition
    manifest = read_object(root, manifest_path(sources, sha(data), mode))
    if not isinstance(manifest, dict):
        raise ValueError('COMPOSITION_MANIFEST_INVALID')
    digest = manifest.get('input_draft_sha256')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('COMPOSITION_DRAFT_INVALID')
    path = prefix(sources) + '/draft-' + digest + '.txt'
    if safe_path(root, path).stat().st_size > 4 * 1024 * 1024:
        raise ValueError('COMPOSITION_TOO_LARGE')
    raw = read_bytes(root, path).decode('utf-8')
    errors = validate_composition(data.decode('utf-8'), raw, manifest, root, sources,
                                  plan['objects'], mode, plan_revision=plan['revision'],
                                  plan_hash=plan['plan_hash'])
    if errors:
        raise ValueError(errors[0])
    return manifest
