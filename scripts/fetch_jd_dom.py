#!/usr/bin/env python3
"""
fetch_jd_dom.py — 通过本机 Chrome osascript 抓取 BOSS直聘 JD 详情页原始文本。

硬阻断规则：
- jd_full 为空 → 写 fetch_error，exit(1) 让 lobster failFast 捕获
- 不做任何摘要/推断，只写原始 DOM 文本（带换行）
"""
import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
OSASCRIPT = ROOT / 'scripts/chrome_osascript.py'

DOM_JD_JS = r"""
(() => {
  const keepLines = (s) => String(s || '')
    .replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\u00a0/g, ' ')
    .split('\n').map(l => l.replace(/[\t\f\v ]+/g, ' ').trim()).filter(Boolean).join('\n');

  const selectors = [
    '.job-sec-text',
    '[class*="job-sec-text"]',
    '.job-detail-section .text',
    '.job-detail-content',
  ];

  const candidates = [];
  for (const sel of selectors) {
    let order = 0;
    for (const el of Array.from(document.querySelectorAll(sel))) {
      const t = keepLines(el.innerText || el.textContent);
      if (!t || t.length < 80) continue;
      candidates.push({ sel, text: t, order });
      order += 1;
    }
  }

  if (!candidates.length) {
    const marker = Array.from(document.querySelectorAll('*')).find(
      el => /职位描述/.test((el.textContent || '').trim())
    );
    if (marker) {
      let p = marker;
      for (let i = 0; i < 5 && p; i++) {
        const t = keepLines(p.innerText || p.textContent);
        if (t.length > 160) { candidates.push({ sel: 'marker-parent', text: t, order: 0 }); break; }
        p = p.parentElement;
      }
    }
  }

  if (!candidates.length) return JSON.stringify({ ok: false, jd: '' });
  candidates.sort((a, b) => a.order - b.order);
  return JSON.stringify({ ok: true, jd: candidates[0].text });
})();
"""


def run(cmd: list) -> dict:
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout or '').strip()
    if not out:
        return {'ok': False, 'error': (p.stderr or 'empty').strip()}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {'ok': False, 'error': f'INVALID_JSON: {out[:120]}'}


def fetch_jd(url: str, min_delay: float = 1.5, max_delay: float = 4.0) -> str:
    run(['python3', str(OSASCRIPT), 'open-url', url])
    time.sleep(random.uniform(min_delay, max_delay))

    ret = run(['python3', str(OSASCRIPT), 'execute-js', DOM_JD_JS])
    if not ret.get('ok'):
        return ''

    result_str = ret.get('result', '')
    if not result_str:
        return ''
    try:
        data = json.loads(result_str)
        return data.get('jd', '').strip()
    except (json.JSONDecodeError, AttributeError):
        return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--yaml', required=True)
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--min-delay', type=float, default=2.0)
    ap.add_argument('--max-delay', type=float, default=5.0)
    ap.add_argument('--refetch', action='store_true',
                    help='Re-fetch even if jd_full already exists')
    args = ap.parse_args()

    limit = max(1, min(args.limit, 50))
    p = Path(args.yaml)
    data = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    items = data.get('internships', data) if isinstance(data, dict) else data

    targets = [
        it for it in items
        if isinstance(it, dict)
        and it.get('url', '').startswith('http')
        and (args.refetch or not (it.get('jd_full') or '').strip())
        and not it.get('fetch_error')
    ][:limit]

    if not targets:
        print('No targets to fetch.')
        sys.exit(0)

    failed = 0
    for i, it in enumerate(targets, 1):
        url = it.get('url', '')
        print(
            'PROGRESS ' + json.dumps(
                {
                    'phase': 'jd-fetch',
                    'current': i,
                    'total': len(targets),
                    'message': f'正在补抓 JD: {it.get("company", "")} | {it.get("title", "")}',
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        jd = fetch_jd(url, args.min_delay, args.max_delay)

        if not jd:
            it['fetch_error'] = 'empty_job_sec_text'
            print(f'[{i}/{len(targets)}] FAIL: {url}', file=sys.stderr)
            failed += 1
        else:
            it['jd_full'] = jd
            it.pop('fetch_error', None)
            print(f'[{i}/{len(targets)}] OK: {url}')

        if i < len(targets):
            time.sleep(random.uniform(args.min_delay, args.max_delay))

    # 写回（兼容 internships: [...] 和裸列表两种格式）
    if isinstance(data, dict):
        data['internships'] = items
        p.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False), encoding='utf-8')
    else:
        p.write_text(yaml.dump(items, allow_unicode=True, default_flow_style=False, sort_keys=False), encoding='utf-8')

    print(f'fetched={len(targets) - failed} failed={failed}')
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
