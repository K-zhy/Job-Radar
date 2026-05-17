#!/usr/bin/env python3
"""
fetch_job_links.py — 通过 BOSS直聘内部 API 抓取职位列表，写入 internships.yaml。
只写结构字段（title/company/salary/url 等），不写 jd_full/jd_summary（留给后续节点）。
"""
import argparse, json, re, subprocess, time, random
from datetime import date
from pathlib import Path
import yaml

ROOT = Path(__file__).parent.parent
MCP  = ROOT / 'scripts/mcp_call.py'

# 默认兜底值，优先从 internship-prefs.md 读取
DEFAULT_BIG_TECH = {
    '字节', '抖音', 'tiktok', '阿里', '淘宝', '天猫', '腾讯', '百度', '美团', '京东',
    '华为', '小米', '网易', 'bilibili', '哔哩', '滴滴', '快手', '拼多多', '蚂蚁',
    '微软', '谷歌', 'google', 'microsoft', 'apple', '苹果',
}

DEFAULT_NON_TECH = {
    '销售', '运营', '市场', '客服', '行政', '财务', '人事', 'hr', '猎头', '外包', '派遣',
    '合伙人', '审核', '售前', '售后', '文职',
}

CITY_CODES = {
    '全国': '100010000', '北京': '101010100', '上海': '101020100',
    '杭州': '101210100', '深圳': '101280600', '广州': '101280100',
    '成都': '101270100', '武汉': '101200100', '南京': '101190100',
    '苏州': '101190400', '无锡': '101190200', '宁波': '101210400',
}

SCALE_CODES = {
    '0-20人': '301',
    '20-99人': '302',
    '100-499人': '303',
    '500-999人': '304',
    '1000-9999人': '305',
    '10000人以上': '306',
}
JOB_TYPES = {'全职': '1901', '实习': '4', '校招': '1903'}


def normalize_scale_label(value: str) -> str:
    return re.sub(r'\s+', '', str(value or '').strip())


def run_mcp(tool, args_dict):
    try:
        out = subprocess.check_output(
            ['python3', str(MCP), tool, json.dumps(args_dict, ensure_ascii=False)],
            text=True, stderr=subprocess.PIPE,
        )
        return out.strip()
    except subprocess.CalledProcessError as e:
        print(f"  MCP Call Error: {e.stderr.strip()}")
        return ""


def parse_list(txt: str, pattern: str, default: str) -> list[str]:
    m = re.search(pattern, txt)
    if not m: return [x.strip() for x in re.split(r'[,，、]', default) if x.strip()]
    raw = m.group(1).split('\n')[0].strip()
    return [x.strip() for x in re.split(r'[,，、]', raw) if x.strip()]


