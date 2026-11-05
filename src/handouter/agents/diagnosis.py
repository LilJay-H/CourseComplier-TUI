"""Make CLI Agent failures actionable without exposing their raw output.

Child output is still forwarded to stderr as before. A bounded tail is kept in
memory only to recognise a few well-known failure shapes (not logged in, quota
or rate limit, network). Only fixed hint strings, plus a short reset time when
the CLI prints one, are ever added to results; transcript or note text from the
tail is never copied into receipts, state or the final message.
"""

from __future__ import annotations

import codecs
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from typing import Any

TAIL_BYTES = 16 * 1024
HEARTBEAT_SECONDS = 30.0


def heartbeat_line(elapsed: float) -> str:
    """A progress line in the CLI's `[progress]` format: the Agent reports no
    percentage, so show elapsed time instead of a bar frozen at 0%."""
    minutes, seconds = divmod(int(elapsed), 60)
    spent = f'{minutes} 分 {seconds:02d} 秒' if minutes else f'{seconds} 秒'
    return (f"[progress] {'Agent/交付':<10} [{'-' * 24}]   0%  "
            f"Agent 正在写讲义，已用 {spent}（通常需要 5–25 分钟）")

LOGIN_HINTS = {
    'claude': 'Claude Code CLI 未登录：在终端运行 claude，输入 /login 完成登录（桌面 App 的登录不与命令行共用），再用 continue 续作',
    'codex': 'Codex CLI 未登录：在终端运行 codex login 完成登录，再用 continue 续作',
}

_LOGIN = re.compile(r'not logged in|please run /login|please log ?in|run `?codex login|invalid api key'
                    r'|authentication (?:failed|required)|unauthori[sz]ed', re.I)
_QUOTA = re.compile(r'hit your [^\n]{0,40}limit|usage limit|rate[ -]limit|quota|too many requests'
                    r'|credit balance is too low', re.I)
# Only a clock/duration shape is ever echoed (e.g. "3pm", "14:30", "2h 5m"),
# never surrounding words, so no lecture text can reach receipts.
_RESET = re.compile(r'(?:resets?|try again) (?:at |in )?([0-9]{1,2}(?::[0-9]{2})?\s?(?:[ap]m|[hm](?:\s?[0-9]{1,2}m)?)?)\b', re.I)
# A CLI prints its own fatal error last; only that end of the output is
# matched, so a transcript or tool output mentioning "quota" or
# "unauthorized" earlier in the run cannot be mistaken for the failure cause.
DIAGNOSIS_WINDOW = 1500
_NETWORK = re.compile(r'stream disconnected|network error|connection (?:refused|reset|error|timed out)'
                      r'|ENOTFOUND|ECONNRESET|ETIMEDOUT|getaddrinfo failed', re.I)


def diagnose(agent: str, returncode: int, tail: str) -> str | None:
    """Return one fixed, user-facing hint for a failed CLI Agent run, or None."""
    if returncode == 0 or not tail:
        return None
    tail = tail[-DIAGNOSIS_WINDOW:]
    if _LOGIN.search(tail):
        return LOGIN_HINTS.get(agent, f'{agent} CLI 未登录：请先在终端完成登录再续作')
    if _QUOTA.search(tail):
        reset = _RESET.search(tail)
        when = f'（CLI 提示 {reset.group(1).strip()} 恢复）' if reset else ''
        other = {'claude': 'codex', 'codex': 'claude'}.get(agent)
        switch = f'，或改用 --agent {other}' if other else ''
        return f'{agent} 额度已用完或被限流{when}：等额度恢复后用 continue 续作{switch}；已完成的 ASR 等步骤会复用'
    if _NETWORK.search(tail):
        return f'{agent} 网络连接失败：检查网络或代理后用 continue 续作'
    return None


_MARKERS = ('CLI 未登录：', '额度已用完或被限流', '网络连接失败：')


def hint_from_errors(errors) -> str | None:
    """Recover a hint already placed in validation errors (e.g. by staged runs)."""
    for error in errors:
        for marker in _MARKERS:
            if marker in error:
                start = error.find('：', error.find(' exited with code ')) + 1 if ' exited with code ' in error else 0
                return error[start:] if start else error
    return None


