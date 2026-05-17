#!/usr/bin/env python3
"""
notion_sync.py — Sync internships.yaml to a Notion database.

Usage:
  python3 notion_sync.py [--yaml PATH] [--db-id ID_OR_URL] [--mode new|update|all|reset]
                         [--filter COMPANY] [--dry-run]

Modes:
  new    POST entries where notion_page_id is empty (default)
  update PATCH entries that already have notion_page_id (full overwrite)
  all    new + update
  reset  archive all DB pages → clear YAML notion_page_ids → POST all entries fresh

Inputs:
  --yaml     Path to internships.yaml. Default: ~/.openclaw/workspace/internships.yaml
  --db-id    Notion DB ID (UUID or full Notion share URL).
             Falls back to notion_db_id in internship-prefs.yaml; prompts if missing.
  --filter   Only sync entries whose company name contains this string.
  --dry-run  Print what would happen without making any API calls.

Outputs:
  stdout:    ✅ Company | created/updated   or   ❌ Company | reason
  Side-effect: writes notion_page_id back to YAML for newly created pages.
  Exit code: 0 = all succeeded, 1 = any failure.

Environment:
  NOTION_API_KEY  — required
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import aiohttp
except ImportError:
    print("❌ aiohttp not installed. Run: pip install aiohttp")
    sys.exit(1)

WORKSPACE    = Path(__file__).parent.parent
DEFAULT_YAML = WORKSPACE / "internships.yaml"
PREFS_FILE   = WORKSPACE / "internship-prefs.yaml"
API_CONFIG_FILE = WORKSPACE / "api-config.yaml"
API_VERSION  = "2022-06-28"
NOTION_KEY   = ""
BASE_URL     = "https://api.notion.com/v1"
CONCURRENCY  = 3  # stay within Notion's ~3 req/s average

FIELD_MAP = {
    "company":       ("Name",     "title"),
    "salary":        ("薪资",     "rich_text"),
    "location":      ("城市",     "rich_text"),
    "company_size":  ("规模",     "select"),
    "funding_stage": ("融资阶段", "select"),
    "jd_quality":    ("JD质量",   "select"),
    "status":        ("状态",     "select"),
    "tags":          ("技术标签", "multi_select"),
    "url":           ("链接",     "url"),
    "collected_at":  ("收录日期", "date"),
    "jd_summary":    ("JD摘要",   "rich_text"),
    "jd_full":       ("JD原文",   "rich_text"),
}

# ── DB ID helpers ─────────────────────────────────────────────────────────────

def extract_db_id(raw: str) -> str:
    m = re.search(r"([0-9a-f]{32})", raw.replace("-", ""))
    if not m:
        return ""
    h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

def load_prefs(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.suffix.lower() in ['.yaml', '.yml']:
        try:
            import yaml as _yaml
            with path.open('r', encoding='utf-8') as f:
                res = _yaml.safe_load(f)
                return res if isinstance(res, dict) else {}
        except ImportError:
            pass
    content = path.read_text(encoding='utf-8')
    prefs = {}
    m = re.search(r"notion_db_id:\s*([^\s\n]+)", content)
    if m:
        prefs['notion_db_id'] = m.group(1)
    return prefs

def load_api_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml as _yaml
        with path.open('r', encoding='utf-8') as f:
            res = _yaml.safe_load(f)
            return res if isinstance(res, dict) else {}
    except ImportError:
        return {}

def read_api_config_notion() -> dict:
    config = load_api_config(API_CONFIG_FILE)
    notion = config.get('notion') if isinstance(config, dict) else {}
    return notion if isinstance(notion, dict) else {}

def resolve_notion_api_key() -> str:
    notion = read_api_config_notion()
    return os.environ.get("NOTION_API_KEY", "").strip() or str(notion.get('api_key') or '').strip()

def read_prefs_db_id() -> str:
    notion = read_api_config_notion()
    saved_db_id = str(notion.get('database_id') or notion.get('db_id') or '').strip()
    if saved_db_id:
        return saved_db_id
    prefs = load_prefs(PREFS_FILE)
    return str(prefs.get('notion_db_id', '')).strip()

def write_prefs_db_id(db_id: str):
    config = load_api_config(API_CONFIG_FILE)
    notion = config.get('notion') if isinstance(config, dict) else {}
    if not isinstance(notion, dict):
        notion = {}
    notion['database_id'] = db_id
    config['notion'] = notion

    try:
        import yaml as _yaml
        API_CONFIG_FILE.write_text(
            _yaml.dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding='utf-8',
        )
    except ImportError:
        pass

    if not PREFS_FILE.exists():
        if PREFS_FILE.suffix.lower() in ['.yaml', '.yml']:
            PREFS_FILE.write_text(f"notion_db_id: {db_id}\n", encoding='utf-8')
        else:
            PREFS_FILE.write_text(f"---\nnotion_db_id: {db_id}\n---\n", encoding='utf-8')
        return

    content = PREFS_FILE.read_text(encoding='utf-8')
    if "notion_db_id:" in content:
        content = re.sub(r"notion_db_id:\s*[^\s\n]+", f"notion_db_id: {db_id}", content)
    else:
        if PREFS_FILE.suffix.lower() in ['.yaml', '.yml']:
            content = f"{content}\nnotion_db_id: {db_id}\n"
        else:
            content = f"---\nnotion_db_id: {db_id}\n---\n\n{content}"
    PREFS_FILE.write_text(content, encoding='utf-8')

def resolve_db_id(arg: str) -> str:
    if arg:
        db_id = extract_db_id(arg)
        if db_id:
            return db_id
    db_id = read_prefs_db_id()
    if db_id:
        return db_id
    print("⚠️  未找到 Notion 数据库 ID。")
    print("请提供：UUID / Notion 分享链接 / 输入 'new' 新建")
    raw = input("→ ").strip()
    if raw.lower() == "new":
        db_id = asyncio.run(create_database_sync())
    else:
        db_id = extract_db_id(raw)
    if not db_id:
        print("❌ 无法解析，退出。")
        sys.exit(1)
    write_prefs_db_id(db_id)
    print(f"✅ 已保存 notion_db_id: {db_id}")
    return db_id

# ── Async HTTP ────────────────────────────────────────────────────────────────

def headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_KEY}",
        "Notion-Version": API_VERSION,
        "Content-Type": "application/json",
    }

async def notion_req(session: aiohttp.ClientSession, method: str,
                     endpoint: str, payload: dict | None = None,
                     retries: int = 3) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    for attempt in range(retries):
        try:
            async with session.request(method, url, json=payload,
                                       headers=headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    wait = int(resp.headers.get("Retry-After", "2"))
                    print(f"  ⏳ rate limited, waiting {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                return await resp.json()
        except Exception as e:
            if attempt == retries - 1:
                return {"error": str(e)}
            await asyncio.sleep(1.5)
    return {"error": "max retries exceeded"}

# ── YAML helpers ──────────────────────────────────────────────────────────────

def parse_yaml(path: Path) -> list[dict]:
    try:
        import yaml as _yaml
        raw = _yaml.safe_load(path.read_text())
    except ImportError:
        raw = None

    if raw is None:
        # fallback: try ruamel or plain regex
        raw = None

    # Support both top-level list and {"internships": [...]}
    if isinstance(raw, dict):
        for key in ("internships", "jobs", "entries"):
            if key in raw and isinstance(raw[key], list):
                raw = raw[key]
                break
        else:
            raw = list(raw.values())[0] if raw else []

    if not isinstance(raw, list):
        return []

    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        e = {}
        for f in ["company", "salary", "location", "company_size",
                  "funding_stage", "jd_summary", "jd_full", "url", "source", "status",
                  "jd_quality", "collected_at", "notion_page_id"]:
            val = item.get(f, "")
            e[f] = str(val) if val is not None else ""
        tags = item.get("tags", [])
        if isinstance(tags, list):
            e["tags"] = [str(t) for t in tags if t]
        elif isinstance(tags, str):
            e["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        else:
            e["tags"] = []
        if not e.get("jd_full"):
            legacy = item.get("jd_summary", "")
            e["jd_full"] = str(legacy) if legacy is not None else ""
        if not e.get("jd_summary"):
            txt = " ".join(str(e.get("jd_full", "")).split())
            e["jd_summary"] = txt[:50]
        entries.append(e)
    return entries

def clear_notion_ids(path: Path):
    import yaml as _yaml
    raw = _yaml.safe_load(path.read_text())
    lst = raw["internships"] if isinstance(raw, dict) and "internships" in raw else raw
    for item in lst:
        if isinstance(item, dict):
            item["notion_page_id"] = ""
    path.write_text(_yaml.dump(raw, allow_unicode=True, sort_keys=False))

def write_notion_id(path: Path, url: str, notion_id: str):
    import yaml as _yaml
    raw = _yaml.safe_load(path.read_text())
    lst = raw["internships"] if isinstance(raw, dict) and "internships" in raw else raw
    for item in lst:
        if isinstance(item, dict) and item.get("url") == url:
            item["notion_page_id"] = notion_id
            break
    path.write_text(_yaml.dump(raw, allow_unicode=True, sort_keys=False))

# ── Property builder ──────────────────────────────────────────────────────────

def build_props(entry: dict) -> dict:
    props = {}
    for yaml_key, (notion_key, notion_type) in FIELD_MAP.items():
        val = entry.get(yaml_key, "")
        if not val and yaml_key != "company":
            continue
        if notion_type == "title":
            props[notion_key] = {"title": [{"text": {"content": str(val)[:2000]}}]}
        elif notion_type == "rich_text":
            props[notion_key] = {"rich_text": [{"text": {"content": str(val)[:2000]}}]}
        elif notion_type == "select":
            props[notion_key] = {"select": {"name": str(val)}}
        elif notion_type == "multi_select":
            props[notion_key] = {"multi_select": [{"name": t} for t in val[:10]]}
        elif notion_type == "url":
            props[notion_key] = {"url": val}
        elif notion_type == "date":
            props[notion_key] = {"date": {"start": val}}
    return props

# ── Core async tasks ──────────────────────────────────────────────────────────

async def fetch_all_page_ids(session: aiohttp.ClientSession, db_id: str) -> list[str]:
    ids = []
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = await notion_req(session, "POST", f"databases/{db_id}/query", payload)
        for p in res.get("results", []):
            ids.append(p["id"])
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return ids

async def archive_page(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                       page_id: str) -> bool:
    async with sem:
        res = await notion_req(session, "PATCH", f"pages/{page_id}", {"archived": True})
        return bool(res.get("archived") or res.get("id"))

async def upsert_entry(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                       entry: dict, db_id: str, yaml_path: Path,
                       dry_run: bool) -> tuple[str, bool]:
    company = entry.get("company", "?")
    nid = entry.get("notion_page_id", "")
    action = "updated" if nid else "created"

    if dry_run:
        return (f"[dry-run] {action} → {company}", True)

    async with sem:
        props = build_props(entry)
        if nid:
            res = await notion_req(session, "PATCH", f"pages/{nid}", {"properties": props})
        else:
            res = await notion_req(session, "POST", "pages",
                                   {"parent": {"database_id": db_id}, "properties": props})

    if res.get("id"):
        if not nid and entry.get("url"):
            write_notion_id(yaml_path, entry["url"], res["id"])
        return (f"✅ {company} | {action}", True)
    else:
        err = res.get("message") or res.get("error") or "unknown"
        return (f"❌ {company} | {err[:80]}", False)

async def create_database_sync() -> str:
    async with aiohttp.ClientSession() as session:
        parent_id = "3102496b-9cb5-8003-8188-d6bf72b71afa"
        res = await notion_req(session, "POST", "databases", {
            "parent": {"type": "page_id", "page_id": parent_id},
            "icon": {"type": "emoji", "emoji": "📋"},
            "title": [{"type": "text", "text": {"content": "实习岗位追踪"}}],
        })
        db_id = res.get("id", "")
        if not db_id:
            print("❌ 创建失败:", res.get("message", ""))
            sys.exit(1)
        await notion_req(session, "PATCH", f"databases/{db_id}", {"properties": {
            "薪资": {"rich_text": {}}, "城市": {"rich_text": {}},
            "规模": {"select": {"options": [{"name": "20-99人", "color": "green"}, {"name": "100-499人", "color": "blue"}]}},
            "融资阶段": {"select": {"options": [
                {"name": "天使轮", "color": "pink"}, {"name": "A轮", "color": "orange"},
                {"name": "B轮", "color": "yellow"}, {"name": "C轮", "color": "green"},
                {"name": "未融资", "color": "gray"}, {"name": "不需要融资", "color": "gray"},
            ]}},
            "JD质量": {"select": {"options": [{"name": "good", "color": "green"}, {"name": "unclear", "color": "yellow"}, {"name": "skip", "color": "red"}]}},
            "状态": {"select": {"options": [
                {"name": "pending", "color": "gray"}, {"name": "applied", "color": "blue"},
                {"name": "interviewing", "color": "orange"}, {"name": "offered", "color": "green"},
                {"name": "rejected", "color": "red"}, {"name": "ghosted", "color": "brown"},
            ]}},
            "技术标签": {"multi_select": {"options": []}},
            "来源": {"rich_text": {}}, "链接": {"url": {}},
            "收录日期": {"date": {}}, "JD摘要": {"rich_text": {}},
        }})
        print(f"✅ 数据库已创建: {db_id}")
        return db_id

# ── Main ──────────────────────────────────────────────────────────────────────

async def run(args):
    yaml_path = Path(args.yaml).expanduser()
    db_id = resolve_db_id(args.db_id)
    entries = parse_yaml(yaml_path)

    if args.filter:
        entries = [e for e in entries if args.filter.lower() in e.get("company", "").lower()]

    sem = asyncio.Semaphore(CONCURRENCY)

    async with aiohttp.ClientSession() as session:

        # ── reset: archive all → clear YAML → rebuild ──
        if args.mode == "reset":
            print("🔄 Step 1/3: 查询数据库所有页面...")
            page_ids = await fetch_all_page_ids(session, db_id)
            print(f"  找到 {len(page_ids)} 条，开始 archive...")

            if not args.dry_run:
                tasks = [archive_page(session, sem, pid) for pid in page_ids]
                results = await asyncio.gather(*tasks)
                ok = sum(results)
                print(f"  archived {ok}/{len(page_ids)}")

                print("🔄 Step 2/3: 清空 YAML notion_page_id...")
                clear_notion_ids(yaml_path)
                entries = parse_yaml(yaml_path)
                if args.filter:
                    entries = [e for e in entries if args.filter.lower() in e.get("company","").lower()]
            else:
                print(f"  [dry-run] would archive {len(page_ids)} pages")

            print(f"🔄 Step 3/3: 全量重建 {len(entries)} 条...")
            tasks = [upsert_entry(session, sem, e, db_id, yaml_path, args.dry_run)
                     for e in entries]
            results = await asyncio.gather(*tasks)
            errors = 0
            for msg, ok in results:
                print(f"  {msg}")
                if not ok:
                    errors += 1
            print(f"\n✅ reset 完成: {len(entries)-errors}/{len(entries)} 条成功")
            return errors

        # ── new / update / all ──
        if args.mode == "new":
            target = [e for e in entries if not e.get("notion_page_id")]
        elif args.mode == "update":
            target = [e for e in entries if e.get("notion_page_id")]
        else:
            target = entries

        print(f"DB: {db_id} | mode: {args.mode} | entries: {len(target)}"
              + (" [dry-run]" if args.dry_run else ""))

        tasks = [upsert_entry(session, sem, e, db_id, yaml_path, args.dry_run)
                 for e in target]
        results = await asyncio.gather(*tasks)
        errors = 0
        for msg, ok in results:
            print(msg)
            if not ok:
                errors += 1
        return errors


def main():
    global NOTION_KEY
    parser = argparse.ArgumentParser(description="Sync internships.yaml → Notion")
    parser.add_argument("--yaml",    default=str(DEFAULT_YAML))
    parser.add_argument("--db-id",   default="")
    parser.add_argument("--mode",    choices=["new", "update", "all", "reset"], default="new")
    parser.add_argument("--filter",  default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    NOTION_KEY = resolve_notion_api_key()

    if not NOTION_KEY:
        print("❌ NOTION_API_KEY 未配置，请在环境变量或 api-config.yaml 中提供")
        sys.exit(1)

    errors = asyncio.run(run(args))
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
