"""Small persistent audio/ASR cache; only completed, hashed stages are reusable."""
import hashlib
import json
from pathlib import Path
import shutil
import uuid
import zipfile
from urllib.parse import urlsplit, parse_qsl, urlencode

from .asr import ASRResult
from .media import AudioExtractResult
from .safe_io import safe_path, read_bytes, publish_bytes, relative_path, sha256_file


def _media_identity(url: str) -> str:
    parsed = urlsplit(url)
    transient = {'auth_key', 'token', 'access_token', 'signature', 'sig', 'expires', 'expiration', 'api_key', 'cookie'}
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in transient and not key.lower().startswith('x-amz-')]
    return parsed._replace(query=urlencode(query), fragment='').geturl()


def asset_fingerprint(path: str | Path) -> str:
    """Content identity excludes private URL query rotation, never stores URLs."""
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        if "handouter-input.json" not in archive.namelist():
            from .external_collector import inspect_browser_export
            details = inspect_browser_export(path)
            return details.get("source_fingerprint") or details["source_sha256"]
        infos = archive.infolist()
        if len(infos) > 10000 or sum(i.file_size for i in infos) > 512 * 1024 * 1024:
            raise ValueError('资产超出安全大小限制')
        names = set()
        for info in sorted(infos, key=lambda i: i.filename):
            if info.is_dir():
                continue
            name = relative_path(info.filename)
            if name in names:
                raise ValueError('资产包含重复成员')
            names.add(name)
            digest.update(name.encode('utf-8') + b'\0')
            with archive.open(info) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(source: str | Path, *, asset: str | Path | None = None) -> str:
    text = str(source)
    parsed = urlsplit(text)
    digest = hashlib.sha256()
    if parsed.scheme.lower() in {'http', 'https'}:
        # Asset contents and media location bind a browser task. Only signature
        # refresh is ignored; no address is written to public metadata.
        identity = _media_identity(text) if asset else text
        digest.update(identity.encode('utf-8'))
    else:
        with Path(source).open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
    if asset:
        digest.update(asset_fingerprint(asset).encode())
    return digest.hexdigest()


def local_input_fingerprint(primary: str | Path, **extras: str | Path | None) -> str:
    """Bind a loose local source and explicitly selected ancillary materials."""
    digest = hashlib.sha256(source_fingerprint(primary).encode())
    for role, value in sorted(extras.items()):
        if value is None:
            continue
        candidate = Path(value).expanduser()
        if candidate.is_symlink():
            raise FileNotFoundError(f"本地附加材料不存在或不是安全普通路径: {candidate}")
        path = candidate.resolve()
        if not path.exists():
            raise FileNotFoundError(f"本地附加材料不存在或不是安全普通路径: {path}")
        digest.update(role.encode() + b'\0')
        if path.is_file():
            digest.update(path.name.encode() + b'\0' + source_fingerprint(path).encode())
        elif path.is_dir():
            for item in sorted(path.iterdir(), key=lambda entry: entry.name.casefold()):
                if item.is_symlink() or not item.is_file():
                    continue
                digest.update(item.name.encode() + b'\0' + source_fingerprint(item).encode())
        else:
            raise ValueError(f"本地附加材料不是文件或目录: {path}")
    return digest.hexdigest()


class MaterialCache:
    def __init__(self, root: Path, lecture_id: str, identity: str, params: dict):
        self.root = root.resolve()
        key = hashlib.sha256(json.dumps([identity, params], sort_keys=True).encode()).hexdigest()
        self.relative = f'.handouter-cache/{lecture_id}/{key}'
        self.directory = safe_path(self.root, self.relative, missing=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.record_relative = self.relative + '/stages.json'
        record = safe_path(self.root, self.record_relative, missing=True)
        self.records = json.loads(read_bytes(self.root, self.record_relative)) if record.exists() else {}
        if not isinstance(self.records, dict):
            raise ValueError('缓存完成记录无效；请检查缓存，未自动删除')

    def _get(self, stage: str):
        record = self.records.get(stage)
        if not record:
            return None
        if record.get('status') != 'complete' or not str(record.get('path', '')).startswith(self.relative + '/'):
            raise ValueError('缓存阶段记录无效')
        expected = {record['path']} if stage == 'audio' else {
            record['path'] + '/' + name for name in ('raw.json', 'segments.json', 'transcript.txt')}
        if set(record.get('hashes', {})) != expected:
            raise ValueError('缓存阶段文件清单不完整')
        for relative, digest in record['hashes'].items():
            if not relative.startswith(self.relative + '/'):
                raise ValueError('缓存记录越界')
            if sha256_file(self.root, relative) != digest:
                raise ValueError('缓存文件哈希不匹配；拒绝复用，请查看 cache-status')
        return record

    def _save(self, stage: str, paths: list[Path], metadata: dict):
        hashes = {p.relative_to(self.root).as_posix(): sha256_file(self.root, p.relative_to(self.root).as_posix())
                  for p in paths}
        self.records[stage] = {**metadata, 'hashes': hashes, 'status': 'complete'}
        publish_bytes(self.root, self.record_relative,
                      json.dumps(self.records, ensure_ascii=False, indent=2).encode(), replace=True)

    def has_complete(self, stage: str) -> bool:
        return self._get(stage) is not None

    def audio(self, source, extract):
        record = self._get('audio')
        if record:
            path = safe_path(self.root, record['path'])
            return AudioExtractResult(path, record['duration_ms'], path.stat().st_size, record['mode'])
        output = self.directory / ('audio-' + uuid.uuid4().hex + '.m4a')
        result = extract(source, output)
        self._save('audio', [output], {'path': output.relative_to(self.root).as_posix(),
                   'duration_ms': result.duration_ms, 'mode': result.mode})
        return result

    def asr(self, audio, transcribe, **options):
        record = self._get('asr')
        if record:
            directory = safe_path(self.root, record['path'])
            return ASRResult(directory, directory / 'raw.json', directory / 'segments.json',
                             directory / 'transcript.txt', record['segment_count'], record['timestamp_kind'],
                             record['device'], record['elapsed_seconds'])
        output = self.directory / ('asr-' + uuid.uuid4().hex)
        computed = False
        try:
            result = transcribe(audio, output, **options)
            computed = True
            self._save('asr', [result.raw_path, result.segments_path, result.transcript_path],
                       {'path': output.relative_to(self.root).as_posix(), 'segment_count': result.segment_count,
                        'timestamp_kind': result.timestamp_kind, 'device': result.device,
                        'elapsed_seconds': result.elapsed_seconds})
            return result
        except BaseException:
            # Only this invocation's uncommitted attempt is eligible for cleanup.
            if not computed and output.exists() and not output.is_symlink():
                shutil.rmtree(output)
            raise
