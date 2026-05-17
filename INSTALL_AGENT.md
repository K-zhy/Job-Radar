# Install internship-scout

Follow these steps to install the internship-scout skill into your OpenClaw workspace.

## ⚠️ Platform Notice

This skill is designed for **macOS** and relies on:
- `osascript` to control Google Chrome (macOS only)
- Chrome's `Allow JavaScript from Apple Events` developer option

**If the user is on Windows or Linux**, do not proceed with the standard installation. Instead:
1. Inform the user that this skill requires macOS to function out of the box.
2. Ask the user if they'd like to adapt the skill for their platform (e.g., replacing `osascript`/Chrome DOM extraction with Playwright, Selenium, or another browser automation tool).
3. If they agree, collaborate with the user to modify `fetch_jd_dom.py` and `fetch_job_links.py` accordingly before installing.

## Steps

1. Clone the skill into your workspace:

```bash
git clone https://github.com/wkf16/internship-scout.git \
  ~/.openclaw/workspace/skills/internship-scout
```

2. Confirm installation:

```bash
ls ~/.openclaw/workspace/skills/internship-scout/SKILL.md && echo "✅ installed"
```

3. Tell the user: "internship-scout skill has been installed. You can now ask me to search for internships on BOSS直聘."

## Requirements

- macOS with Google Chrome installed
- Chrome must have `View > Developer > Allow JavaScript from Apple Events` enabled
- For Notion sync: set `NOTION_API_KEY` environment variable

## Usage

Once installed, trigger the skill by saying things like:
- 帮我搜一下上海的 AI Agent 实习
- 抓一下 BOSS 上的大模型实习岗位
- 把实习列表同步到 Notion