def run_forwarding(command: list[str], *, input_text: str, cwd: Any,
                   heartbeat: float | None = HEARTBEAT_SECONDS) -> tuple[int, str]:
    """Run like before (output streamed to stderr) and also return a short output tail.

    Every `heartbeat` seconds an elapsed-time `[progress]` line is written from the
    same thread that forwards the child's bytes, starting on a fresh line, so it
    never splits a line of child output.
    """
    # Separate writer/reader handles: an inherited Windows handle shares its file
    # pointer, so the reader must never seek on the child's handle.
    # ignore_cleanup_errors: a grandchild still holding the log (or a virus
    # scanner) must not turn a finished Agent run into an exception that skips
    # audit and publication.
    with tempfile.TemporaryDirectory(prefix='handouter-agent-log-', ignore_cleanup_errors=True) as directory, \
            open(f'{directory}/output.log', 'wb') as sink, \
            open(f'{directory}/output.log', 'rb', buffering=0) as reader:
        done = threading.Event()
        tail: deque[str] = deque()
        size = [0]
        line_open = [False]
        started = time.monotonic()
        next_beat = [started + heartbeat if heartbeat else None]

        def remember(text: str, raw: bytes = b'') -> None:
            # Forward the child's bytes unchanged when stderr is a real stream, so a
            # redirected log keeps the child's own (UTF-8) encoding as before.
            try:
                buffer = getattr(sys.stderr, 'buffer', None)
                if buffer is not None and raw:
                    sys.stderr.flush()
                    buffer.write(raw)
                    buffer.flush()
                elif text:
                    sys.stderr.write(text)
                    sys.stderr.flush()
            except (OSError, ValueError):
                pass
            if raw:
                line_open[0] = not raw.endswith((b'\n', b'\r'))
            elif text:
                line_open[0] = not text.endswith(('\n', '\r'))
            if not text:
                return
            tail.append(text)
            size[0] += len(text)
            while size[0] > TAIL_BYTES and len(tail) > 1:
                size[0] -= len(tail.popleft())

        def pump() -> None:
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            while True:
                finished = done.is_set()
                try:
                    chunk = reader.read()
                except (OSError, ValueError):
                    return  # The log was closed after a stuck join; nothing left to forward.
                if chunk:
                    remember(decoder.decode(chunk), chunk)
                elif finished:
                    remember(decoder.decode(b'', final=True))
                    return
                else:
                    time.sleep(0.2)
                now = time.monotonic()
                if next_beat[0] is not None and now >= next_beat[0]:
                    next_beat[0] = now + heartbeat
                    try:
                        sys.stderr.write(('\n' if line_open[0] else '') + heartbeat_line(now - started) + '\n')
                        sys.stderr.flush()
                        line_open[0] = False
                    except (OSError, ValueError):
                        pass

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            completed = subprocess.run(command, input=input_text, text=True, cwd=cwd,
                                       stdout=sink, stderr=subprocess.STDOUT,
                                       encoding='utf-8', errors='replace', check=False)
        finally:
            done.set()
            thread.join(timeout=10)
        return completed.returncode, ''.join(tail)


def login_status(agent: str, executable: str | None, *, timeout: float = 20) -> str:
    """Best-effort: 'logged_in', 'logged_out' or 'unknown'. Never blocks a run on 'unknown'."""
    if not executable:
        return 'unknown'
    command = {'claude': [executable, 'auth', 'status'], 'codex': [executable, 'login', 'status']}.get(agent)
    if command is None:
        return 'unknown'
    # Output goes to files, not pipes: on Windows the npm `.cmd` wrapper's node
    # grandchild keeps an inherited pipe open after a timeout kills cmd.exe, and
    # reading such a pipe never finishes. A file never blocks the check.
    try:
        with tempfile.TemporaryDirectory(prefix='handouter-login-', ignore_cleanup_errors=True) as directory:
            with open(f'{directory}/out', 'wb') as out, open(f'{directory}/err', 'wb') as err:
                returncode = subprocess.run(command, stdout=out, stderr=err, timeout=timeout,
                                            check=False, stdin=subprocess.DEVNULL).returncode
            with open(f'{directory}/out', 'rb') as out, open(f'{directory}/err', 'rb') as err:
                stdout = out.read(64 * 1024).decode('utf-8', 'replace')
                stderr = err.read(64 * 1024).decode('utf-8', 'replace')
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    output = stdout + stderr
    if agent == 'claude':
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get('loggedIn'), bool):
            return 'logged_in' if data['loggedIn'] else 'logged_out'
    if re.search(r'not logged in|logged out', output, re.I):
        return 'logged_out'
    if returncode == 0 and re.search(r'logged in', output, re.I):
        return 'logged_in'
    return 'unknown'
