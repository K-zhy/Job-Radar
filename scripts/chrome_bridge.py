from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

SESSION_ENV_VAR = 'ROLE_SCOUT_CHROME_SESSION'
SESSION_DIR = Path(tempfile.gettempdir()) / 'role-scout-chrome'


def escape_applescript(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"')


def run_osascript(script: str) -> str:
    process = subprocess.Popen(
        ['osascript', '-e', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.strip() or 'osascript failed')
    return stdout.strip()


def session_name(explicit: str | None = None) -> str:
    name = explicit or os.environ.get(SESSION_ENV_VAR, '')
    return str(name).strip()


def session_file(name: str) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^a-zA-Z0-9_.-]+', '-', name)
    return SESSION_DIR / f'{safe}.json'


def load_window_id(name: str) -> int | None:
    if not name:
        return None
    path = session_file(name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get('window_id')
    if isinstance(value, int):
        return value
    return None


def save_window_id(name: str, window_id: int) -> None:
    if not name:
        return
    path = session_file(name)
    path.write_text(json.dumps({'window_id': window_id}), encoding='utf-8')


def clear_window_id(name: str) -> None:
    if not name:
        return
    path = session_file(name)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def current_window_url(window_id: int) -> str | None:
    script = f'''
tell application "Google Chrome"
    return URL of active tab of window id {window_id}
end tell
'''
    try:
        return run_osascript(script).strip()
    except RuntimeError:
        return None


def should_reuse_window(window_id: int) -> bool:
    current_url = current_window_url(window_id)
    if not current_url:
        return False
    normalized = current_url.strip().lower()
    if not normalized:
        return False
    if normalized == 'about:blank':
        return True
    return 'zhipin.com' in normalized


def open_url(url: str, explicit_session: str | None = None) -> dict:
    name = session_name(explicit_session)
    url_esc = escape_applescript(url)

    if not name:
        run_osascript(f'tell application "Google Chrome" to set URL of active tab of front window to "{url_esc}"')
        return {'ok': True, 'window_id': None, 'session': ''}

    window_id = load_window_id(name)
    if window_id is not None:
        if not should_reuse_window(window_id):
            clear_window_id(name)
            window_id = None

    if window_id is not None:
        script = f'''
set previousWindowId to missing value
tell application "Google Chrome"
    if (count of windows) > 0 then set previousWindowId to id of front window
    set URL of active tab of window id {window_id} to "{url_esc}"
    if previousWindowId is not missing value then
        if previousWindowId is not {window_id} then
            try
                set index of window id previousWindowId to 1
            end try
        end if
    end if
end tell
return {window_id}
'''
        try:
            run_osascript(script)
            return {'ok': True, 'window_id': window_id, 'session': name}
        except RuntimeError:
            clear_window_id(name)

    script = f'''
tell application "Google Chrome" to launch
set previousWindowId to missing value
set targetWindowId to missing value
tell application "Google Chrome"
    if (count of windows) > 0 then set previousWindowId to id of front window
    set newWindow to make new window
    set targetWindowId to id of newWindow
    set URL of active tab of newWindow to "{url_esc}"
    if previousWindowId is not missing value then
        if previousWindowId is not targetWindowId then
            try
                set index of window id previousWindowId to 1
            end try
        end if
    end if
end tell
return targetWindowId
'''
    result = run_osascript(script)
    target_window_id = int(result)
    save_window_id(name, target_window_id)
    return {'ok': True, 'window_id': target_window_id, 'session': name}


def execute_js(code: str, explicit_session: str | None = None) -> dict:
    name = session_name(explicit_session)
    code_esc = escape_applescript(code)

    if not name:
        result = run_osascript(
            f'tell application "Google Chrome" to execute active tab of front window javascript "{code_esc}"'
        )
        return {'ok': True, 'result': result, 'window_id': None, 'session': ''}

    window_id = load_window_id(name)
    if window_id is None:
        return {'ok': False, 'error': f'No Chrome session window for {name}'}

    script = f'''
set previousWindowId to missing value
set jsResult to missing value
tell application "Google Chrome"
    if (count of windows) > 0 then set previousWindowId to id of front window
    set jsResult to execute active tab of window id {window_id} javascript "{code_esc}"
    if previousWindowId is not missing value then
        if previousWindowId is not {window_id} then
            try
                set index of window id previousWindowId to 1
            end try
        end if
    end if
end tell
return jsResult
'''
    try:
        result = run_osascript(script)
    except RuntimeError as exc:
        clear_window_id(name)
        return {'ok': False, 'error': str(exc)}

    return {'ok': True, 'result': result, 'window_id': window_id, 'session': name}


def close_session(explicit_session: str | None = None) -> dict:
    name = session_name(explicit_session)
    if not name:
        return {'ok': True, 'session': ''}
    window_id = load_window_id(name)
    if window_id is None:
        return {'ok': True, 'session': name}

    script = f'''
tell application "Google Chrome"
    close window id {window_id}
end tell
'''
    try:
        run_osascript(script)
    except RuntimeError as exc:
        clear_window_id(name)
        return {'ok': False, 'error': str(exc), 'session': name}

    clear_window_id(name)
    return {'ok': True, 'session': name}