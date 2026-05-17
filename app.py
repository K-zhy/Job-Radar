#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).parent
LEGACY_DB_PATH = ROOT / 'role_scout.db'
JOB_RADAR_DB_PATH = ROOT / 'job_radar.db'
DB_PATH = LEGACY_DB_PATH if LEGACY_DB_PATH.exists() and not JOB_RADAR_DB_PATH.exists() else JOB_RADAR_DB_PATH
JOBS_YAML_PATH = ROOT / 'internships.yaml'
PREFS_YAML_PATH = ROOT / 'internship-prefs.yaml'
API_CONFIG_PATH = ROOT / 'api-config.yaml'
STATIC_DIR = ROOT / 'web'
FETCH_CHROME_SESSION = 'job-radar-fetch'
FETCH_LOG_LIMIT = 300
RESUME_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
RESUME_MARKDOWN_MAX_CHARS = 120_000
RESUME_CHAT_HISTORY_LIMIT = 18

DEFAULT_PREFS = {
    'cities': ['上海'],
    'job_type': '全职',
    'salary': {
        'min_day': 150,
        'min_month': 3000,
    },
    'scales': ['20-99人'],
    'queries': ['AI产品'],
    'filters': {
        'exclude_big_tech': ['无'],
        'exclude_non_tech': [],
        'exclude_extra': [],
    },
    'preferred_tags': [],
    'profile': {
        'education': '',
        'internship_duration': '',
        'note': '',
    },
}

DEFAULT_API_CONFIG = {
    'llm': {
        'base_url': '',
        'model': '',
        'api_key': '',
    },
    'notion': {
        'api_key': '',
        'database_id': '',
    },
}

FORBIDDEN_SQL_PATTERN = re.compile(
    r'\b(insert|update|delete|drop|alter|attach|detach|pragma|vacuum|create|replace|truncate|reindex)\b',
    re.IGNORECASE,
)
WHITESPACE_PATTERN = re.compile(r'\s+')
TRUE_VALUES = {'1', 'true', 'yes', 'y', 'on', '已投递'}
ALLOWED_PAGE_SIZES = {50, 100, 200}

RESUME_QUERY_DIRECT_TERMS = (
    '岗位库',
    '数据库',
    'sql',
    '链接',
    'url',
    'link',
    '投递链接',
    '岗位链接',
)
RESUME_QUERY_DATA_TERMS = (
    '岗位',
    '职位',
    '公司',
    '薪资',
    '工资',
    '地点',
    '城市',
    'jd',
    '标签',
    '链接',
    'url',
)
RESUME_QUERY_INTENT_TERMS = (
    '给我',
    '给出',
    '列出',
    '罗列',
    '有哪些',
    '哪些',
    '推荐',
    '筛选',
    '查询',
    '查一下',
    '展示',
    '返回',
    '输出',
    '看看',
    '看下',
    '找出',
    '发我',
    '提供',
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def today_iso() -> str:
    return datetime.now().date().isoformat()


def ensure_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[,，、\n]', value) if part.strip()]
    return [str(value).strip()]


def bool_from_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value).strip().lower() in TRUE_VALUES