def parse_prefs(path: Path):
    if path.suffix.lower() in ['.yaml', '.yml']:
        # 新版 YAML 解析
        with path.open('r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
            
        queries = cfg.get('queries', ['agent'])
        if isinstance(queries, str):
            queries = [queries]
            
        cities = cfg.get('cities', ['全国'])
        if isinstance(cities, str):
            cities = [cities]
            
        salary_cfg = cfg.get('salary', {})
        min_sal_day = int(salary_cfg.get('min_day', 150))
        min_sal_month = int(salary_cfg.get('min_month', 3000))
        
        scales = cfg.get('scales', ['20-99人'])
        if isinstance(scales, str):
            scales = [scales]
            
        job_type_pref = cfg.get('job_type', '全职')
        
        filters_cfg = cfg.get('filters', {})
        extra_exc = filters_cfg.get('exclude_extra', [])
        if isinstance(extra_exc, str):
            extra_exc = [extra_exc]
            
        big_tech_raw = filters_cfg.get('exclude_big_tech', [])
        if isinstance(big_tech_raw, str):
            big_tech_raw = [big_tech_raw]
            
        if not big_tech_raw:
            big_tech = DEFAULT_BIG_TECH
        elif set(big_tech_raw) & {'无', '不限'}:
            big_tech = set()
        else:
            big_tech = {x.lower() for x in big_tech_raw}
            
        non_tech_raw = filters_cfg.get('exclude_non_tech', [])
        if isinstance(non_tech_raw, str):
            non_tech_raw = [non_tech_raw]
            
        if not non_tech_raw:
            non_tech = DEFAULT_NON_TECH
        elif set(non_tech_raw) & {'无', '不限'}:
            non_tech = set()
        else:
            non_tech = {x.lower() for x in non_tech_raw}
            
    else:
        # 旧版 MD 解析（兼容保留）
        txt = path.read_text(encoding='utf-8')
    
        def find(pattern, default=''):
            m = re.search(pattern, txt)
            return m.group(1).strip() if m else default
    
        queries   = parse_list(txt, r'搜索词[^:：]*[:：][ \t]*(.+)', 'agent')
        cities    = parse_list(txt, r'目标城市[^:：]*[:：][ \t]*(.+)', '全国')
        min_sal_day   = int(find(r'日薪下限[^:：]*[:：][ \t]*(\d+)', '150'))
        min_sal_month = int(find(r'月薪下限[^:：]*[:：][ \t]*(\d+)', '3000'))
        scales    = parse_list(txt, r'公司规模[^:：]*[:：][ \t]*(.+)', '20-99人')
        job_type_pref = find(r'职位类型[^:：]*[:：][ \t]*(.+)', '全职')
        extra_exc = parse_list(txt, r'排除关键词[^:：]*[:：][ \t]*(.+)', '')
    
        big_tech_raw = parse_list(txt, r'大厂排除[^:：]*[:：][ \t]*(.+)', '')
        if not big_tech_raw:
            big_tech = DEFAULT_BIG_TECH
        elif set(big_tech_raw) & {'无', '不限'}:
            big_tech = set()
        else:
            big_tech = {x.lower() for x in big_tech_raw}
    
        non_tech_raw = parse_list(txt, r'非技术岗排除[^:：]*[:：][ \t]*(.+)', '')
        if not non_tech_raw:
            non_tech = DEFAULT_NON_TECH
        elif set(non_tech_raw) & {'无', '不限'}:
            non_tech = set()
        else:
            non_tech = {x.lower() for x in non_tech_raw}

    city_codes  = [CITY_CODES.get(c, '100010000') for c in cities]
    normalized_scales = [str(scale).strip() for scale in scales if str(scale).strip()]
    scale_queries = [
        {
            'label': scale,
            'code': SCALE_CODES.get(scale, ''),
        }
        for scale in normalized_scales
    ] or [{'label': '不限规模', 'code': ''}]
    job_type_code = JOB_TYPES.get(job_type_pref, '1901')
    return queries, city_codes, min_sal_day, min_sal_month, normalized_scales, scale_queries, job_type_code, extra_exc, big_tech, non_tech


def is_excluded(job: dict, min_sal_day: int, min_sal_month: int, job_type_code: str,
                extra_exc: list, big_tech: set, non_tech: set, selected_scales: list[str]) -> tuple[bool, str]:
    name    = (job.get('jobName') or '').lower()
    company = (job.get('brandName') or '').lower()
    sal_str = (job.get('salaryDesc') or '').lower()
    scale_name = normalize_scale_label(job.get('brandScaleName') or '')

    for k in big_tech:
        if k in company:
            return True, f"大厂排除 ({k})"
    
    for k in non_tech:
        if k in name:
            return True, f"非技术岗排除 ({k})"
            
    for k in extra_exc:
        if k.lower() in name or k.lower() in company:
            return True, f"自定义排除 ({k})"

    normalized_selected_scales = {normalize_scale_label(scale) for scale in selected_scales if str(scale).strip()}
    if normalized_selected_scales and scale_name not in normalized_selected_scales:
        return True, f"公司规模不匹配 ({job.get('brandScaleName') or '未知规模'})"

    # 薪资处理
    m = re.search(r'(\d+)', sal_str)
    if not m:
        return False, ""
    
    val = int(m.group(1))
    # 全职/校招只看月薪；实习优先按日薪判断，若返回 K 薪则退回月薪判断。
    if job_type_code == '4' and 'k' not in sal_str:
        if val < min_sal_day:
            return True, f"薪资过低 ({sal_str} < {min_sal_day}元/天)"
    elif 'k' in sal_str:
        # 转换为月薪 (15K -> 15000)
        actual_month = val * 1000
        if actual_month < min_sal_month:
            return True, f"薪资过低 ({sal_str} < {min_sal_month/1000:.0f}K)"

    return False, ""


def extract_json(s: str):
    m = re.search(r'\{.*\}', s, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prefs', required=True)
    ap.add_argument('--yaml',  required=True)
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    prefs_path = Path(args.prefs)
    yaml_path  = Path(args.yaml)

    if yaml_path.exists():
        data = yaml.safe_load(yaml_path.read_text(encoding='utf-8')) or []
    else:
        data = []
    if isinstance(data, dict):
        items = data.get('internships', [])
    elif isinstance(data, list):
        items = data
    else:
        items = []
    by_url = {it.get('url'): it for it in items if isinstance(it, dict) and it.get('url')}

    queries, city_codes, min_sal_day, min_sal_month, selected_scales, scale_queries, job_type_code, extra_exc, big_tech, non_tech = parse_prefs(prefs_path)
    total_limit = max(0, args.limit)
    total_searches = max(1, len(queries) * len(city_codes) * len(scale_queries))

    # 激活 cookie
    run_mcp('chrome_navigate', {'url': 'https://www.zhipin.com/web/geek/job?query=agent&city=100010000'})

    added = 0
    progress = 0
    stop_requested = False
    for q in queries:
        for city in city_codes:
            for scale_query in scale_queries:
                scale_label = scale_query['label']
                scale_code = scale_query['code']
                progress += 1
                print(
                    'PROGRESS ' + json.dumps(
                        {
                            'phase': 'job-list',
                            'current': progress,
                            'total': total_searches,
                            'message': f'正在抓岗位列表: {q} / {city} / {scale_label}',
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                print(f"Searching: query='{q}', city={city}, scale={scale_label}...")
                scale_param = f'&scale={scale_code}' if scale_code else ''
                js = (
                    f"var xhr = new XMLHttpRequest();"
                    f"xhr.open('GET', `/wapi/zpgeek/search/joblist.json?query=${{encodeURIComponent('{q}')}}&city={city}&page=1&pageSize=30&jobType={job_type_code}{scale_param}`, false);"
                    f"xhr.withCredentials = true;"
                    f"xhr.send();"
                    f"xhr.responseText;"
                )
                raw    = run_mcp('chrome_javascript', {'code': js})
                data_j = extract_json(raw)
                  
                if not data_j:
                    print(f"  Warning: Could not parse JSON response for {q}")
                    if raw:
                        print(f"  Raw output: {raw[:200]}...")
                    continue
                 
                if data_j.get('code') != 0:
                    code = data_j.get('code')
                    msg = data_j.get('message', 'Unknown error')
                    print(f"  API Error: {msg} (code={code})")
                    if code == 7:
                        print("  Tip: Please make sure you are logged in to BOSS Zhipin in Chrome.")
                    elif code == 37:
                        print("  Critical: Environment anomaly detected (Anti-crawling).")
                        print("  Action: Please open Chrome, visit BOSS Zhipin, and solve any CAPTCHA or slide verification.")
                        print("  Waiting 10 seconds before continuing...")
                        time.sleep(10)
                    continue

                jobs   = ((data_j.get('zpData') or {}).get('jobList') or []) if isinstance(data_j, dict) else []
                print(f"  Found {len(jobs)} jobs in raw response.")

                for job in jobs:
                    eid = job.get('encryptJobId', '')
                    if not eid:
                        continue
                    url = f'https://www.zhipin.com/job_detail/{eid}.html'
                    if url in by_url:
                        continue
                    
                    excluded, reason = is_excluded(job, min_sal_day, min_sal_month, job_type_code, extra_exc, big_tech, non_tech, selected_scales)
                    if excluded:
                        print(f"    Skipping: {job.get('jobName')} @ {job.get('brandName')} (Filtered: {reason})")
                        continue

                    rec = {
                        'collected_at':   str(date.today()),
                        'company':        (job.get('brandName') or '').strip(),
                        'title':          job.get('jobName', ''),
                        'salary':         job.get('salaryDesc', ''),
                        'location':       job.get('cityName', ''),
                        'company_size':   job.get('brandScaleName', ''),
                        'funding_stage':  job.get('brandIndustry', ''),
                        'job_type':       '全职' if job_type_code == '1901' else ('校招' if job_type_code == '1903' else '实习'),
                        'source':         'boss直聘',
                        'url':            url,
                        'status':         'pending',
                        'jd_full':        '',
                        'jd_summary':     '',
                        'tags':           [],
                        'jd_quality':     '',
                        'notion_page_id': '',
                    }
                    items.append(rec)
                    by_url[url] = rec
                    added += 1
                    if total_limit and added >= total_limit:
                        stop_requested = True
                        print(f'  Reached job limit: {total_limit}')
                        break

                if stop_requested:
                    break

                # 每次搜索之间随机延迟 4-12s，避免触发风控
                delay = random.uniform(4.0, 12.0)
                print(f'  sleeping {delay:.1f}s...')
                time.sleep(delay)

            if stop_requested:
                break
        if stop_requested:
            break

    if isinstance(data, dict):
        data['internships'] = items
        payload = data
    else:
        payload = items
    yaml_path.write_text(yaml.dump(payload, allow_unicode=True, sort_keys=False), encoding='utf-8')
    print(f'added={added} total={len(items)}')


if __name__ == '__main__':
    main()
