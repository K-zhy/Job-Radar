# YAML Schema — internships.yaml

```yaml
internships:
  - title: "AI Agent 开发工程师"
    company: "示例科技"
    salary: "20-30K"
    location: "北京·朝阳区"
    experience: "1-3年"
    education: "本科"
    company_size: "20-99人"
    funding_stage: "A轮"
    tags:
      - Python
      - LangChain
      - Agent开发
      - LLM
      - 转正机会
    jd_summary: "在团队指导下参与AI Agent算法研发与评测，支持数据分析"
    jd_full: "（完整JD原文，不做摘要）"
    url: "https://www.zhipin.com/job_detail/xxx.html"
    source: "boss直聘"
    status: "pending"
    jd_quality: "good"
    collected_at: "2026-02-24"
    notion_page_id: ""
```

## Field Notes

- `salary`: keep as raw string from platform, e.g. "20-30K·13薪" or "200元/天"
- `tags`: array of short strings, 3-10 items
- `jd_summary`: 30-50字摘要（优先由轻隔离总结子Agent生成）
- `jd_full`: full raw text, can be long; do not summarize/truncate when collecting
- `company_size`: raw string from BOSS, e.g. "20-99人" / "100-499人"
- `funding_stage`: raw string from BOSS, e.g. "A轮" / "未融资" / "天使轮"
- `jd_quality`: one of `good / unclear / skip`
  - `good`: JD ≥200字 且技术关键词 ≥3 个
  - `unclear`: JD 偏短或技术描述模糊
  - `skip`: 非技术岗 / 外包 / 地点不合适 / 学历门槛过高
- `status`: one of `pending / applied / interviewing / offered / rejected / ghosted`
- `collected_at`: ISO date string YYYY-MM-DD
- `notion_page_id`: Notion page ID if synced, empty string if not
- Dedup key: `company` + `title` combination