def int_from_param(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def normalize_question_text(value: Any) -> str:
    return WHITESPACE_PATTERN.sub('', str(value or '').lower())


def should_force_resume_job_query(message: str) -> bool:
    compact = normalize_question_text(message)
    if not compact:
        return False
    if any(term in compact for term in RESUME_QUERY_DIRECT_TERMS):
        return True
    return any(term in compact for term in RESUME_QUERY_DATA_TERMS) and any(
        term in compact for term in RESUME_QUERY_INTENT_TERMS
    )


def enrich_resume_sql_question(message: str, hint_source: str | None = None) -> str:
    base = str(message or '').strip()
    compact = normalize_question_text(hint_source or base)
    hints: list[str] = []
    if any(term in compact for term in ('链接', 'url', 'link')):
        hints.append('请优先返回岗位链接(url)相关字段；如果没有链接字段，明确说明未查到，不要编造链接。')
    if any(term in compact for term in ('岗位', '职位', '公司')) and any(term in compact for term in RESUME_QUERY_INTENT_TERMS):
        hints.append('结果尽量包含 company、title、location、salary、url 等字段。')
    if not hints:
        return base
    return f'{base}。{" ".join(hints)}'


def parse_yaml_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ('internships', 'jobs', 'entries'):
            value = raw.get(key)
            if isinstance(value, list):
                raw = value
                break
        else:
            raw = []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


RESUME_MARKDOWN_PATH = ROOT / 'resume.md'
RESUME_ADVICE_PATH = ROOT / 'resume_advice.json'

class JobScoutStore:
    def __init__(self, root: Path):
        self.root = root
        self.db_path = DB_PATH
        self.jobs_yaml_path = JOBS_YAML_PATH
        self.prefs_yaml_path = PREFS_YAML_PATH
        self.api_config_path = API_CONFIG_PATH
        self.static_dir = STATIC_DIR
        self.lock = threading.RLock()
        self.fetch_process: subprocess.Popen[str] | None = None
        self.fetch_process_task_id = ''
        self.fetch_task = self._new_fetch_task_state()
        self.resume_sessions: dict[str, dict[str, Any]] = {}
        self.init_db()
        self.sync_from_yaml()
        self.load_cached_resume_session()

    def _new_fetch_task_state(self) -> dict[str, Any]:
        return {
            'task_id': '',
            'status': 'idle',
            'current_step': 0,
            'total_steps': 0,
            'step_label': '',
            'step_percent': 0,
            'progress_current': 0,
            'progress_total': 0,
            'progress_message': '',
            'overall_percent': 0,
            'logs': [],
            'steps': [],
            'started_at': '',
            'finished_at': '',
            'result': None,
            'error': '',
            'cancel_requested': False,
            'current_process_pid': None,
            'chrome_session': FETCH_CHROME_SESSION,
        }

    def get_fetch_status(self) -> dict[str, Any]:
        with self.lock:
            return {
                **self.fetch_task,
                'logs': list(self.fetch_task.get('logs', [])),
                'steps': [dict(step, logs=list(step.get('logs', []))) for step in self.fetch_task.get('steps', [])],
            }

    def _append_fetch_log(self, task_id: str, line: str, step_index: int | None = None) -> None:
        clean = line.rstrip()
        if not clean:
            return
        with self.lock:
            if self.fetch_task.get('task_id') != task_id:
                return
            logs = self.fetch_task['logs']
            logs.append(clean)
            if len(logs) > FETCH_LOG_LIMIT:
                del logs[:-FETCH_LOG_LIMIT]
            if step_index is not None and 0 <= step_index - 1 < len(self.fetch_task['steps']):
                step_logs = self.fetch_task['steps'][step_index - 1]['logs']
                step_logs.append(clean)
                if len(step_logs) > FETCH_LOG_LIMIT:
                    del step_logs[:-FETCH_LOG_LIMIT]

    def _update_fetch_progress(
        self,
        task_id: str,
        *,
        step_index: int | None = None,
        step_label: str | None = None,
        step_percent: int | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
        progress_message: str | None = None,
        overall_percent: int | None = None,
        status: str | None = None,
        error: str | None = None,
        result: Any = ...,
        finished: bool = False,
    ) -> None:
        with self.lock:
            if self.fetch_task.get('task_id') != task_id:
                return
            if step_index is not None:
                self.fetch_task['current_step'] = step_index
            if step_label is not None:
                self.fetch_task['step_label'] = step_label
            if step_percent is not None:
                self.fetch_task['step_percent'] = max(0, min(100, step_percent))
            if progress_current is not None:
                self.fetch_task['progress_current'] = max(0, progress_current)
            if progress_total is not None:
                self.fetch_task['progress_total'] = max(0, progress_total)
            if progress_message is not None:
                self.fetch_task['progress_message'] = progress_message
            if overall_percent is not None:
                self.fetch_task['overall_percent'] = max(0, min(100, overall_percent))
            if status is not None:
                self.fetch_task['status'] = status
            if error is not None:
                self.fetch_task['error'] = error
            if result is not ...:
                self.fetch_task['result'] = result
            if finished:
                self.fetch_task['finished_at'] = now_iso()

    def _handle_progress_line(self, task_id: str, line: str) -> bool:
        if not line.startswith('PROGRESS '):
            return False
        try:
            payload = json.loads(line[len('PROGRESS '):])
        except json.JSONDecodeError:
            return False

        with self.lock:
            if self.fetch_task.get('task_id') != task_id:
                return True
            current_step = self.fetch_task.get('current_step', 0)
            total_steps = max(1, self.fetch_task.get('total_steps', 1))
            current = int(payload.get('current') or 0)
            total = int(payload.get('total') or 0)
            step_percent = int((current / total) * 100) if total > 0 else self.fetch_task.get('step_percent', 0)
            completed_steps = max(0, current_step - 1)
            overall_percent = int(((completed_steps + (step_percent / 100)) / total_steps) * 100)
            self.fetch_task['progress_current'] = current
            self.fetch_task['progress_total'] = total
            self.fetch_task['step_percent'] = step_percent
            self.fetch_task['overall_percent'] = overall_percent
            self.fetch_task['progress_message'] = str(payload.get('message') or '').strip()
        return True

    def _terminate_fetch_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.terminate()
            except Exception:
                return

    def build_fetch_commands(
        self,
        include_jd_fetch: bool,
        include_summary: bool,
        job_limit: int,
        jd_limit: int,
        summary_limit: int,
    ) -> list[tuple[str, list[str]]]:
        commands = [
            (
                '抓取岗位列表',
                [
                    'python3',
                    'scripts/fetch_job_links.py',
                    '--prefs',
                    str(self.prefs_yaml_path),
                    '--yaml',
                    str(self.jobs_yaml_path),
                    '--limit',
                    str(max(0, job_limit)),
                ],
            )
        ]
        if include_jd_fetch:
            commands.append(
                (
                    '补抓 JD 原文',
                    [
                        'python3',
                        'scripts/fetch_jd_dom.py',
                        '--yaml',
                        str(self.jobs_yaml_path),
                        '--limit',
                        str(max(1, min(jd_limit, 50))),
                    ],
                )
            )
        if include_summary:
            commands.append(
                (
                    '生成 JD 摘要',
                    [
                        'python3',
                        'scripts/summarize_jds.py',
                        '--yaml',
                        str(self.jobs_yaml_path),
                        '--limit',
                        str(max(1, summary_limit)),
                    ],
                )
            )
        return commands

    def start_fetch_task(
        self,
        include_jd_fetch: bool,
        include_summary: bool,
        job_limit: int,
        jd_limit: int,
        summary_limit: int,
    ) -> dict[str, Any]:
        commands = self.build_fetch_commands(include_jd_fetch, include_summary, job_limit, jd_limit, summary_limit)
        if not commands:
            raise ValueError('没有可执行的抓取步骤。')

        task_id = str(uuid.uuid4())
        with self.lock:
            if self.fetch_task.get('status') in {'running', 'cancelling'}:
                raise RuntimeError('已有抓取任务正在运行，请等待当前任务结束。')
            self.fetch_task = self._new_fetch_task_state()
            self.fetch_task.update(
                {
                    'task_id': task_id,
                    'status': 'running',
                    'total_steps': len(commands),
                    'started_at': now_iso(),
                    'logs': ['准备开始抓取，BOSS 会使用独立的 Chrome 抓取窗口，不再占用当前页面。'],
                    'steps': [
                        {'label': label, 'command': ' '.join(command), 'status': 'pending', 'returncode': None, 'logs': []}
                        for label, command in commands
                    ],
                }
            )

        worker = threading.Thread(
            target=self._run_fetch_task,
            args=(task_id, commands),
            daemon=True,
        )
        worker.start()
        return self.get_fetch_status()

    def cancel_fetch_task(self) -> dict[str, Any]:
        with self.lock:
            if self.fetch_task.get('status') not in {'running', 'cancelling'}:
                raise RuntimeError('当前没有正在运行的抓取任务。')
            task_id = str(self.fetch_task.get('task_id') or '')
            if self.fetch_task.get('cancel_requested'):
                return self.get_fetch_status()

            self.fetch_task['cancel_requested'] = True
            self.fetch_task['status'] = 'cancelling'
            self.fetch_task['progress_message'] = '正在终止抓取任务…'
            process = self.fetch_process if self.fetch_process_task_id == task_id else None

        self._append_fetch_log(task_id, '已收到终止请求，正在停止当前抓取步骤。')
        if process is not None:
            self._terminate_fetch_process(process)
        return self.get_fetch_status()

    def _run_fetch_task(self, task_id: str, commands: list[tuple[str, list[str]]]) -> None:
        task_result: dict[str, Any] = {'steps': [], 'sync': None, 'ok': False}
        failure_message = ''
        cancelled = False
        environment = os.environ.copy()
        environment['PYTHONUNBUFFERED'] = '1'
        environment['ROLE_SCOUT_CHROME_SESSION'] = FETCH_CHROME_SESSION

        for step_index, (label, command) in enumerate(commands, start=1):
            with self.lock:
                if self.fetch_task.get('task_id') != task_id:
                    return
                cancelled = bool(self.fetch_task.get('cancel_requested'))
            if cancelled:
                self._append_fetch_log(task_id, '已终止抓取，后续步骤不会继续执行。')
                break

            total_steps = max(1, len(commands))
            self._update_fetch_progress(
                task_id,
                step_index=step_index,
                step_label=label,
                step_percent=0,
                progress_current=0,
                progress_total=0,
                progress_message=f'正在执行：{label}',
                overall_percent=int(((step_index - 1) / total_steps) * 100),
            )
            with self.lock:
                if self.fetch_task.get('task_id') == task_id:
                    self.fetch_task['steps'][step_index - 1]['status'] = 'running'

            self._append_fetch_log(task_id, f'[{step_index}/{total_steps}] {label}', step_index)
            process = subprocess.Popen(
                command,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
                start_new_session=True,
            )
            with self.lock:
                if self.fetch_task.get('task_id') == task_id:
                    self.fetch_process = process
                    self.fetch_process_task_id = task_id
                    self.fetch_task['current_process_pid'] = process.pid

            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip('\n')
                if self._handle_progress_line(task_id, line):
                    continue
                self._append_fetch_log(task_id, line, step_index)

            returncode = process.wait()
            with self.lock:
                if self.fetch_process_task_id == task_id and self.fetch_process is process:
                    self.fetch_process = None
                    self.fetch_process_task_id = ''
                    if self.fetch_task.get('task_id') == task_id:
                        self.fetch_task['current_process_pid'] = None
                cancel_requested = self.fetch_task.get('task_id') == task_id and bool(self.fetch_task.get('cancel_requested'))

            step_result = {
                'label': label,
                'command': ' '.join(command),
                'returncode': returncode,
            }
            task_result['steps'].append(step_result)
            with self.lock:
                if self.fetch_task.get('task_id') == task_id:
                    if cancel_requested and returncode != 0:
                        step_status = 'cancelled'
                    else:
                        step_status = 'completed' if returncode == 0 else 'failed'
                    self.fetch_task['steps'][step_index - 1]['status'] = step_status
                    self.fetch_task['steps'][step_index - 1]['returncode'] = returncode

            if returncode != 0:
                if cancel_requested:
                    cancelled = True
                    self._append_fetch_log(task_id, f'{label} 已终止。', step_index)
                    self._update_fetch_progress(
                        task_id,
                        status='cancelling',
                        progress_message='正在终止抓取任务…',
                    )
                    break
                failure_message = f'{label} 执行失败，返回码 {returncode}'
                self._append_fetch_log(task_id, failure_message, step_index)
                self._update_fetch_progress(
                    task_id,
                    step_percent=100,
                    overall_percent=int((step_index / total_steps) * 100),
                    progress_message=failure_message,
                )
                break

            self._update_fetch_progress(
                task_id,
                step_percent=100,
                progress_current=1,
                progress_total=1,
                overall_percent=int((step_index / total_steps) * 100),
                progress_message=f'{label} 完成',
            )
            if cancel_requested:
                cancelled = True
                self._append_fetch_log(task_id, '已终止抓取，后续步骤不会继续执行。')
                break

        try:
            sync = self.sync_from_yaml()
        except Exception as exc:  # noqa: BLE001
            sync = None
            failure_message = failure_message or f'同步 YAML/SQLite 失败: {exc}'
            self._append_fetch_log(task_id, failure_message)

        task_result['sync'] = sync
        task_result['ok'] = not failure_message and not cancelled
        task_result['cancelled'] = cancelled
        final_status = 'cancelled' if cancelled else ('completed' if not failure_message else 'failed')
        final_message = '抓取已终止。' if cancelled else ('抓取已完成。' if not failure_message else failure_message)
        final_kwargs: dict[str, Any] = {
            'status': final_status,
            'progress_message': final_message,
            'result': task_result,
            'error': '' if cancelled else failure_message,
            'finished': True,
        }
        if not cancelled:
            final_kwargs['step_percent'] = 100
            final_kwargs['overall_percent'] = 100
        self._update_fetch_progress(task_id, **final_kwargs)

    def delete_job(self, job_id: int) -> dict[str, Any]:
        with self.lock:
            with self.connect() as connection:
                current = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
                if current is None:
                    raise KeyError(f'job {job_id} not found')
                connection.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
                connection.commit()
                snapshot = {'id': current['id'], 'company': current['company'], 'title': current['title']}
            self.export_to_yaml()
            return snapshot

    def delete_jobs(self, job_ids: list[int]) -> dict[str, Any]:
        normalized_ids = []
        seen_ids = set()
        for value in job_ids:
            try:
                job_id = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError('批量删除的岗位 id 必须是整数。') from exc
            if job_id <= 0 or job_id in seen_ids:
                continue
            normalized_ids.append(job_id)
            seen_ids.add(job_id)

        if not normalized_ids:
            raise ValueError('请先选择要删除的岗位。')

        placeholders = ', '.join('?' for _ in normalized_ids)
        with self.lock:
            with self.connect() as connection:
                rows = connection.execute(
                    f'SELECT id, company, title FROM jobs WHERE id IN ({placeholders}) ORDER BY id ASC',
                    normalized_ids,
                ).fetchall()
                if not rows:
                    raise KeyError('所选岗位不存在。')
                connection.execute(
                    f'DELETE FROM jobs WHERE id IN ({placeholders})',
                    normalized_ids,
                )
                connection.commit()
                deleted_jobs = [
                    {'id': row['id'], 'company': row['company'], 'title': row['title']}
                    for row in rows
                ]
            self.export_to_yaml()
            return {
                'deleted': len(deleted_jobs),
                'jobs': deleted_jobs,
            }

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                '''
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    salary TEXT DEFAULT '',
                    location TEXT DEFAULT '',
                    company_size TEXT DEFAULT '',
                    funding_stage TEXT DEFAULT '',
                    job_type TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    applied INTEGER DEFAULT 0,
                    jd_full TEXT DEFAULT '',
                    jd_summary TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    jd_quality TEXT DEFAULT '',
                    jd_score INTEGER,
                    notion_page_id TEXT DEFAULT '',
                    fetch_error TEXT DEFAULT '',
                    collected_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(company, title)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_location ON jobs(location);
                CREATE INDEX IF NOT EXISTS idx_jobs_collected_at ON jobs(collected_at);
                '''
            )

    def read_jobs_yaml(self) -> tuple[Any, list[dict[str, Any]]]:
        if not self.jobs_yaml_path.exists():
            return [], []
        raw = yaml.safe_load(self.jobs_yaml_path.read_text(encoding='utf-8'))
        return raw, parse_yaml_entries(raw)

    def write_jobs_yaml(self, entries: list[dict[str, Any]]) -> None:
        existing_raw = []
        if self.jobs_yaml_path.exists():
            existing_raw = yaml.safe_load(self.jobs_yaml_path.read_text(encoding='utf-8')) or []

        if isinstance(existing_raw, dict):
            payload = dict(existing_raw)
            payload['internships'] = entries
        else:
            payload = entries

        self.jobs_yaml_path.write_text(
            yaml.dump(payload, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding='utf-8',
        )

    def read_prefs(self) -> dict[str, Any]:
        if not self.prefs_yaml_path.exists():
            return self.normalize_prefs(DEFAULT_PREFS)
        raw = yaml.safe_load(self.prefs_yaml_path.read_text(encoding='utf-8')) or {}
        if not isinstance(raw, dict):
            raw = {}
        return self.normalize_prefs(raw)

    def write_prefs(self, payload: dict[str, Any]) -> dict[str, Any]:
        prefs = self.normalize_prefs(payload)
        existing_raw = {}
        if self.prefs_yaml_path.exists():
            loaded = yaml.safe_load(self.prefs_yaml_path.read_text(encoding='utf-8')) or {}
            if isinstance(loaded, dict):
                existing_raw = loaded

        payload_to_write = dict(prefs)
        legacy_notion_db_id = str(existing_raw.get('notion_db_id') or '').strip()
        if legacy_notion_db_id:
            payload_to_write['notion_db_id'] = legacy_notion_db_id

        self.prefs_yaml_path.write_text(
            yaml.dump(payload_to_write, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding='utf-8',
        )
        return prefs

    def normalize_api_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        llm_raw = data.get('llm') or {}
        notion_raw = data.get('notion') or {}

        legacy_prefs = {}
        if self.prefs_yaml_path.exists():
            loaded = yaml.safe_load(self.prefs_yaml_path.read_text(encoding='utf-8')) or {}
            if isinstance(loaded, dict):
                legacy_prefs = loaded

        return {
            'llm': {
                'base_url': str(llm_raw.get('base_url') or DEFAULT_API_CONFIG['llm']['base_url']).strip(),
                'model': str(llm_raw.get('model') or DEFAULT_API_CONFIG['llm']['model']).strip(),
                'api_key': str(llm_raw.get('api_key') or DEFAULT_API_CONFIG['llm']['api_key']).strip(),
            },
            'notion': {
                'api_key': str(notion_raw.get('api_key') or DEFAULT_API_CONFIG['notion']['api_key']).strip(),
                'database_id': str(
                    notion_raw.get('database_id')
                    or notion_raw.get('db_id')
                    or legacy_prefs.get('notion_db_id')
                    or DEFAULT_API_CONFIG['notion']['database_id']
                ).strip(),
            },
        }

    def read_api_config(self) -> dict[str, Any]:
        if not self.api_config_path.exists():
            return self.normalize_api_config(DEFAULT_API_CONFIG)
        raw = yaml.safe_load(self.api_config_path.read_text(encoding='utf-8')) or {}
        if not isinstance(raw, dict):
            raw = {}
        return self.normalize_api_config(raw)

    def write_api_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.normalize_api_config(payload)
        self.api_config_path.write_text(
            yaml.dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding='utf-8',
        )
        return config

    def normalize_prefs(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = dict(DEFAULT_PREFS)
        data.update(payload or {})
        salary_raw = data.get('salary') or {}
        filters_raw = data.get('filters') or {}
        profile_raw = data.get('profile') or {}

        return {
            'cities': ensure_list(data.get('cities')) or DEFAULT_PREFS['cities'],
            'job_type': str(data.get('job_type') or DEFAULT_PREFS['job_type']).strip(),
            'salary': {
                'min_day': int(salary_raw.get('min_day', DEFAULT_PREFS['salary']['min_day']) or DEFAULT_PREFS['salary']['min_day']),
                'min_month': int(salary_raw.get('min_month', DEFAULT_PREFS['salary']['min_month']) or DEFAULT_PREFS['salary']['min_month']),
            },
            'scales': ensure_list(data.get('scales')) or DEFAULT_PREFS['scales'],
            'queries': ensure_list(data.get('queries')) or DEFAULT_PREFS['queries'],
            'filters': {
                'exclude_big_tech': ensure_list(filters_raw.get('exclude_big_tech')),
                'exclude_non_tech': ensure_list(filters_raw.get('exclude_non_tech')),
                'exclude_extra': ensure_list(filters_raw.get('exclude_extra')),
            },
            'preferred_tags': ensure_list(data.get('preferred_tags')),
            'profile': {
                'education': str(profile_raw.get('education') or '').strip(),
                'internship_duration': str(profile_raw.get('internship_duration') or '').strip(),
                'note': str(profile_raw.get('note') or '').strip(),
            },
        }

    def normalise_job(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        company = str(entry.get('company') or '').strip()
        title = str(entry.get('title') or '').strip()
        if not company or not title:
            return None

        status = str(entry.get('status') or 'pending').strip() or 'pending'
        explicit_applied = entry.get('applied')
        applied = bool_from_value(explicit_applied, default=status != 'pending')
        if applied and status == 'pending':
            status = 'applied'
        if not applied and status == 'applied':
            status = 'pending'

        tags = ensure_list(entry.get('tags'))
        collected_at = str(entry.get('collected_at') or '').strip() or today_iso()
        timestamp = now_iso()

        return {
            'company': company,
            'title': title,
            'salary': str(entry.get('salary') or '').strip(),
            'location': str(entry.get('location') or '').strip(),
            'company_size': str(entry.get('company_size') or '').strip(),
            'funding_stage': str(entry.get('funding_stage') or '').strip(),
            'job_type': str(entry.get('job_type') or '').strip(),
            'source': str(entry.get('source') or '').strip(),
            'url': str(entry.get('url') or '').strip(),
            'status': status,
            'applied': 1 if applied else 0,
            'jd_full': str(entry.get('jd_full') or '').strip(),
            'jd_summary': str(entry.get('jd_summary') or '').strip(),
            'tags': json.dumps(tags, ensure_ascii=False),
            'jd_quality': str(entry.get('jd_quality') or '').strip(),
            'jd_score': entry.get('jd_score') if entry.get('jd_score') not in ('', None) else None,
            'notion_page_id': str(entry.get('notion_page_id') or '').strip(),
            'fetch_error': str(entry.get('fetch_error') or '').strip(),
            'collected_at': collected_at,
            'created_at': timestamp,
            'updated_at': timestamp,
        }

    def sync_from_yaml(self) -> dict[str, int]:
        with self.lock:
            return self._sync_from_yaml_locked()

    def _sync_from_yaml_locked(self) -> dict[str, int]:
        raw, entries = self.read_jobs_yaml()
        normalised = []
        for entry in entries:
            job = self.normalise_job(entry)
            if job:
                normalised.append(job)

        with self.connect() as connection:
            existing_keys = {
                (row['company'], row['title'])
                for row in connection.execute('SELECT company, title FROM jobs').fetchall()
            }
            yaml_keys = {(job['company'], job['title']) for job in normalised}
            deleted = 0
            for company, title in existing_keys - yaml_keys:
                connection.execute('DELETE FROM jobs WHERE company = ? AND title = ?', (company, title))
                deleted += 1

            inserted = 0
            updated = 0
            for job in normalised:
                exists = connection.execute(
                    'SELECT id, created_at FROM jobs WHERE company = ? AND title = ?',
                    (job['company'], job['title']),
                ).fetchone()
                if exists:
                    job['created_at'] = exists['created_at']
                    updated += 1
                else:
                    inserted += 1
                connection.execute(
                    '''
                    INSERT INTO jobs (
                        company, title, salary, location, company_size, funding_stage, job_type,
                        source, url, status, applied, jd_full, jd_summary, tags, jd_quality,
                        jd_score, notion_page_id, fetch_error, collected_at, created_at, updated_at
                    ) VALUES (
                        :company, :title, :salary, :location, :company_size, :funding_stage, :job_type,
                        :source, :url, :status, :applied, :jd_full, :jd_summary, :tags, :jd_quality,
                        :jd_score, :notion_page_id, :fetch_error, :collected_at, :created_at, :updated_at
                    )
                    ON CONFLICT(company, title) DO UPDATE SET
                        salary = excluded.salary,
                        location = excluded.location,
                        company_size = excluded.company_size,
                        funding_stage = excluded.funding_stage,
                        job_type = excluded.job_type,
                        source = excluded.source,
                        url = excluded.url,
                        status = excluded.status,
                        applied = excluded.applied,
                        jd_full = excluded.jd_full,
                        jd_summary = excluded.jd_summary,
                        tags = excluded.tags,
                        jd_quality = excluded.jd_quality,
                        jd_score = excluded.jd_score,
                        notion_page_id = excluded.notion_page_id,
                        fetch_error = excluded.fetch_error,
                        collected_at = excluded.collected_at,
                        updated_at = excluded.updated_at
                    ''',
                    job,
                )

            return {
                'yaml_count': len(entries),
                'db_count': connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],
                'inserted': inserted,
                'updated': updated,
                'deleted': deleted,
                'is_dict_yaml': 1 if isinstance(raw, dict) else 0,
            }

    def row_to_job(self, row: sqlite3.Row) -> dict[str, Any]:
        tags_raw = row['tags'] or '[]'
        try:
            tags = json.loads(tags_raw)
        except json.JSONDecodeError:
            tags = []

        return {
            'id': row['id'],
            'company': row['company'],
            'title': row['title'],
            'salary': row['salary'],
            'location': row['location'],
            'company_size': row['company_size'],
            'funding_stage': row['funding_stage'],
            'job_type': row['job_type'],
            'source': row['source'],
            'url': row['url'],
            'status': row['status'],
            'applied': bool(row['applied']),
            'jd_full': row['jd_full'],
            'jd_summary': row['jd_summary'],
            'tags': tags if isinstance(tags, list) else [],
            'jd_quality': row['jd_quality'],
            'jd_score': row['jd_score'],
            'notion_page_id': row['notion_page_id'],
            'fetch_error': row['fetch_error'],
            'collected_at': row['collected_at'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
        }

    def export_to_yaml(self) -> int:
        with self.lock:
            with self.connect() as connection:
                rows = connection.execute('SELECT * FROM jobs ORDER BY id ASC').fetchall()
                entries = []
                for row in rows:
                    job = self.row_to_job(row)
                    entries.append(
                        {
                            'collected_at': job['collected_at'],
                            'company': job['company'],
                            'title': job['title'],
                            'salary': job['salary'],
                            'location': job['location'],
                            'company_size': job['company_size'],
                            'funding_stage': job['funding_stage'],
                            'job_type': job['job_type'],
                            'source': job['source'],
                            'url': job['url'],
                            'status': job['status'],
                            'applied': job['applied'],
                            'jd_full': job['jd_full'],
                            'jd_summary': job['jd_summary'],
                            'tags': job['tags'],
                            'jd_quality': job['jd_quality'],
                            'jd_score': job['jd_score'],
                            'fetch_error': job['fetch_error'],
                            'notion_page_id': job['notion_page_id'],
                        }
                    )
                self.write_jobs_yaml(entries)
                return len(entries)

    def list_jobs(self, params: dict[str, list[str]]) -> dict[str, Any]:
        where = []
        values: list[Any] = []
        page = int_from_param((params.get('page') or ['1'])[0], 1)
        page_size = int_from_param((params.get('page_size') or ['50'])[0], 50)
        if page_size not in ALLOWED_PAGE_SIZES:
            page_size = 50

        keyword = (params.get('q') or [''])[0].strip()
        if keyword:
            where.append(
                '('
                'company LIKE ? OR title LIKE ? OR location LIKE ? OR salary LIKE ? '
                'OR jd_summary LIKE ? OR jd_full LIKE ? OR tags LIKE ?'
                ')'
            )
            values.extend([f'%{keyword}%'] * 7)

        status = (params.get('status') or [''])[0].strip()
        if status:
            where.append('status = ?')
            values.append(status)

        applied = (params.get('applied') or [''])[0].strip().lower()
        if applied in {'true', 'false'}:
            where.append('applied = ?')
            values.append(1 if applied == 'true' else 0)

        location = (params.get('location') or [''])[0].strip()
        if location:
            where.append('location LIKE ?')
            values.append(f'%{location}%')

        where_clause = ''
        if where:
            where_clause = ' WHERE ' + ' AND '.join(where)

        count_query = 'SELECT COUNT(*) FROM jobs' + where_clause
        total_pages = 1
        offset = 0

        query = 'SELECT * FROM jobs' + where_clause
        query += ' ORDER BY collected_at DESC, id DESC LIMIT ? OFFSET ?'

        with self.connect() as connection:
            filtered_total = connection.execute(count_query, values).fetchone()[0]
            if filtered_total > 0:
                total_pages = max(1, (filtered_total + page_size - 1) // page_size)
                page = min(page, total_pages)
                offset = (page - 1) * page_size
            else:
                page = 1
                total_pages = 1
                offset = 0

            rows = connection.execute(query, [*values, page_size, offset]).fetchall()
            summary = connection.execute(
                '''
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(applied), 0) AS applied_count,
                    COALESCE(SUM(CASE WHEN jd_quality = 'A' THEN 1 ELSE 0 END), 0) AS quality_a,
                    COALESCE(SUM(CASE WHEN jd_quality = 'B' THEN 1 ELSE 0 END), 0) AS quality_b,
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count
                FROM jobs
                '''
            ).fetchone()

        return {
            'items': [self.row_to_job(row) for row in rows],
            'summary': {
                'total': summary['total'],
                'applied': summary['applied_count'],
                'qualityA': summary['quality_a'],
                'qualityB': summary['quality_b'],
                'pending': summary['pending_count'],
            },
            'pagination': {
                'page': page,
                'pageSize': page_size,
                'totalItems': filtered_total,
                'totalPages': total_pages,
                'hasPrev': page > 1,
                'hasNext': page < total_pages,
            },
        }

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        entry = dict(payload or {})
        entry.setdefault('source', '手动添加')
        entry.setdefault('status', 'pending')
        entry.setdefault('applied', False)

        job = self.normalise_job(entry)
        if job is None:
            raise ValueError('公司名称和职位名称不能为空。')

        with self.lock:
            with self.connect() as connection:
                current = connection.execute(
                    'SELECT id FROM jobs WHERE company = ? AND title = ?',
                    (job['company'], job['title']),
                ).fetchone()
                if current is not None:
                    raise ValueError('该岗位已存在，请直接修改现有记录。')

                connection.execute(
                    '''
                    INSERT INTO jobs (
                        company, title, salary, location, company_size, funding_stage, job_type,
                        source, url, status, applied, jd_full, jd_summary, tags, jd_quality,
                        jd_score, notion_page_id, fetch_error, collected_at, created_at, updated_at
                    ) VALUES (
                        :company, :title, :salary, :location, :company_size, :funding_stage, :job_type,
                        :source, :url, :status, :applied, :jd_full, :jd_summary, :tags, :jd_quality,
                        :jd_score, :notion_page_id, :fetch_error, :collected_at, :created_at, :updated_at
                    )
                    ''',
                    job,
                )
                row = connection.execute('SELECT * FROM jobs WHERE id = last_insert_rowid()').fetchone()
                connection.commit()

            self.export_to_yaml()
            return self.row_to_job(row)

    def update_job(self, job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            with self.connect() as connection:
                current = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
                if current is None:
                    raise KeyError(f'job {job_id} not found')

                status = str(payload.get('status', current['status']) or current['status']).strip() or 'pending'
                applied = bool_from_value(payload.get('applied'), default=bool(current['applied']))
                if applied and status == 'pending':
                    status = 'applied'
                if not applied and status == 'applied':
                    status = 'pending'

                connection.execute(
                    'UPDATE jobs SET status = ?, applied = ?, updated_at = ? WHERE id = ?',
                    (status, 1 if applied else 0, now_iso(), job_id),
                )
                row = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
                connection.commit()
                self.export_to_yaml()
                return self.row_to_job(row)

    def call_model_stream(self, config: dict[str, str], messages: list[dict[str, str]], temperature: float = 0.2):
        base_url = str(config.get('base_url') or '').strip().rstrip('/')
        api_key = str(config.get('api_key') or '').strip()
        model = str(config.get('model') or '').strip()
        if not base_url or not api_key or not model:
            raise ValueError('缺少模型配置，请填写 Base URL、Model 和 API Key。')

        if not base_url.endswith('/chat/completions'):
            if base_url.endswith('/v1'):
                endpoint = f'{base_url}/chat/completions'
            else:
                endpoint = f'{base_url}/v1/chat/completions'
        else:
            endpoint = base_url

        payload = {
            'model': model,
            'temperature': temperature,
            'messages': messages,
            'stream': True,
        }
        request = Request(
            endpoint,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            },
            method='POST',
        )

        try:
            with urlopen(request, timeout=60) as response:
                for line in response:
                    line = line.decode('utf-8').strip()
                    if line.startswith('data: ') and line != 'data: [DONE]':
                        try:
                            data = json.loads(line[6:])
                            delta = data['choices'][0].get('delta', {})
                            if 'content' in delta:
                                yield delta['content']
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            raise RuntimeError(f'模型流式调用失败: HTTP {exc.code} {detail}') from exc
        except URLError as exc:
            raise RuntimeError(f'模型流式调用失败: {exc.reason}') from exc

    def call_model(self, config: dict[str, str], messages: list[dict[str, str]], temperature: float = 0.1) -> str:
        base_url = str(config.get('base_url') or '').strip().rstrip('/')
        api_key = str(config.get('api_key') or '').strip()
        model = str(config.get('model') or '').strip()
        if not base_url or not api_key or not model:
            raise ValueError('缺少模型配置，请填写 Base URL、Model 和 API Key。')

        if not base_url.endswith('/chat/completions'):
            if base_url.endswith('/v1'):
                endpoint = f'{base_url}/chat/completions'
            else:
                endpoint = f'{base_url}/v1/chat/completions'
        else:
            endpoint = base_url

        payload = {
            'model': model,
            'temperature': temperature,
            'messages': messages,
        }
        request = Request(
            endpoint,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            },
            method='POST',
        )

        try:
            with urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            raise RuntimeError(f'模型调用失败: HTTP {exc.code} {detail}') from exc
        except URLError as exc:
            raise RuntimeError(f'模型调用失败: {exc.reason}') from exc

        choices = body.get('choices') or []
        if not choices:
            raise RuntimeError(f'模型返回缺少 choices: {body}')

        content = choices[0].get('message', {}).get('content', '')
        if isinstance(content, list):
            fragments = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    fragments.append(block.get('text', ''))
            content = ''.join(fragments)

        return str(content).strip()

    def extract_json_object(self, text: str) -> dict[str, Any]:
        start = text.find('{')
        end = text.rfind('}')
        if start == -1 or end == -1:
            raise ValueError('模型未返回 JSON 对象。')
        return json.loads(text[start:end + 1])

    def validate_sql(self, sql: str) -> str:
        cleaned = sql.strip().rstrip(';').strip()
        if not cleaned:
            raise ValueError('模型未生成 SQL。')
        if '--' in cleaned or '/*' in cleaned:
            raise ValueError('SQL 中不允许注释。')
        if ';' in cleaned:
            raise ValueError('只允许单条 SQL。')
        if FORBIDDEN_SQL_PATTERN.search(cleaned):
            raise ValueError('只允许只读 SELECT/CTE 查询。')

        compact = WHITESPACE_PATTERN.sub(' ', cleaned).lower()
        if not (compact.startswith('select ') or compact.startswith('with ')):
            raise ValueError('只允许 SELECT 或 WITH 查询。')
        return cleaned

    def execute_analysis_sql(self, sql: str) -> tuple[list[str], list[list[Any]]]:
        safe_sql = self.validate_sql(sql)
        with self.connect() as connection:
            cursor = connection.execute(safe_sql)
            rows = cursor.fetchmany(200)
            columns = [desc[0] for desc in cursor.description or []]
        data_rows = [[row[col] for col in columns] for row in rows]
        return columns, data_rows

    def format_analysis_sample_value(self, value: Any, limit: int = 48) -> str:
        text = str(value or '').strip()
        if not text:
            return ''

        if text.startswith('['):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                text = ', '.join(str(item).strip() for item in parsed if str(item).strip())

        text = WHITESPACE_PATTERN.sub(' ', text.replace('\r', ' ').replace('\n', ' ')).strip()
        if len(text) > limit:
            text = f'{text[:limit - 3]}...'
        return text

    def get_analysis_schema_examples(
        self,
        connection: sqlite3.Connection,
        column_name: str,
        limit: int = 3,
    ) -> list[str]:
        quoted_column = column_name.replace('"', '""')
        rows = connection.execute(
            f'''
            SELECT value
            FROM (
                SELECT CAST("{quoted_column}" AS TEXT) AS value, MAX(id) AS latest_id
                FROM jobs
                WHERE "{quoted_column}" IS NOT NULL
                  AND TRIM(CAST("{quoted_column}" AS TEXT)) != ''
                GROUP BY CAST("{quoted_column}" AS TEXT)
            )
            ORDER BY latest_id DESC
            LIMIT ?
            ''',
            (limit,),
        ).fetchall()

        examples = []
        for row in rows:
            sample = self.format_analysis_sample_value(row['value'])
            if sample:
                examples.append(sample)
        return examples

    def build_analysis_schema(self) -> str:
        lines = ['表 jobs 的字段：']
        with self.connect() as connection:
            columns = connection.execute('PRAGMA table_info(jobs)').fetchall()

            for column in columns:
                column_name = str(column['name'] or '').strip()
                column_type = str(column['type'] or 'TEXT').strip() or 'TEXT'
                examples = self.get_analysis_schema_examples(connection, column_name)
                example_text = ' / '.join(examples) if examples else '无'
                lines.append(f'- {column_name} {column_type}，示例值：{example_text}')

        lines.extend(
            [
                '',
                '规则：',
                '- 只能生成 SQLite 的只读 SQL。',
                '- tags 是 JSON 字符串数组，优先用 LIKE 匹配，不要依赖 json_each。',
                '- 如果做明细查询，必须 LIMIT 200 以内。',
                '- 如果问题与 JD 要求相关，优先统计 jd_full / jd_summary 中的关键词。',
                '- 输出必须是 JSON 对象：{"sql": "...", "title": "...", "assumption": "..."}',
            ]
        )
        return '\n'.join(lines)

    def run_analysis(self, question: str, llm_config: dict[str, str] | None = None) -> dict[str, Any]:
        prefs = self.read_prefs()
        resolved_llm_config = self.resolve_llm_config(llm_config)
        schema = self.build_analysis_schema()

        planner_text = self.call_model(
            resolved_llm_config,
            [
                {
                    'role': 'system',
                    'content': '你是一个 SQLite NL2SQL 规划器，只输出 JSON，不要输出解释。',
                },
                {
                    'role': 'user',
                    'content': f'{schema}\n\n当前用户偏好：{json.dumps(prefs, ensure_ascii=False)}\n\n用户问题：{question}',
                },
            ],
            temperature=0.0,
        )
        planner = self.extract_json_object(planner_text)
        sql = self.validate_sql(str(planner.get('sql') or ''))
        columns, rows = self.execute_analysis_sql(sql)

        summary_rows = rows[:50]
        summary_text = self.call_model(
            resolved_llm_config,
            [
                {
                    'role': 'system',
                    'content': '你是一个招聘数据分析助手。请基于 SQL 查询结果给出简洁、可执行的中文洞察，不要编造结果中不存在的信息。',
                },
                {
                    'role': 'user',
                    'content': (
                        f'用户问题：{question}\n'
                        f'生成 SQL：{sql}\n'
                        f'列名：{json.dumps(columns, ensure_ascii=False)}\n'
                        f'结果样本：{json.dumps(summary_rows, ensure_ascii=False)}\n'
                        '请输出 3-5 句中文分析，总结主要要求、出现频率或明显缺口。'
                    ),
                },
            ],
            temperature=0.2,
        )

        return {
            'title': str(planner.get('title') or '分析结果').strip() or '分析结果',
            'assumption': str(planner.get('assumption') or '').strip(),
            'sql': sql,
            'columns': columns,
            'rows': rows,
            'insight': summary_text.strip(),
        }

    def resolve_llm_config(self, llm_config: dict[str, str] | None = None) -> dict[str, str]:
        saved_llm_config = self.read_api_config().get('llm', {})
        return {
            **saved_llm_config,
            **{
                key: str(value).strip()
                for key, value in (llm_config or {}).items()
                if str(value or '').strip()
            },
        }

    def truncate_text(self, text: str, limit: int, *, head: int = 9000, tail: int = 2500) -> str:
        content = str(text or '')
        if limit <= 0 or len(content) <= limit:
            return content
        head = max(0, min(head, limit))
        tail = max(0, min(tail, limit - head))
        if head + tail >= limit:
            return content[:limit]
        return f'{content[:head]}\n\n[...内容过长，已截断...]\n\n{content[-tail:]}'

    def execute_readonly_sql(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        safe_sql = self.validate_sql(sql)
        with self.connect() as connection:
            cursor = connection.execute(safe_sql, params)
            return cursor.fetchmany(200)

    def match_resume_to_jobs(self, profile: dict[str, Any], llm_config: dict[str, str] | None = None) -> list[dict[str, Any]]:
        # Let the LLM write a SQL query to match jobs based on the parsed resume profile
        question = (
            f"根据候选人简历要求匹配合适的岗位，候选人期望职位：{', '.join(profile.get('target_roles', []))}，"
            f"掌握技能：{', '.join(profile.get('skills', []))}，"
            f"经验关键词：{', '.join(profile.get('keywords', []))}。"
            "请编写 SQL 查找匹配度最高的前 20 个岗位。你可以使用 LIKE 匹配 title, jd_summary, tags。"
            "查询结果请包含: id, company, title, salary, location, jd_summary, tags, match_score(可以根据匹配度自行打分或简单赋值1)"
        )
        try:
            analysis_result = self.run_analysis(question, llm_config)
            rows = analysis_result.get('rows', [])
            columns = analysis_result.get('columns', [])
            
            result = []
            for row_data in rows:
                row_dict = dict(zip(columns, row_data))
                # Add default match_score if not present
                if 'match_score' not in row_dict:
                    row_dict['match_score'] = 1
                result.append(row_dict)
            return result[:30]
        except Exception as exc:
            # Fallback to simple matching if NL2SQL fails
            keywords = ensure_list(profile.get('keywords')) + ensure_list(profile.get('skills')) + ensure_list(profile.get('target_roles'))
            return self._fallback_match_resume_to_jobs(keywords, limit=30)

    def _fallback_match_resume_to_jobs(self, keywords: list[str], limit: int = 30) -> list[dict[str, Any]]:
        cleaned = []
        for raw in keywords:
            word = str(raw or '').strip()
            if not word:
                continue
            if len(word) > 40:
                continue
            cleaned.append(word)
            if len(cleaned) >= 10:
                break

        if not cleaned:
            return []

        conditions = []
        values: list[Any] = []
        for word in cleaned:
            like = f'%{word}%'
            conditions.append('(title LIKE ? OR company LIKE ? OR jd_summary LIKE ? OR jd_full LIKE ? OR tags LIKE ?)')
            values.extend([like, like, like, like, like])

        sql = (
            'SELECT * FROM jobs '
            f'WHERE {" OR ".join(conditions)} '
            'ORDER BY COALESCE(jd_score, 0) DESC, id DESC '
            'LIMIT 200'
        )
        rows = self.execute_readonly_sql(sql, tuple(values))
        scored = []
        for row in rows:
            text = ' '.join(
                [
                    str(row['company'] or ''),
                    str(row['title'] or ''),
                    str(row['location'] or ''),
                    str(row['salary'] or ''),
                    str(row['jd_summary'] or ''),
                    str(row['tags'] or ''),
                ]
            ).lower()
            score = sum(1 for word in cleaned if word.lower() in text)
            scored.append((score, row))

        scored.sort(key=lambda item: (item[0], item[1]['jd_score'] or 0, item[1]['id']), reverse=True)
        result = []
        for score, row in scored[: max(1, limit)]:
            job = self.row_to_job(row) if isinstance(row, sqlite3.Row) else {}
            job['match_score'] = score
            job['jd_summary'] = str(row['jd_summary'] or '')
            result.append(job)
        return result

    def analyse_resume_markdown(self, markdown: str, llm_config: dict[str, str] | None = None) -> dict[str, Any]:
        resolved_llm_config = self.resolve_llm_config(llm_config)
        resume_text = self.truncate_text(markdown, 16_000)
        raw = self.call_model(
            resolved_llm_config,
            [
                {
                    'role': 'system',
                    'content': '你是简历解析器，只输出 JSON，不要输出解释。输出字段必须稳定且可被程序解析。',
                },
                {
                    'role': 'user',
                    'content': (
                        '请从下面的简历 Markdown 中抽取关键信息，输出 JSON：\n'
                        '{\n'
                        '  "target_roles": ["..."],\n'
                        '  "seniority": "实习/校招/初级/中级/高级/不确定",\n'
                        '  "education": ["..."],\n'
                        '  "experience_summary": ["..."],\n'
                        '  "projects_summary": ["..."],\n'
                        '  "skills": ["..."],\n'
                        '  "tools": ["..."],\n'
                        '  "domains": ["..."],\n'
                        '  "highlights": ["..."],\n'
                        '  "risks": ["..."],\n'
                        '  "keywords": ["..."]\n'
                        '}\n\n'
                        f'简历 Markdown：\n{resume_text}'
                    ),
                },
            ],
            temperature=0.0,
        )
        parsed = self.extract_json_object(raw)
        for key in ('target_roles', 'education', 'experience_summary', 'projects_summary', 'skills', 'tools', 'domains', 'highlights', 'risks', 'keywords'):
            value = parsed.get(key)
            if isinstance(value, list):
                parsed[key] = [str(item).strip() for item in value if str(item).strip()]
            elif value is None:
                parsed[key] = []
            else:
                parsed[key] = [str(value).strip()] if str(value).strip() else []
        parsed['seniority'] = str(parsed.get('seniority') or '').strip() or '不确定'
        return parsed

    def build_resume_advice(
        self,
        markdown: str,
        profile: dict[str, Any],
        matched_jobs: list[dict[str, Any]],
        llm_config: dict[str, str] | None = None,
        question: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        resolved_llm_config = self.resolve_llm_config(llm_config)
        resume_text = self.truncate_text(markdown, 14_000)
        jobs_sample = [
            {
                'company': job.get('company'),
                'title': job.get('title'),
                'location': job.get('location'),
                'salary': job.get('salary'),
                'tags': job.get('tags'),
                'jd_summary': self.truncate_text(job.get('jd_summary') or '', 600),
                'match_score': job.get('match_score', 0),
            }
            for job in (matched_jobs or [])[:10]
        ]
        messages: list[dict[str, str]] = [
            {
                'role': 'system',
                'content': (
                    '你是“简历优化 + 岗位匹配”助手。'
                    '只基于给定简历与岗位库样本给建议，不要编造不存在的经历。'
                    '输出必须是中文，结构清晰，可执行。'
                ),
            },
            {
                'role': 'user',
                'content': (
                    f'简历结构化信息：{json.dumps(profile, ensure_ascii=False)}\n\n'
                    f'岗位库匹配样本（Top 10）：{json.dumps(jobs_sample, ensure_ascii=False)}\n\n'
                    f'简历 Markdown（截断）：\n{resume_text}\n\n'
                    f'用户问题：{(question or "请给出简历改进建议，并指出与岗位库匹配度最高/最低的点").strip()}'
                ),
            },
        ]
        if history:
            messages.extend(history[-RESUME_CHAT_HISTORY_LIMIT :])
        return self.call_model(resolved_llm_config, messages, temperature=0.2).strip()

    def create_resume_session(
        self,
        *,
        resume_filename: str,
        uploaded_path: str,
        markdown_path: str,
        markdown: str,
        profile: dict[str, Any],
        matched_jobs: list[dict[str, Any]],
        initial_advice: str,
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        session = {
            'session_id': session_id,
            'resume_filename': resume_filename,
            'uploaded_path': uploaded_path,
            'markdown_path': markdown_path,
            'markdown': markdown,
            'profile': profile,
            'matched_jobs': matched_jobs,
            'history': [{'role': 'assistant', 'content': initial_advice}] if initial_advice else [],
            'created_at': now_iso(),
            'updated_at': now_iso(),
        }
        with self.lock:
            self.resume_sessions[session_id] = session
            if len(self.resume_sessions) > 20:
                oldest = sorted(self.resume_sessions.values(), key=lambda item: item.get('created_at') or '')[:5]
                for item in oldest:
                    self.resume_sessions.pop(str(item.get('session_id') or ''), None)
        
        # Save to local file cache
        try:
            RESUME_MARKDOWN_PATH.write_text(markdown, encoding='utf-8')
            RESUME_ADVICE_PATH.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as exc:
            print(f"Warning: Failed to cache resume locally: {exc}")
            
        return session

    def load_cached_resume_session(self) -> dict[str, Any] | None:
        with self.lock:
            if self.resume_sessions:
                return max(
                    self.resume_sessions.values(),
                    key=lambda item: str(item.get('updated_at') or item.get('created_at') or ''),
                )

        cached_session: dict[str, Any] | None = None
        if RESUME_ADVICE_PATH.exists():
            try:
                loaded = json.loads(RESUME_ADVICE_PATH.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    cached_session = loaded
            except Exception:
                cached_session = None

        markdown = ''
        if RESUME_MARKDOWN_PATH.exists():
            try:
                markdown = RESUME_MARKDOWN_PATH.read_text(encoding='utf-8').strip()
            except Exception:
                markdown = ''

        cached_markdown = str((cached_session or {}).get('markdown') or '').strip()
        if markdown and markdown != cached_markdown:
            cached_session = {
                'session_id': str(uuid.uuid4()),
                'resume_filename': RESUME_MARKDOWN_PATH.name,
                'uploaded_path': '',
                'markdown_path': str(RESUME_MARKDOWN_PATH),
                'markdown': markdown,
                'profile': {},
                'matched_jobs': [],
                'history': [],
                'created_at': now_iso(),
                'updated_at': now_iso(),
            }

        if not cached_session:
            return None

        session_id = str(cached_session.get('session_id') or '').strip() or str(uuid.uuid4())
        normalized = {
            'session_id': session_id,
            'resume_filename': str(cached_session.get('resume_filename') or (RESUME_MARKDOWN_PATH.name if markdown else '')),
            'uploaded_path': str(cached_session.get('uploaded_path') or ''),
            'markdown_path': str(cached_session.get('markdown_path') or (RESUME_MARKDOWN_PATH if markdown else '')),
            'markdown': markdown or cached_markdown,
            'profile': cached_session.get('profile') if isinstance(cached_session.get('profile'), dict) else {},
            'matched_jobs': cached_session.get('matched_jobs') if isinstance(cached_session.get('matched_jobs'), list) else [],
            'history': cached_session.get('history') if isinstance(cached_session.get('history'), list) else [],
            'created_at': str(cached_session.get('created_at') or now_iso()),
            'updated_at': now_iso(),
        }

        with self.lock:
            self.resume_sessions[session_id] = normalized

        try:
            if normalized['markdown']:
                RESUME_MARKDOWN_PATH.write_text(normalized['markdown'], encoding='utf-8')
            RESUME_ADVICE_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

        return normalized

    def resume_chat_stream(self, session_id: str, message: str, llm_config: dict[str, str] | None = None):
        clean_message = str(message or '').strip()
        if not clean_message:
            raise ValueError('消息不能为空。')
        with self.lock:
            session = self.resume_sessions.get(session_id)
            if not session and RESUME_ADVICE_PATH.exists():
                try:
                    cached_session = json.loads(RESUME_ADVICE_PATH.read_text(encoding='utf-8'))
                    if cached_session.get('session_id') == session_id:
                        session = cached_session
                        self.resume_sessions[session_id] = session
                except Exception:
                    pass
        if not session:
            raise KeyError('会话不存在，请重新上传简历。')

        history: list[dict[str, str]] = list(session.get('history') or [])
        history.append({'role': 'user', 'content': clean_message})
        history = history[-RESUME_CHAT_HISTORY_LIMIT :]

        yield {'type': 'status', 'content': '正在思考...'}

        # Extract context to see if LLM wants to run SQL
        extra_context = ""
        forced_query = should_force_resume_job_query(clean_message)
        try:
            resolved_llm_config = self.resolve_llm_config(llm_config)
            planner = {'need_query': True, 'sql_question': clean_message} if forced_query else {}
            if not forced_query:
                planner_text = self.call_model(
                    resolved_llm_config,
                    [
                        {
                            'role': 'system',
                            'content': (
                                '你是一个只负责路由判断的分类器，只能输出 JSON。'
                                '如果用户问题需要从岗位库查询新的数据，输出 JSON {"need_query": true, "sql_question": "..."}；否则输出 {"need_query": false}。'
                                '以下场景必须 need_query=true：用户要求岗位/职位/公司列表、推荐岗位、匹配岗位、薪资、地点、城市、标签、JD 摘要、岗位链接、url、投递链接，'
                                '或任何明确提到岗位库/数据库/SQL/表里已有数据的问题。'
                                '只有纯简历润色、经历改写、技能补强、面试表达等不依赖岗位库新数据的问题，才允许 need_query=false。'
                                '如果用户要链接或 url，sql_question 必须明确要求返回 url 字段，且不能编造链接。'
                            ),
                        },
                        {
                            'role': 'user',
                            'content': f'用户问题：{clean_message}',
                        },
                    ],
                    temperature=0.0,
                )
                planner = self.extract_json_object(planner_text)
            if planner.get('need_query'):
                sql_question = enrich_resume_sql_question(planner.get('sql_question') or clean_message, clean_message)
                yield {'type': 'system', 'content': f'正在查询岗位库…\n意图：{sql_question}'}
                try:
                    analysis_result = self.run_analysis(sql_question, llm_config)
                    rows = analysis_result.get('rows', [])[:10]
                    columns = analysis_result.get('columns', [])
                    sql_executed = analysis_result.get('sql', '')

                    def _safe(v: Any) -> Any:
                        if v is None or isinstance(v, (bool, int, float, str)):
                            return v
                        if isinstance(v, bytes):
                            return v.decode('utf-8', errors='replace')
                        return str(v)

                    safe_rows = [[_safe(cell) for cell in (r if isinstance(r, list) else list(r))] for r in rows]
                    yield {
                        'type': 'sql_result',
                        'content': {
                            'sql': sql_executed,
                            'columns': columns,
                            'rows': safe_rows,
                            'sql_question': sql_question,
                            'row_count': len(safe_rows),
                        },
                    }
                    extra_context = f"\n\n补充岗位库查询结果（针对问题'{sql_question}'）：\n列：{columns}\n数据：{rows}"
                except Exception as sql_exc:
                    yield {'type': 'system', 'content': f'查询失败：{sql_exc}'}
        except Exception as exc:
            extra_context = ""

        # Build streaming reply
        markdown = session.get('markdown') or ''
        profile = session.get('profile') or {}
        matched_jobs = session.get('matched_jobs') or []
        resume_text = self.truncate_text(markdown, 14_000)
        jobs_sample = [
            {
                'company': job.get('company'),
                'title': job.get('title'),
                'location': job.get('location'),
                'salary': job.get('salary'),
                'tags': job.get('tags'),
                'jd_summary': self.truncate_text(job.get('jd_summary') or '', 600),
                'match_score': job.get('match_score', 0),
            }
            for job in matched_jobs[:10]
        ]
        
        messages: list[dict[str, str]] = [
            {
                'role': 'system',
                'content': (
                    '你是“简历优化 + 岗位匹配”助手。'
                    '只基于给定简历与岗位库样本给建议，不要编造不存在的经历。'
                    '凡是岗位库字段相关的信息（公司、职位、薪资、地点、标签、JD 摘要、岗位链接/url），只能依据给定岗位库样本或补充查询结果回答。'
                    '如果没有查到结果，就直接说明未查到，绝不能编造岗位详情或链接。'
                    '输出必须是中文，结构清晰，可执行。'
                ),
            },
            {
                'role': 'user',
                'content': (
                    f'简历结构化信息：{json.dumps(profile, ensure_ascii=False)}\n\n'
                    f'岗位匹配样本（Top 10）：{json.dumps(jobs_sample, ensure_ascii=False)}\n\n'
                    f'简历 Markdown（截断）：\n{resume_text}\n\n'
                    f'用户问题：{(clean_message + extra_context).strip()}'
                ),
            },
        ]
        
        chat_history = [item for item in history if item.get('role') in {'user', 'assistant'} and str(item.get('content') or '').strip()]
        if chat_history:
            messages.extend(chat_history[-RESUME_CHAT_HISTORY_LIMIT :])
            
        yield {'type': 'start'}
        full_reply = ""
        try:
            for chunk in self.call_model_stream(resolved_llm_config, messages, temperature=0.2):
                full_reply += chunk
                yield {'type': 'chunk', 'content': chunk}
        except Exception as exc:
            yield {'type': 'error', 'content': f'\n[生成出错: {exc}]'}
            
        history.append({'role': 'assistant', 'content': full_reply})
        history = history[-RESUME_CHAT_HISTORY_LIMIT :]

        with self.lock:
            session['history'] = history
            session['updated_at'] = now_iso()
            self.resume_sessions[session_id] = session
            
        try:
            RESUME_ADVICE_PATH.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
            
        yield {'type': 'done'}


class AppHandler(BaseHTTPRequestHandler):
    server_version = 'JobRadar/1.0'

    def __init__(self, *args: Any, store: JobScoutStore, **kwargs: Any):
        self.store = store
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == '/api/jobs':
            self.respond_json(self.store.list_jobs(parse_qs(parsed.query)))
            return
        if parsed.path == '/api/apis':
            self.respond_json(self.store.read_api_config())
            return
        if parsed.path == '/api/fetch':
            self.respond_json(self.store.get_fetch_status())
            return
        if parsed.path == '/api/prefs':
            self.respond_json(self.store.read_prefs())
            return
        if parsed.path == '/api/sync':
            self.respond_json(self.store.sync_from_yaml())
            return
        if parsed.path == '/api/resume/current':
            session = self.store.load_cached_resume_session()
            if not session:
                self.respond_json({})
                return
            self.respond_json({
                'session_id': session.get('session_id', ''),
                'resume_filename': session.get('resume_filename', ''),
                'markdown_preview': self.store.truncate_text(session.get('markdown', ''), 2000, head=1200, tail=800),
                'advice': (session.get('history', [])[0]['content'] if session.get('history') else ''),
                'matched_jobs': session.get('matched_jobs', []),
                'history': session.get('history', []),
            })
            return
        self.serve_static(parsed.path)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == '/api/apis':
            try:
                payload = self.read_json_body()
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self.respond_json(self.store.write_api_config(payload))
            return
        if parsed.path == '/api/prefs':
            try:
                payload = self.read_json_body()
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self.respond_json(self.store.write_prefs(payload))
            return
        self.respond_error(HTTPStatus.NOT_FOUND, 'Not found')

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/jobs/'):
            try:
                payload = self.read_json_body()
                job_id = int(parsed.path.rsplit('/', 1)[-1])
                job = self.store.update_job(job_id, payload)
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except KeyError as exc:
                self.respond_error(HTTPStatus.NOT_FOUND, str(exc))
                return
            self.respond_json(job)
            return
        self.respond_error(HTTPStatus.NOT_FOUND, 'Not found')

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == '/api/resume/upload':
            try:
                filename, file_bytes = self.read_multipart_file('file', max_bytes=RESUME_UPLOAD_MAX_BYTES)
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            suffix = Path(filename).suffix.lower() if filename else ''
            if len(suffix) > 12:
                suffix = ''
            if not suffix:
                suffix = '.bin'

            try:
                uploaded_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                uploaded_file.write(file_bytes)
                uploaded_file.close()
                uploaded_path = uploaded_file.name
            except Exception as exc:
                self.respond_error(HTTPStatus.INTERNAL_SERVER_ERROR, f'保存上传文件失败: {exc}')
                return

            try:
                from markitdown import MarkItDown
            except Exception as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, f'未安装 markitdown，无法解析简历: {exc}')
                return

            try:
                converter = MarkItDown()
                result = converter.convert(uploaded_path)
                markdown = str(getattr(result, 'text_content', '') or '')
            except Exception as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, f'简历转换失败: {exc}')
                return

            markdown = markdown.strip()
            if not markdown:
                self.respond_error(HTTPStatus.BAD_REQUEST, '简历转换结果为空，请检查文件是否可读取。')
                return
            if len(markdown) > RESUME_MARKDOWN_MAX_CHARS:
                markdown = self.store.truncate_text(markdown, RESUME_MARKDOWN_MAX_CHARS, head=90_000, tail=20_000)

            try:
                md_file = tempfile.NamedTemporaryFile(delete=False, suffix='.md')
                md_file.write(markdown.encode('utf-8'))
                md_file.close()
                markdown_path = md_file.name
            except Exception as exc:
                self.respond_error(HTTPStatus.INTERNAL_SERVER_ERROR, f'保存 Markdown 失败: {exc}')
                return

            try:
                profile = self.store.analyse_resume_markdown(markdown)
                llm_config = self.store.read_api_config().get('llm', {})
                matched_jobs = self.store.match_resume_to_jobs(profile, llm_config=llm_config)
                advice = self.store.build_resume_advice(markdown, profile, matched_jobs, llm_config=llm_config)
                session = self.store.create_resume_session(
                    resume_filename=filename,
                    uploaded_path=uploaded_path,
                    markdown_path=markdown_path,
                    markdown=markdown,
                    profile=profile,
                    matched_jobs=matched_jobs,
                    initial_advice=advice,
                )
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except (RuntimeError, sqlite3.Error, json.JSONDecodeError) as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return

            payload = {
                'session_id': session['session_id'],
                'resume_filename': filename,
                'markdown_path': markdown_path,
                'profile': profile,
                'matched_jobs': matched_jobs[:10],
                'advice': advice,
                'markdown_preview': self.store.truncate_text(markdown, 1400),
            }
            self.respond_json(payload, status=HTTPStatus.CREATED)
            return
        if parsed.path == '/api/resume/chat':
            try:
                payload = self.read_json_body()
                session_id = str(payload.get('session_id') or '').strip()
                message = str(payload.get('message') or '').strip()
                llm_config = payload.get('llm_config') or {}
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if not session_id:
                self.respond_error(HTTPStatus.BAD_REQUEST, '缺少 session_id，请先上传简历。')
                return
                
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            
            try:
                for event in self.store.resume_chat_stream(session_id, message, llm_config):
                    self.wfile.write((json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8'))
                    self.wfile.flush()
            except Exception as exc:
                err_event = {'type': 'error', 'content': str(exc)}
                self.wfile.write((json.dumps(err_event, ensure_ascii=False) + '\n').encode('utf-8'))
                self.wfile.flush()
            return
        if parsed.path == '/api/jobs':
            try:
                payload = self.read_json_body()
                job = self.store.create_job(payload)
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self.respond_json(job, status=HTTPStatus.CREATED)
            return
        if parsed.path == '/api/jobs/bulk-delete':
            try:
                payload = self.read_json_body()
                result = self.store.delete_jobs(payload.get('ids') or [])
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except KeyError as exc:
                self.respond_error(HTTPStatus.NOT_FOUND, str(exc))
                return
            self.respond_json(result)
            return
        if parsed.path == '/api/fetch':
            try:
                payload = self.read_json_body()
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            try:
                result = self.store.start_fetch_task(
                    include_jd_fetch=bool_from_value(payload.get('include_jd_fetch')),
                    include_summary=bool_from_value(payload.get('include_summary')),
                    job_limit=int(payload.get('job_limit', 0) or 0),
                    jd_limit=int(payload.get('jd_limit', 10) or 10),
                    summary_limit=int(payload.get('summary_limit', 10) or 10),
                )
            except RuntimeError as exc:
                self.respond_error(HTTPStatus.CONFLICT, str(exc))
                return
            self.respond_json(result, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path == '/api/analysis/query':
            try:
                payload = self.read_json_body()
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            question = str(payload.get('question') or '').strip()
            llm_config = payload.get('llm_config') or {}
            if not question:
                self.respond_error(HTTPStatus.BAD_REQUEST, '问题不能为空。')
                return
            try:
                result = self.store.run_analysis(question, llm_config)
            except (ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self.respond_json(result)
            return
        self.respond_error(HTTPStatus.NOT_FOUND, 'Not found')

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == '/api/fetch':
            try:
                result = self.store.cancel_fetch_task()
            except RuntimeError as exc:
                self.respond_error(HTTPStatus.CONFLICT, str(exc))
                return
            self.respond_json(result)
            return
        if parsed.path.startswith('/api/jobs/'):
            try:
                job_id = int(parsed.path.rsplit('/', 1)[-1])
                deleted = self.store.delete_job(job_id)
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except KeyError as exc:
                self.respond_error(HTTPStatus.NOT_FOUND, str(exc))
                return
            self.respond_json({'deleted': True, 'job': deleted})
            return
        self.respond_error(HTTPStatus.NOT_FOUND, 'Not found')

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get('Content-Length', '0') or '0')
        raw = self.rfile.read(length) if length > 0 else b'{}'
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError as exc:
            raise ValueError('请求体不是合法 JSON。') from exc
        if not isinstance(data, dict):
            raise ValueError('请求体必须是 JSON 对象。')
        return data

    def read_multipart_file(self, field_name: str, *, max_bytes: int) -> tuple[str, bytes]:
        content_type = str(self.headers.get('Content-Type') or '')
        if 'multipart/form-data' not in content_type:
            raise ValueError('请使用 multipart/form-data 上传文件。')
        boundary = ''
        for part in content_type.split(';'):
            part = part.strip()
            if part.startswith('boundary='):
                boundary = part.split('=', 1)[-1].strip().strip('"')
                break
        if not boundary:
            raise ValueError('上传请求缺少 boundary。')

        length = int(self.headers.get('Content-Length', '0') or '0')
        if length <= 0:
            raise ValueError('上传内容为空。')
        if max_bytes > 0 and length > max_bytes:
            raise ValueError(f'文件过大，最大允许 {max_bytes // (1024 * 1024)}MB。')

        body = self.rfile.read(length)
        boundary_bytes = boundary.encode('utf-8')
        delimiter = b'--' + boundary_bytes
        parts = body.split(delimiter)
        for chunk in parts:
            if not chunk:
                continue
            if chunk.startswith(b'--'):
                continue
            if chunk.startswith(b'\r\n'):
                chunk = chunk[2:]
            header_blob, separator, content = chunk.partition(b'\r\n\r\n')
            if not separator:
                continue
            headers = header_blob.decode('latin-1', errors='ignore').split('\r\n')
            disposition = ''
            for header in headers:
                if header.lower().startswith('content-disposition:'):
                    disposition = header
                    break
            if not disposition:
                continue
            m_name = re.search(r'name="([^"]+)"', disposition)
            if not m_name or m_name.group(1) != field_name:
                continue
            m_filename = re.search(r'filename="([^"]*)"', disposition)
            filename = (m_filename.group(1) if m_filename else '').strip()
            if content.endswith(b'\r\n'):
                content = content[:-2]
            if content.endswith(b'--'):
                content = content[:-2]
            safe_name = os.path.basename(filename).replace('\x00', '').strip()
            if not safe_name:
                safe_name = 'resume'
            return safe_name, content
        raise ValueError(f'未找到字段 {field_name} 的文件内容。')

    def serve_static(self, request_path: str) -> None:
        relative_path = request_path.lstrip('/') or 'index.html'
        static_root = self.store.static_dir.resolve()
        target = (static_root / relative_path).resolve()

        if not target.exists() or target.is_dir() or static_root not in target.parents:
            target = static_root / 'index.html'

        if not target.exists():
            self.respond_error(HTTPStatus.NOT_FOUND, '前端文件不存在。')
            return

        content = target.read_bytes()
        content_type, _ = mimetypes.guess_type(str(target))
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_type or 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def respond_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_error(self, status: HTTPStatus, message: str) -> None:
        self.respond_json({'error': message}, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f'[{self.log_date_time_string()}] {self.address_string()} {fmt % args}')


def build_handler(store: JobScoutStore):
    def factory(*args: Any, **kwargs: Any) -> AppHandler:
        return AppHandler(*args, store=store, **kwargs)

    return factory


def main() -> None:
    parser = argparse.ArgumentParser(description='Job Radar 本地 Web 控制台')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()

    store = JobScoutStore(ROOT)
    handler = build_handler(store)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f'Job Radar running at http://{args.host}:{args.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
