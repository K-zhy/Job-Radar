#!/usr/bin/env python3
"""
dedup_check.py — 用编辑距离查询 internships.yaml 中的重复条目

用法：
  python3 dedup_check.py --company "示例科技" --title "AI工程师" --salary "20-30K" --location "北京" --jd "负责LLM开发..."
  python3 dedup_check.py --company "示例科技" --title "AI工程师"   # 只匹配公司+岗位
  python3 dedup_check.py --threshold 0.2                          # 调整相似度阈值（默认0.25）

输出：
  匹配到重复条目时，打印 index（在 internships 列表中的位置）和相似度分数，退出码 0
  未找到重复时，退出码 1
"""

import sys
import argparse
import yaml

YAML_PATH = "/Users/okonfu/.openclaw/workspace/internships.yaml"


def edit_distance(a: str, b: str) -> int:
    """标准 Levenshtein 编辑距离"""
    a, b = a.lower(), b.lower()
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            temp = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]


def similarity(a: str, b: str) -> float:
    """归一化相似度：0.0（完全不同）~ 1.0（完全相同）"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    max_len = max(len(a), len(b))
    return 1.0 - edit_distance(a, b) / max_len


def field_sim(entry: dict, key: str, query: str) -> float:
    """取 entry 中某字段与 query 的相似度，字段不存在时返回 1.0（不参与判断）"""
    if not query:
        return 1.0
    val = str(entry.get(key, "") or "")
    return similarity(val, query)


def main():
    parser = argparse.ArgumentParser(description="internships.yaml 模糊去重查询")
    parser.add_argument("--company",   default="", help="公司名")
    parser.add_argument("--title",     default="", help="岗位名称")
    parser.add_argument("--salary",    default="", help="薪资字符串")
    parser.add_argument("--location",  default="", help="工作地点")
    parser.add_argument("--jd",        default="", help="JD 文本（可传 jd_full 或 jd_summary）")
    parser.add_argument("--threshold", type=float, default=0.25,
                        help="各字段平均距离率阈值，低于此值视为重复（默认 0.25）")
    parser.add_argument("--yaml",      default=YAML_PATH, help="YAML 文件路径")
    args = parser.parse_args()

    try:
        with open(args.yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"[dedup_check] 文件不存在: {args.yaml}", file=sys.stderr)
        sys.exit(2)

    entries = data.get("internships", [])
    if not entries:
        print("[dedup_check] YAML 中暂无条目")
        sys.exit(1)

    # 权重：公司+岗位最重要，JD 次之，薪资/地点辅助
    weights = {
        "company":  0.30,
        "title":    0.30,
        "salary":   0.15,
        "location": 0.10,
        "jd":       0.15,
    }

    results = []
    for idx, entry in enumerate(entries):
        jd_val = str(entry.get("jd_full", "") or entry.get("jd_summary", "") or "")
        sims = {
            "company":  field_sim(entry, "company",  args.company),
            "title":    field_sim(entry, "title",    args.title),
            "salary":   field_sim(entry, "salary",   args.salary),
            "location": field_sim(entry, "location", args.location),
            "jd":       similarity(jd_val, args.jd) if args.jd else 1.0,
        }
        # 加权平均相似度
        score = sum(sims[k] * weights[k] for k in weights)
        # 距离率 = 1 - score；低于阈值 → 重复
        dist_rate = 1.0 - score
        if dist_rate < args.threshold:
            results.append((idx, score, dist_rate, entry))

    if not results:
        print("[dedup_check] 未找到重复条目")
        sys.exit(1)

    results.sort(key=lambda x: x[1], reverse=True)  # 相似度高的排前面
    print(f"[dedup_check] 找到 {len(results)} 条疑似重复：\n")
    for idx, score, dist_rate, entry in results:
        print(f"  index={idx}  相似度={score:.3f}  距离率={dist_rate:.3f}")
        print(f"    公司: {entry.get('company', '')}  岗位: {entry.get('title', '')}")
        print(f"    薪资: {entry.get('salary', '')}  地点: {entry.get('location', '')}")
        print(f"    状态: {entry.get('status', '')}  收录: {entry.get('collected_at', '')}")
        print()
    sys.exit(0)


if __name__ == "__main__":
    main()
