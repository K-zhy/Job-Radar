# CLAUDE.md

这个文件用于告诉 Claude Code 在这个仓库里应该如何工作。

## 首要目标

- 这个仓库的首要任务是把 Job Radar 启动起来并保持可验证。
- 如果用户说“启动项目”“把页面跑起来”“看看服务是否正常”，默认流程是：
  1. 安装依赖
  2. 启动 `app.py`
  3. 验证 `http://127.0.0.1:8765`

## 推荐命令

```bash
python3 -m pip install -r requirements.txt
python3 app.py --host 127.0.0.1 --port 8765
curl -I http://127.0.0.1:8765
curl -s http://127.0.0.1:8765/api/resume/current
python3 -m py_compile app.py
```

如果用户要跑数据抓取或补抓流程，可以再用：

```bash
python3 scripts/fetch_job_links.py --prefs internship-prefs.yaml --yaml internships.yaml
python3 scripts/fetch_jd_dom.py --yaml internships.yaml --limit 5 --min-delay 2.0 --max-delay 5.0
python3 scripts/summarize_jds.py --list-pending
python3 scripts/dedup_check.py --yaml internships.yaml
```

## 代码结构

- `app.py`：后端入口，负责 HTTP 服务、SQLite、NL2SQL、简历分析接口
- `web/`：前端页面与交互逻辑
- `scripts/`：职位抓取、JD 提取、摘要生成、去重等脚本
- `references/`：偏好模板与字段说明
- `requirements.txt`：依赖清单

## 启动与调试约定

- 修改 `app.py` 后必须重启服务，前端刷新不会自动带上新的后端逻辑
- 默认端口是 `8765`；如果端口占用，先查占用进程，再决定是否切端口
- 启动成功的最低标准是首页能打开，且 `/app.js`、`/styles.css` 返回 `200`
- 简历分析页会读取项目根目录的 `resume.md` 和 `resume_advice.json` 作为本地缓存

## 环境前提

- JD DOM 抓取仅支持 macOS + Google Chrome
- Chrome 需要开启 `View > Developer > Allow JavaScript from Apple Events`
- `markitdown[all]` 用于简历文件转 Markdown，如果缺失会影响简历上传

## 协作要求

- 与用户统一使用中文沟通
- 优先解决“启动、验证、排障”问题，再做功能修改
- 不要随意改动与当前任务无关的脚本或数据文件
- 对外说明以本地 Web 控制台为主，不再把 Notion 看板作为当前主路径