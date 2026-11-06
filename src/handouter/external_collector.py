"""Site-neutral v1 acquisition bridge for the standalone CLI/TUI.

Only a user-configured, digest-pinned executable may read browser exports.
The local processing pipeline sees the fully validated standard package only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .platform_support import start_task_process, terminate_process_tree
from .settings import _check_config_path, directory_settings_path
from .credentials import scan_credentials

MAX_FRAME = 16 * 1024
_CONFIG_KEYS = {"executable", "sha256", "tool_id", "exporter_script"}
_STAGES = {"preparing", "downloading", "packaging", "publishing"}


def _group_has_no_live_members(pid: int) -> bool:
    """A Darwin EPERM is conclusive only after checking the exact process group."""
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="],
            capture_output=True, text=True, timeout=2, check=False)
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or not fields[0].isdigit() or not fields[1]:
                return False
            if int(fields[0]) == pid and not fields[1].startswith("Z"):
                return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def configuration_path() -> Path:
    return directory_settings_path().with_name("collector.json")


def _regular(path: Path) -> Path:
    _check_config_path(path)
    if not path.is_absolute() or not path.is_file():
        if path.is_absolute() and not path.exists():
            raise ValueError(f"COLLECTOR_PATH: 采集器文件不存在：{path}。"
                             "如果移动或重装过插件，请重新运行 collector-configure")
        raise ValueError("COLLECTOR_PATH: 请选择独立安装的采集器普通文件")
    return path


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _validated(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _CONFIG_KEYS:
        raise ValueError("COLLECTOR_CONFIG: 插件配置无效，请重新配置")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ValueError("COLLECTOR_CONFIG: 需要可信发布渠道提供的 SHA-256")
    if not isinstance(value["tool_id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", value["tool_id"]):
        raise ValueError("COLLECTOR_CONFIG: 工具身份无效")
    if not isinstance(value["executable"], str):
        raise ValueError("COLLECTOR_CONFIG: 工具路径无效")
    executable = _regular(Path(value["executable"]).expanduser())
    if _digest(executable) != value["sha256"]:
        raise ValueError("COLLECTOR_HASH_MISMATCH: 插件与已保存摘要不符，请重新核对配置")
    script = value["exporter_script"]
    if script is not None:
        if not isinstance(script, str):
            raise ValueError("COLLECTOR_CONFIG: 脚本路径无效")
        _regular(Path(script).expanduser())
    return value


def load_configuration() -> dict:
    path = configuration_path()
    _check_config_path(path)
    if not path.exists():
        raise ValueError("COLLECTOR_NOT_CONFIGURED: 浏览器 ZIP 需要独立采集插件；先运行 course-compiler collector-configure --help")
    if path.stat().st_size > MAX_FRAME:
        raise ValueError("COLLECTOR_CONFIG: 插件配置无效，请重新配置")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("COLLECTOR_CONFIG: 插件配置无效，请重新配置") from exc
    return _validated(value)


def configured_exporter_script() -> Path | None:
    path = configuration_path()
    if not path.exists():
        return None
    try:
        _check_config_path(path)
        if path.stat().st_size > MAX_FRAME:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("exporter_script"), str):
            return None
        return _regular(Path(value["exporter_script"]).expanduser())
    except (OSError, ValueError):
        # This is optional display metadata, never an execution decision. A
        # broken plugin must not prevent unrelated TXT/media workflows opening.
        return None


def _invoke(config: dict, method: str, params: dict, *, progress=None, timeout=30) -> dict:
    _validated(config)
    request = json.dumps({"protocol_version": 1, "method": method, "params": params}, ensure_ascii=False).encode() + b"\n"
    if len(request) > MAX_FRAME:
        raise ValueError("COLLECTOR_REQUEST_LIMIT")
    env = dict(os.environ)
    # The trusted bootstrap joins a Windows Job before it starts the plugin.
    # It imports the installed TUI package, not an arbitrary ZIP program.
    process, job = start_task_process(["--external-collector", config["executable"]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    events = queue.Queue(maxsize=32)
    stopped = threading.Event()

    def reader():
        try:
            while not stopped.is_set():
                raw = process.stdout.readline(MAX_FRAME + 1)
                event = ("frame", raw) if raw else ("eof", None)
                while not stopped.is_set():
                    try:
                        events.put(event, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if not raw or len(raw) > MAX_FRAME:
                    return
        except OSError:
            if not stopped.is_set():
                events.put(("eof", None))

    worker = threading.Thread(target=reader, daemon=True)
    def writer():
        try:
            process.stdin.write(request)
            process.stdin.close()
        except (OSError, ValueError):
            pass  # The bounded reader/deadline reports the child failure.
    input_worker = threading.Thread(target=writer, daemon=True)
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGTERM)
        def cancelled(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, cancelled)
    try:
        worker.start()
        deadline = time.monotonic() + timeout
        input_worker.start()
        count = 0
        byte_frames = 0
        terminal = None
        while terminal is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("COLLECTOR_TIMEOUT: 插件超时，原始文件已保留")
            try:
                kind, raw = events.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            if kind == "eof" or len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                raise ValueError("COLLECTOR_PROTOCOL: 插件响应无效")
            try:
                frame = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("COLLECTOR_PROTOCOL: 插件响应无效") from exc
            if not isinstance(frame, dict):
                raise ValueError("COLLECTOR_PROTOCOL: 插件响应无效")
            if frame.get("type") == "progress" and method == "convert":
                # Byte frames are counted apart: a long download from a plugin that
                # reports often must not trip the stage-frame limit hours in.
                if "completed_bytes" in frame:
                    byte_frames += 1
                else:
                    count += 1
                if frame.get("stage") not in _STAGES or count > 1000 or byte_frames > 100_000:
                    raise ValueError("COLLECTOR_PROTOCOL: 插件进度无效")
                if progress:
                    progress("本机采集", *_progress_display(frame))
            elif frame.get("type") == "error":
                # Only known codes map to fixed local wording; plugin messages are never echoed.
                if frame.get("code") == "COLLECTOR_NO_MEDIA":
                    raise ValueError("COLLECTOR_NO_MEDIA: 资料包里没有回放视频地址，通常是网页没加载完就导出了；"
                                     "请打开该课的回放页，等视频和 PPT 加载完成后重新导出；原件已保留")
                raise ValueError("COLLECTOR_FAILED: 采集未完成，请检查授权、媒体域名和插件；原件已保留")
            elif frame.get("type") == {"identity":"identity", "inspect":"inspection", "convert":"result"}[method]:
                terminal = frame
            else:
                raise ValueError("COLLECTOR_PROTOCOL: 插件响应类型无效")
        try:
            if process.wait(timeout=5) != 0:
                raise ValueError("COLLECTOR_FAILED: 插件执行失败，原件已保留")
        except subprocess.TimeoutExpired as exc:
            raise ValueError("COLLECTOR_TIMEOUT: 插件未正常退出") from exc
        return terminal
    except (OSError, BrokenPipeError) as exc:
        raise ValueError("COLLECTOR_FAILED: 无法运行已配置的插件，原件已保留") from exc
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        stopped.set()
        cleanup_errors = []
        cleanup = terminate_process_tree(process, grace_seconds=0.1, cleanup_errors=cleanup_errors)
        if job is not None:
            job.close()
        if cleanup is not None:
            cleanup.join()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        worker.join(timeout=1)
        input_worker.join(timeout=1)
        process.stdout.close()
        if cleanup_errors and not (sys.platform == "darwin"
                and all(isinstance(error, PermissionError) for error in cleanup_errors)
                and _group_has_no_live_members(process.pid)):
            raise ValueError("COLLECTOR_CONTAINMENT: 无法确认采集进程树已结束，请检查任务进程")


def _progress_display(frame: dict) -> tuple[int, str]:
    """Bar position and wording for one plugin progress frame.

    Stage-only frames get fixed positions so the bar visibly advances. With the
    optional `progress_bytes` extension a downloading frame also carries
    `completed_bytes` and `total_bytes` (null when unknown); the download then
    fills 10–79 % of the bar, or shows the amount received so far.
    """
    stage = frame["stage"]
    if stage == "downloading" and "completed_bytes" in frame:
        done, total = frame.get("completed_bytes"), frame.get("total_bytes")
        if (type(done) is not int or done < 0
                or not (total is None or (type(total) is int and total > 0))):
            raise ValueError("COLLECTOR_PROTOCOL: 插件进度无效")
        mb = 1024 * 1024
        if total:
            ratio = min(1.0, done / total)
            return 10 + int(69 * ratio), f"下载媒体 {done // mb}/{total // mb} MB"
        return 10, f"下载媒体，已收到 {done // mb} MB"
    return {"preparing": (5, "准备材料"),
            "downloading": (10, "下载媒体（视频较大，可能需要几分钟）"),
            "packaging": (80, "生成标准包"), "publishing": (95, "发布标准包")}[stage]


def _identity(config: dict) -> list:
    frame = _invoke(config, "identity", {})
    capabilities = frame.get("capabilities", [])
    if (frame.get("tool_id") != config["tool_id"] or frame.get("protocol_version") != 1
            or not isinstance(frame.get("tool_version"), str) or not isinstance(capabilities, list)
            or not all(cap in capabilities for cap in ("inspect", "convert"))):
        raise ValueError("COLLECTOR_IDENTITY: 工具身份或协议不匹配")
    return capabilities


def configure(executable: str, sha256: str, tool_id: str, exporter_script: str | None = None) -> dict:
    value = {"executable": str(Path(executable).expanduser().absolute()), "sha256": sha256.lower(),
             "tool_id": tool_id, "exporter_script": str(Path(exporter_script).expanduser().absolute()) if exporter_script else None}
    _validated(value)
    _identity(value)  # Always a fresh identity check, never the cache.
    _INSPECTED.clear()
    _CAPABILITIES.clear()
    path = configuration_path()
    _check_config_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_config_path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".collector-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"tool_id": tool_id, "protocol_version": 1}


def _binding(config: dict) -> str:
    return hashlib.sha256(json.dumps({key: config[key] for key in ("executable", "sha256", "tool_id")}, sort_keys=True).encode()).hexdigest()


# One CLI run used to inspect the same export five times (each an identity plus
# an inspect process). Results are reused within this process while the
# source file and the configured plugin are unchanged; every process that does
# start still has its executable digest checked first (_invoke -> _validated).
_INSPECTED: dict[tuple, dict] = {}
_CAPABILITIES: dict[tuple, list] = {}


def _file_key(path: Path) -> tuple:
    status = path.stat()
    return str(path), status.st_size, status.st_mtime_ns


def _capabilities(config: dict) -> list:
    key = (_binding(config), *_file_key(Path(config["executable"])))
    if key not in _CAPABILITIES:
        _CAPABILITIES[key] = _identity(config)
    return _CAPABILITIES[key]


def inspect_browser_export(source: str | Path, *, configuration: dict | None = None) -> dict:
    config = load_configuration() if configuration is None else configuration
    source = _regular(Path(source).expanduser().absolute())
    key = (_binding(config), *_file_key(Path(config["executable"])), *_file_key(source))
    if key in _INSPECTED:
        return dict(_INSPECTED[key])
    _capabilities(config)
    frame = _invoke(config, "inspect", {"source_path": str(source)})
    title = frame.get("course_title")
    day = frame.get("lecture_date")
    hosts = frame.get("media_hosts")
    digest = frame.get("source_sha256")
    if (frame.get("tool_id") != config["tool_id"] or frame.get("protocol_version") != 1
            or not isinstance(title, str) or not title.strip() or len(title) > 300
            or any(ord(char) < 32 for char in title) or "://" in title
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(hosts, list) or not 1 <= len(hosts) <= 16
            or any(not isinstance(host, str) or len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")) for host in hosts)
            or len(set(hosts)) != len(hosts)):
        raise ValueError("COLLECTOR_PROTOCOL: 插件预检结果无效")
    if day is not None:
        from .organization import lecture_date
        day = lecture_date(day)
    if scan_credentials(title).errors or scan_credentials(title).warnings:
        raise ValueError("COLLECTOR_PROTOCOL: 插件显示信息包含敏感字段")
    if _digest(source) != digest:
        raise ValueError("SOURCE_CHANGED: 原始资产已变化，请重新预检")
    result = {"course_title": title, "lecture_date": day, "media_hosts": hosts, "source_sha256": digest,
              "collector_binding": _binding(config)}
    if "source_id" in frame:
        if not isinstance(frame["source_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", frame["source_id"]):
            raise ValueError("COLLECTOR_PROTOCOL: 插件来源身份无效")
        if scan_credentials(frame["source_id"]).errors or scan_credentials(frame["source_id"]).warnings:
            raise ValueError("COLLECTOR_PROTOCOL: 插件来源身份包含敏感字段")
        result["source_id"] = frame["source_id"]
    if "source_fingerprint" in frame:
        if not isinstance(frame["source_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", frame["source_fingerprint"]):
            raise ValueError("COLLECTOR_PROTOCOL: 插件来源指纹无效")
        result["source_fingerprint"] = frame["source_fingerprint"]
    if len(_INSPECTED) >= 64:
        _INSPECTED.clear()
    _INSPECTED[key] = dict(result)
    return result


def convert_browser_export(source: str | Path, output: str | Path, *, allowed_media_hosts: list[str], expected_source_sha256: str | None = None, expected_collector_binding: str | None = None, progress=None):
    config = load_configuration()
    if expected_collector_binding is not None and _binding(config) != expected_collector_binding:
        raise ValueError("COLLECTOR_CHANGED: 已确认的插件配置已变化，请重新预检")
    details = inspect_browser_export(source, configuration=config)
    if expected_source_sha256 is not None and details["source_sha256"] != expected_source_sha256:
        raise ValueError("SOURCE_CHANGED: 原始资产已变化，请重新确认")
    if set(allowed_media_hosts) != set(details["media_hosts"]):
        raise ValueError("COLLECTOR_HOST_CONFIRMATION: 请确认预检显示的全部媒体域名后重试")
    capabilities = _capabilities(config)
    target = Path(output).expanduser().absolute()
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise ValueError("COLLECTOR_OUTPUT: 输出必须是新的标准包路径")
    params = {"source_path": str(Path(source).expanduser().absolute()),
        "source_sha256": details["source_sha256"], "allowed_media_hosts": details["media_hosts"],
        "output_path": str(target)}
    if "progress_bytes" in capabilities:
        # Optional extension: only plugins that declare it receive the switch,
        # so older plugins keep their strict request schema.
        params["progress_bytes"] = True
    result = _invoke(config, "convert", params, progress=progress, timeout=6 * 60 * 60)
    if result.get("output_path") != str(target):
        raise ValueError("COLLECTOR_PROTOCOL: 插件输出路径无效")
    from handouter_local.assets import inspect
    inspected = inspect(target)
    if inspected.source_kind != "asset_zip":
        raise ValueError("COLLECTOR_OUTPUT_INVALID: 标准包未通过完整预检")
    return inspected
