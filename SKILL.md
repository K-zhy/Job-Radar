---
name: job-radar
description: Help the user start, verify, and troubleshoot the local Job Radar project. Use when the user wants to install dependencies, run the web console, confirm the access URL, or diagnose startup failures.
---

# Job Radar 启动助手

## 目标

这个技能只做一件事：帮助用户把当前仓库里的 Job Radar 在本地启动起来，并确认页面可以访问。

默认优先级：
1. 检查依赖
2. 启动服务
3. 验证页面
4. 定位启动失败原因

## 何时使用

- 用户说“启动项目”“把这个仓库跑起来”“打开本地页面”“看为什么启动失败”
- 用户需要确认依赖、端口、启动命令、访问地址
- 用户修改了后端或前端后，需要重新启动并验证

## 最短启动路径

```bash
python3 -m pip install -r requirements.txt
python3 app.py --host 127.0.0.1 --port 8765
```

浏览器访问 `http://127.0.0.1:8765`

## 启动前检查

```bash
python3 --version
python3 -m pip show PyYAML aiohttp markitdown
```

如果缺依赖，直接执行：

```bash
python3 -m pip install -r requirements.txt
```

## 启动成功标准

- 终端输出 `Job Radar running at http://127.0.0.1:8765`
- 首页可以打开
- `GET /app.js`、`GET /styles.css` 返回 `200`
- Web 页面能正常显示岗位库与简历分析页

## 项目入口

- `app.py`：本地 Web 服务入口
- `web/`：前端页面、样式和交互逻辑
- `requirements.txt`：Python 依赖
- `internships.yaml`：岗位数据
- `internship-prefs.yaml`：偏好配置
- `resume.md` / `resume_advice.json`：简历分析缓存

## 常用验证命令

```bash
curl -I http://127.0.0.1:8765
curl -s http://127.0.0.1:8765/api/resume/current
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

## 常见问题

- 端口占用：先检查 `8765`，再结束旧进程或改用其他端口
- 缺少依赖：重新执行 `python3 -m pip install -r requirements.txt`
- 修改 `app.py` 后页面没变化：后端必须重启
- JD 抓取失败：仅支持 macOS + Chrome，并且 Chrome 需要开启 `Allow JavaScript from Apple Events`
- 简历页为空：确认项目根目录是否存在 `resume.md`，重新上传会覆盖旧缓存

## Agent 行为要求

- 先把项目启动起来，再考虑功能扩展或重构
- 启动失败时先给出明确 blocker，不要泛泛描述
- 做过后端修改后，必须重启服务再验证
- 回复里要明确告诉用户访问地址和当前状态
