const STATUS_OPTIONS = ['pending', 'applied', 'interviewing', 'offered', 'rejected', 'ghosted'];
const JOB_TABLE_WIDTH_STORAGE_KEY = 'job-radar.jobs-table-widths';

// ── Markdown renderer ──────────────────────────────────────────────────────
function renderMarkdown(text) {
  const renderer = globalThis.marked;
  if (!renderer || typeof renderer.parse !== 'function') return escapeHtml(text);
  try {
    return renderer.parse(String(text || ''), { breaks: true, gfm: true });
  } catch {
    return escapeHtml(text);
  }
}

const JOB_TABLE_COLUMNS = [
  { key: 'select', min: 56, max: 120, defaultWidth: 68, fontMin: 0.76, fontMax: 0.84 },
  { key: 'company', min: 140, max: 420, defaultWidth: 180, fontMin: 0.8, fontMax: 1.02 },
  { key: 'title', min: 160, max: 480, defaultWidth: 220, fontMin: 0.8, fontMax: 1.04 },
  { key: 'salary', min: 92, max: 180, defaultWidth: 118, fontMin: 0.78, fontMax: 0.96 },
  { key: 'location', min: 96, max: 200, defaultWidth: 118, fontMin: 0.78, fontMax: 0.96 },
  { key: 'jd', min: 180, max: 720, defaultWidth: 340, fontMin: 0.78, fontMax: 0.98 },
  { key: 'link', min: 84, max: 180, defaultWidth: 92, fontMin: 0.76, fontMax: 0.9 },
  { key: 'status', min: 108, max: 200, defaultWidth: 120, fontMin: 0.76, fontMax: 0.9 },
  { key: 'applied', min: 96, max: 180, defaultWidth: 108, fontMin: 0.76, fontMax: 0.9 },
  { key: 'action', min: 96, max: 180, defaultWidth: 108, fontMin: 0.76, fontMax: 0.9 },
];

let jobsTableResizeInitialized = false;

const state = {
  jobs: [],
  summary: {},
  pagination: {
    page: 1,
    pageSize: 50,
    totalItems: 0,
    totalPages: 1,
    hasPrev: false,
    hasNext: false,
  },
  prefs: null,
  apiConfig: null,
  lastSql: '',
  fetchStatus: null,
  fetchPollTimer: null,
  selectedJobIds: new Set(),
  resumeSessionId: '',
};

const jobsTableBody = document.querySelector('#jobsTableBody');
const fetchLog = document.querySelector('#fetchLog');
const analysisResult = document.querySelector('#analysisResult');
const toast = document.querySelector('#toast');

document.addEventListener('DOMContentLoaded', async () => {
  bindTabs();
  bindHeroLinks();
  bindJobsFilters();
  bindPrefsActions();
  bindApiActions();
  bindAnalysisActions();
  bindResumeActions();
  initJobsTableInteractions();

  await Promise.all([loadJobs(), loadPrefs(), loadApiConfig(), loadFetchStatus(), loadCurrentResume()]);
});

function bindTabs() {
  document.querySelectorAll('[data-tab]').forEach((button) => {
    button.addEventListener('click', () => activateTab(button.dataset.tab));
  });
}

function bindHeroLinks() {
  document.querySelectorAll('[data-jump]').forEach((button) => {
    button.addEventListener('click', () => activateTab(button.dataset.jump));
  });
}

function activateTab(tab) {
  document.querySelectorAll('.tab-button').forEach((button) => {
    button.classList.toggle('is-active', button.dataset.tab === tab);
  });
  document.querySelectorAll('.tab-panel').forEach((panel) => {
    panel.classList.toggle('is-active', panel.dataset.panel === tab);
  });
}

function bindJobsFilters() {
  document.querySelector('#jobsFilterForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    state.pagination.page = 1;
    await loadJobs();
  });

  document.querySelector('#resetFiltersButton').addEventListener('click', async () => {
    document.querySelector('#filterKeyword').value = '';
    document.querySelector('#filterStatus').value = '';
    document.querySelector('#filterLocation').value = '';
    document.querySelector('#filterApplied').value = '';
    state.pagination.page = 1;
    await loadJobs();
  });

  document.querySelector('#reloadJobsButton').addEventListener('click', loadJobs);
  document.querySelector('#pageSizeSelect').addEventListener('change', async (event) => {
    state.pagination.pageSize = Number(event.target.value || 50);
    state.pagination.page = 1;
    await loadJobs();
  });
  document.querySelector('#prevPageButton').addEventListener('click', async () => changeJobsPage(-1));
  document.querySelector('#nextPageButton').addEventListener('click', async () => changeJobsPage(1));
  document.querySelector('#prevPageButtonBottom').addEventListener('click', async () => changeJobsPage(-1));
  document.querySelector('#nextPageButtonBottom').addEventListener('click', async () => changeJobsPage(1));
  document.querySelector('#selectAllJobsCheckbox').addEventListener('change', toggleSelectAllJobs);
  document.querySelector('#bulkDeleteButton').addEventListener('click', bulkDeleteJobs);
  document.querySelector('#toggleManualJobButton').addEventListener('click', () => {
    toggleManualJobPanel(document.querySelector('#manualJobPanel').hidden);
  });
  document.querySelector('#manualJobForm').addEventListener('submit', submitManualJob);
  document.querySelector('#cancelManualJobButton').addEventListener('click', () => {
    resetManualJobForm();
    toggleManualJobPanel(false);
  });
  document.querySelector('#syncJobsButton').addEventListener('click', async () => {
    try {
      setButtonBusy(document.querySelector('#syncJobsButton'), true);
      const result = await api('/api/sync');
      showToast(`已从 YAML 同步，当前共 ${result.db_count} 条岗位。`);
      await loadJobs();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      setButtonBusy(document.querySelector('#syncJobsButton'), false);
    }
  });
}

function bindPrefsActions() {
  document.querySelector('#savePrefsButton').addEventListener('click', async () => {
    await savePrefs();
  });
  document.querySelector('#prefsJobType').addEventListener('change', syncPrefsSalaryInputsForJobType);

  document.querySelector('#runFetchButton').addEventListener('click', async () => {
    const button = document.querySelector('#runFetchButton');
    try {
      setButtonBusy(button, true);
      await savePrefs(true);
      const payload = {
        include_jd_fetch: document.querySelector('#fetchIncludeJd').checked,
        include_summary: document.querySelector('#fetchIncludeSummary').checked,
        job_limit: Number(document.querySelector('#fetchJobLimit').value || 0),
        jd_limit: Number(document.querySelector('#fetchJdLimit').value || 10),
        summary_limit: Number(document.querySelector('#fetchSummaryLimit').value || 10),
      };
      const result = await api('/api/fetch', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      renderFetchStatus(result);
      startFetchPolling();
      showToast('抓取任务已启动，当前页面会停留在控制台。');
    } catch (error) {
      fetchLog.textContent = error.message;
      showToast(error.message, true);
    } finally {
      if (!['running', 'cancelling'].includes(state.fetchStatus?.status)) {
        setButtonBusy(button, false);
      }
    }
  });

  document.querySelector('#stopFetchButton').addEventListener('click', stopFetch);
}

async function loadCurrentResume() {
  try {
    const payload = await api('/api/resume/current');
    if (payload.session_id) {
      state.resumeSessionId = payload.session_id;
      
      const status = document.querySelector('#resumeFileStatus');
      if (status) {
        status.textContent = payload.resume_filename ? `已加载缓存：${payload.resume_filename}` : '已加载缓存简历';
      }
      
      const snapshot = document.querySelector('#resumeSnapshot');
      if (snapshot) {
        snapshot.hidden = false;
      }
      
      document.querySelector('#resumeMarkdownPreview').innerHTML = renderMarkdown(payload.markdown_preview || '');
      document.querySelector('#resumeMarkdownPreview').dataset.raw = payload.markdown_preview || '';
      renderResumeMatchedJobs(payload.matched_jobs || []);
      
      const chatMessages = document.querySelector('#resumeChatMessages');
      if (chatMessages) {
        chatMessages.innerHTML = '';
        const history = payload.history || [];
        // Show all history including the initial advice (history[0]) as first assistant message
        for (let i = 0; i < history.length; i++) {
          const item = history[i];
          if (item.role === 'user' || item.role === 'assistant') {
            appendResumeChatMessage(item.role, item.content);
          }
        }
        // scroll to bottom
        chatMessages.scrollTop = chatMessages.scrollHeight;
      }
    }
  } catch (error) {
    // silently fail for cached load
    console.error('Failed to load cached resume:', error);
  }
}

function bindResumeActions() {
  const uploadButton = document.querySelector('#uploadResumeButton');
  const resetButton = document.querySelector('#resetResumeButton');
  const sendButton = document.querySelector('#resumeChatSendButton');
  const input = document.querySelector('#resumeChatInput');
  const copyButton = document.querySelector('#copyResumeMarkdownButton');

  const fileInput = document.querySelector('#resumeFile');
  fileInput?.addEventListener('change', () => {
    const file = fileInput.files[0];
    const status = document.querySelector('#resumeFileStatus');
    if (status) {
      if (file) {
        status.textContent = file.name;
        status.title = file.name;
      } else {
        status.textContent = '未选择文件';
        status.title = '';
      }
    }
  });

  uploadButton?.addEventListener('click', uploadResume);
  resetButton?.addEventListener('click', resetResumeSession);
  sendButton?.addEventListener('click', sendResumeChat);
  input?.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      sendResumeChat();
    }
  });
  copyButton?.addEventListener('click', async () => {
    const preview = document.querySelector('#resumeMarkdownPreview').dataset.raw || '';
    if (!preview.trim()) {
      showToast('没有可复制的内容', true);
      return;
    }
    try {
      await navigator.clipboard.writeText(preview);
      showToast('已复制 Markdown 预览');
    } catch (error) {
      showToast(error.message, true);
    }
  });

  resetResumeSession();
}

function resetResumeSession() {
  state.resumeSessionId = '';
  const fileInput = document.querySelector('#resumeFile');
  const status = document.querySelector('#resumeUploadStatus');
  const snapshot = document.querySelector('#resumeSnapshot');
  const messages = document.querySelector('#resumeChatMessages');
  const chatInput = document.querySelector('#resumeChatInput');

  if (fileInput) {
    fileInput.value = '';
  }
  if (status) {
    status.textContent = '未上传';
  }
  if (snapshot) {
    snapshot.hidden = true;
  }
  if (messages) {
    messages.innerHTML = '';
  }
  if (chatInput) {
    chatInput.value = '';
  }
}

function appendResumeChatMessage(role, content) {
  const container = document.querySelector('#resumeChatMessages');
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role}`;
  if (role === 'assistant' && content) {
    bubble.innerHTML = renderMarkdown(content);
    bubble.dataset.raw = content;
  } else {
    bubble.textContent = content;
  }
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
  return bubble;
}

function appendSqlResultCard(data, replaceEl) {
  const container = document.querySelector('#resumeChatMessages');
  const card = document.createElement('div');
  card.className = 'chat-sql-card';

  const head = document.createElement('div');
  head.className = 'chat-sql-card-head';
  head.innerHTML =
    `<span class="chip chat-sql-badge">NL2SQL</span>` +
    `<strong>${escapeHtml(data.sql_question || '岗位库查询')}</strong>` +
    `<span class="micro-copy">${data.row_count ?? 0} 行结果</span>`;
  card.appendChild(head);

  if (data.sql) {
    const code = document.createElement('div');
    code.className = 'chat-sql-code';
    code.textContent = data.sql;
    card.appendChild(code);
  }

  if (data.columns?.length && data.rows?.length) {
    const shell = document.createElement('div');
    shell.className = 'chat-sql-table-shell';
    const table = document.createElement('table');
    table.className = 'chat-sql-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    data.columns.forEach((col) => {
      const th = document.createElement('th');
      th.textContent = col;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    data.rows.forEach((row) => {
      const tr = document.createElement('tr');
      (Array.isArray(row) ? row : data.columns.map((c) => row[c])).forEach((val) => {
        const td = document.createElement('td');
        td.textContent = val ?? '';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    shell.appendChild(table);
    card.appendChild(shell);
  } else {
    const empty = document.createElement('div');
    empty.className = 'chat-sql-card-head micro-copy';
    empty.textContent = '查询无结果';
    card.appendChild(empty);
  }

  if (replaceEl && replaceEl.parentNode) {
    replaceEl.parentNode.replaceChild(card, replaceEl);
  } else {
    container.appendChild(card);
  }
  container.scrollTop = container.scrollHeight;
}

function escapeHtml(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderResumeMatchedJobs(jobs = []) {
  const container = document.querySelector('#resumeMatchedJobs');
  if (!container) {
    return;
  }
  container.innerHTML = '';
  if (!jobs.length) {
    container.textContent = '未找到明显匹配的岗位，可在对话窗口补充你想投的方向/城市。';
    return;
  }
  jobs.forEach((job) => {
    const item = document.createElement('div');
    item.className = 'resume-job';
    const left = document.createElement('div');
    left.className = 'resume-job-main';
    const title = document.createElement('strong');
    title.textContent = `${job.company || ''} | ${job.title || ''}`.trim();
    const meta = document.createElement('div');
    meta.className = 'micro-copy';
    meta.textContent = `${job.location || ''} ${job.salary || ''} 匹配分 ${job.match_score ?? 0}`.trim();
    left.appendChild(title);
    left.appendChild(meta);
    item.appendChild(left);

    const link = document.createElement('a');
    link.className = 'button ghost tiny';
    link.textContent = '打开';
    link.href = job.url || '#';
    link.target = '_blank';
    link.rel = 'noreferrer';
    item.appendChild(link);
    container.appendChild(item);
  });
}

async function uploadResume() {
  const fileInput = document.querySelector('#resumeFile');
  const uploadButton = document.querySelector('#uploadResumeButton');
  const status = document.querySelector('#resumeUploadStatus');
  const snapshot = document.querySelector('#resumeSnapshot');

  const file = fileInput?.files?.[0];
  if (!file) {
    showToast('请先选择一个简历文件', true);
    return;
  }
  const form = new FormData();
  form.append('file', file, file.name);

  try {
    setButtonBusy(uploadButton, true);
    if (status) {
      status.textContent = '上传中…';
    }
    const response = await fetch('/api/resume/upload', { method: 'POST', body: form });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || '上传失败');
    }
    state.resumeSessionId = payload.session_id || '';
    if (status) {
      status.textContent = payload.resume_filename ? `已上传：${payload.resume_filename}` : '已上传';
    }
    if (snapshot) {
      snapshot.hidden = false;
    }
    document.querySelector('#resumeMarkdownPreview').innerHTML = renderMarkdown(payload.markdown_preview || '');
    document.querySelector('#resumeMarkdownPreview').dataset.raw = payload.markdown_preview || '';
    renderResumeMatchedJobs(payload.matched_jobs || []);

    const messages = document.querySelector('#resumeChatMessages');
    if (messages) {
      messages.innerHTML = '';
      // Show initial advice as first assistant message in chat
      if (payload.advice) {
        appendResumeChatMessage('assistant', payload.advice);
      }
    }
    showToast('简历已解析，可在右侧继续追问。');
  } catch (error) {
    if (status) {
      status.textContent = '上传失败';
    }
    showToast(error.message, true);
  } finally {
    setButtonBusy(uploadButton, false);
  }
}

async function sendResumeChat() {
  const button = document.querySelector('#resumeChatSendButton');
  const input = document.querySelector('#resumeChatInput');
  const message = String(input?.value || '').trim();
  if (!state.resumeSessionId) {
    showToast('请先上传简历再开始对话', true);
    return;
  }
  if (!message) {
    showToast('请输入要提问的内容', true);
    return;
  }
  appendResumeChatMessage('user', message);
  input.value = '';
  
  let assistantBubble = null;
  let systemBubble = null;

  const handleStreamEvent = (event) => {
    const container = document.querySelector('#resumeChatMessages');

    if (event.type === 'status') {
      if (!systemBubble) systemBubble = appendResumeChatMessage('system', '');
      systemBubble.textContent = event.content;
    } else if (event.type === 'system') {
      if (!systemBubble) systemBubble = appendResumeChatMessage('system', '');
      systemBubble.textContent = event.content;
    } else if (event.type === 'sql_result') {
      appendSqlResultCard(event.content, systemBubble);
      systemBubble = null;
    } else if (event.type === 'start') {
      assistantBubble = appendResumeChatMessage('assistant', '');
      assistantBubble.classList.add('is-streaming');
    } else if (event.type === 'chunk') {
      if (!assistantBubble) {
        assistantBubble = appendResumeChatMessage('assistant', '');
        assistantBubble.classList.add('is-streaming');
      }
      // 流式期间用 textContent（快），完成后统一渲染 MD
      const raw = (assistantBubble.dataset.raw || '') + event.content;
      assistantBubble.dataset.raw = raw;
      assistantBubble.textContent = raw;
      container.scrollTop = container.scrollHeight;
    } else if (event.type === 'error') {
      if (!assistantBubble) assistantBubble = appendResumeChatMessage('assistant', '');
      assistantBubble.classList.remove('is-streaming');
      assistantBubble.textContent += event.content;
    } else if (event.type === 'done') {
      if (assistantBubble) {
        assistantBubble.classList.remove('is-streaming');
        // 流结束后把纯文本重渲染为 Markdown HTML
        const raw = assistantBubble.dataset.raw || assistantBubble.textContent;
        assistantBubble.innerHTML = renderMarkdown(raw);
        container.scrollTop = container.scrollHeight;
      }
    }
  };
  
  try {
    setButtonBusy(button, true);
    const response = await fetch('/api/resume/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.resumeSessionId,
        message,
        llm_config: {
          base_url: state.apiConfig?.llm?.base_url || '',
          model: state.apiConfig?.llm?.model || '',
          api_key: state.apiConfig?.llm?.api_key || '',
        },
      })
    });
    
    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.error || '请求失败');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        break;
      }
      
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          handleStreamEvent(JSON.parse(line));
        } catch (e) {
          console.error('Failed to parse NDJSON line:', line, e);
        }
      }
    }

    if (buffer.trim()) {
      try {
        handleStreamEvent(JSON.parse(buffer));
      } catch (e) {
        console.error('Failed to parse trailing NDJSON line:', buffer, e);
      }
    }
  } catch (error) {
    if (assistantBubble) assistantBubble.classList.remove('is-streaming');
    appendResumeChatMessage('system', `发生错误: ${error.message}`);
    showToast(error.message, true);
  } finally {
    if (assistantBubble) assistantBubble.classList.remove('is-streaming');
    setButtonBusy(button, false);
  }
}

function toggleManualJobPanel(visible) {
  const panel = document.querySelector('#manualJobPanel');
  const toggleButton = document.querySelector('#toggleManualJobButton');
  panel.hidden = !visible;
  toggleButton.textContent = visible ? '收起手动添加' : '手动添加岗位';
}

function resetManualJobForm() {
  document.querySelector('#manualJobForm').reset();
  document.querySelector('#manualJobType').value = '全职';
}

function collectManualJobFromForm() {
  return {
    company: document.querySelector('#manualJobCompany').value.trim(),
    title: document.querySelector('#manualJobTitle').value.trim(),
    salary: document.querySelector('#manualJobSalary').value.trim(),
    location: document.querySelector('#manualJobLocation').value.trim(),
    company_size: document.querySelector('#manualJobCompanySize').value.trim(),
    job_type: document.querySelector('#manualJobType').value,
    url: document.querySelector('#manualJobUrl').value.trim(),
    jd_full: document.querySelector('#manualJobJdFull').value.trim(),
  };
}

async function submitManualJob(event) {
  event.preventDefault();
  const button = document.querySelector('#submitManualJobButton');

  try {
    setButtonBusy(button, true);
    const job = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify(collectManualJobFromForm()),
    });
    resetManualJobForm();
    toggleManualJobPanel(false);
    state.pagination.page = 1;
    await loadJobs();
    showToast(`已添加 ${job.company} | ${job.title}`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setButtonBusy(button, false);
  }
}

function syncPrefsSalaryInputsForJobType() {
  const jobType = document.querySelector('#prefsJobType').value;
  const minDayInput = document.querySelector('#prefsMinDay');
  const isInternship = jobType === '实习';
  minDayInput.disabled = !isInternship;
  minDayInput.title = isInternship ? '' : '全职/校招抓取不会使用最低日薪过滤';
}

function bindApiActions() {
  document.querySelector('#saveApiConfigButton').addEventListener('click', async () => {
    await saveApiConfig();
  });
}

function bindAnalysisActions() {
  document.querySelectorAll('.quick-prompt').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelector('#analysisQuestion').value = button.textContent.trim();
    });
  });

  document.querySelector('#runAnalysisButton').addEventListener('click', runAnalysis);
  document.querySelector('#clearAnalysisButton').addEventListener('click', clearAnalysis);
  document.querySelector('#goApiConfigButton').addEventListener('click', () => activateTab('apis'));
  document.querySelector('#copySqlButton').addEventListener('click', async () => {
    if (!state.lastSql) {
      return;
    }
    try {
      await navigator.clipboard.writeText(state.lastSql);
      showToast('SQL 已复制到剪贴板。');
    } catch (error) {
      showToast('复制失败，请手动复制。', true);
    }
  });
}

async function loadJobs() {
  const params = new URLSearchParams();
  const keyword = document.querySelector('#filterKeyword').value.trim();
  const status = document.querySelector('#filterStatus').value;
  const location = document.querySelector('#filterLocation').value.trim();
  const applied = document.querySelector('#filterApplied').value;

  if (keyword) {
    params.set('q', keyword);
  }
  if (status) {
    params.set('status', status);
  }
  if (location) {
    params.set('location', location);
  }
  if (applied) {
    params.set('applied', applied);
  }
  params.set('page', String(state.pagination.page || 1));
  params.set('page_size', String(state.pagination.pageSize || 50));

  try {
    const data = await api(`/api/jobs?${params.toString()}`);
    state.jobs = data.items || [];
    state.summary = data.summary || {};
    state.pagination = data.pagination || state.pagination;
    renderHeroStats();
    renderJobsTable();
  } catch (error) {
    jobsTableBody.innerHTML = '';
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 10;
    cell.textContent = error.message;
    row.appendChild(cell);
    jobsTableBody.appendChild(row);
    showToast(error.message, true);
  }
}

function initJobsTableInteractions() {
  const table = document.querySelector('#jobsTable');
  if (!table) {
    return;
  }

  if (!jobsTableResizeInitialized) {
    const headers = table.querySelectorAll('thead th');
    headers.forEach((th, index) => {
      const config = JOB_TABLE_COLUMNS[index];
      if (!config || th.querySelector('.jobs-header-cell')) {
        return;
      }

      const labelText = th.textContent.trim();
      th.textContent = '';

      const wrapper = document.createElement('div');
      wrapper.className = 'jobs-header-cell';

      const label = document.createElement('span');
      label.className = 'jobs-header-label';
      label.textContent = labelText;

      const handle = document.createElement('span');
      handle.className = 'column-resizer';
      handle.dataset.columnKey = config.key;
      handle.title = `拖动调整${labelText}列宽`;

      wrapper.appendChild(label);
      wrapper.appendChild(handle);
      th.appendChild(wrapper);
    });

    bindJobsTableResizeHandles();
    window.addEventListener('resize', updateJobsTableTypography);
    jobsTableResizeInitialized = true;
  }

  applyJobsTableColumnWidths();
}

function bindJobsTableResizeHandles() {
  document.querySelectorAll('#jobsTable .column-resizer').forEach((handle) => {
    handle.addEventListener('mousedown', (event) => {
      const key = event.currentTarget.dataset.columnKey;
      const config = JOB_TABLE_COLUMNS.find((column) => column.key === key);
      if (!config) {
        return;
      }

      const startX = event.clientX;
      const startWidth = getJobsTableColumnWidth(key, config.defaultWidth);
      document.body.classList.add('is-column-resizing');

      const onMove = (moveEvent) => {
        const nextWidth = clampNumber(startWidth + moveEvent.clientX - startX, config.min, config.max);
        setJobsTableColumnWidth(key, nextWidth);
      };

      const onUp = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        document.body.classList.remove('is-column-resizing');
        persistJobsTableWidths();
      };

      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      event.preventDefault();
    });
  });
}

function readStoredJobsTableWidths() {
  try {
    const raw = localStorage.getItem(JOB_TABLE_WIDTH_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (error) {
    console.warn('Failed to restore jobs table widths', error);
    return {};
  }
}

function applyJobsTableColumnWidths() {
  const table = document.querySelector('#jobsTable');
  if (!table) {
    return;
  }

  const storedWidths = readStoredJobsTableWidths();
  JOB_TABLE_COLUMNS.forEach((config) => {
    const nextWidth = clampNumber(Number(storedWidths[config.key]) || config.defaultWidth, config.min, config.max);
    table.style.setProperty(`--jobs-col-${config.key}`, `${nextWidth}px`);
  });
  updateJobsTableTypography();
}

function persistJobsTableWidths() {
  const widths = {};
  JOB_TABLE_COLUMNS.forEach((config) => {
    widths[config.key] = getJobsTableColumnWidth(config.key, config.defaultWidth);
  });
  localStorage.setItem(JOB_TABLE_WIDTH_STORAGE_KEY, JSON.stringify(widths));
}

function getJobsTableColumnWidth(key, fallback) {
  const table = document.querySelector('#jobsTable');
  const columnIndex = JOB_TABLE_COLUMNS.findIndex((column) => column.key === key);
  if (!table || columnIndex === -1) {
    return fallback;
  }
  const header = table.querySelectorAll('thead th')[columnIndex];
  return Math.round(header?.getBoundingClientRect().width || 0) || fallback;
}

function setJobsTableColumnWidth(key, width) {
  const table = document.querySelector('#jobsTable');
  if (!table) {
    return;
  }
  table.style.setProperty(`--jobs-col-${key}`, `${Math.round(width)}px`);
  updateJobsTableTypography();
}

function updateJobsTableTypography() {
  const table = document.querySelector('#jobsTable');
  if (!table) {
    return;
  }

  JOB_TABLE_COLUMNS.forEach((config) => {
    const width = getJobsTableColumnWidth(config.key, config.defaultWidth);
    const ratio = clampNumber((width - config.min) / Math.max(1, config.max - config.min), 0, 1);
    const fontSize = config.fontMin + ratio * (config.fontMax - config.fontMin);
    table.style.setProperty(`--jobs-font-${config.key}`, `${fontSize.toFixed(3)}rem`);
  });
}

async function changeJobsPage(delta) {
  if ((delta < 0 && !state.pagination.hasPrev) || (delta > 0 && !state.pagination.hasNext)) {
    return;
  }
  state.pagination.page += delta;
  await loadJobs();
}

function renderHeroStats() {
  document.querySelectorAll('[data-stat]').forEach((node) => {
    const key = node.dataset.stat;
    node.textContent = state.summary[key] ?? 0;
  });
}

function renderJobsTable() {
  syncSelectionWithVisibleJobs();
  renderBulkToolbar();
  renderPaginationControls();
  jobsTableBody.innerHTML = '';
  if (!state.jobs.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 10;
    cell.textContent = '当前没有符合筛选条件的岗位。';
    row.appendChild(cell);
    jobsTableBody.appendChild(row);
    return;
  }

  state.jobs.forEach((job) => jobsTableBody.appendChild(buildJobRow(job)));
}

function renderPaginationControls() {
  const pagination = state.pagination;
  const summary = `第 ${pagination.page} / ${pagination.totalPages} 页，共 ${pagination.totalItems} 条`;
  document.querySelector('#pageSizeSelect').value = String(pagination.pageSize || 50);
  document.querySelector('#paginationSummary').textContent = summary;
  document.querySelector('#paginationSummaryBottom').textContent = summary;
  document.querySelector('#prevPageButton').disabled = !pagination.hasPrev;
  document.querySelector('#nextPageButton').disabled = !pagination.hasNext;
  document.querySelector('#prevPageButtonBottom').disabled = !pagination.hasPrev;
  document.querySelector('#nextPageButtonBottom').disabled = !pagination.hasNext;
}

function buildJobRow(job) {
  const row = document.createElement('tr');
  row.appendChild(buildSelectCell(job));
  row.appendChild(buildCompanyCell(job));
  row.appendChild(buildTitleCell(job));
  row.appendChild(buildTextCell(job.salary || '—'));
  row.appendChild(buildMetaCell(job));
  row.appendChild(buildJdCell(job));
  row.appendChild(buildLinkCell(job));
  row.appendChild(buildStatusCell(job));
  row.appendChild(buildAppliedCell(job));
  row.appendChild(buildActionCell(job));
  return row;
}

function buildSelectCell(job) {
  const td = document.createElement('td');
  td.className = 'select-cell';

  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = state.selectedJobIds.has(job.id);
  checkbox.addEventListener('change', () => {
    if (checkbox.checked) {
      state.selectedJobIds.add(job.id);
    } else {
      state.selectedJobIds.delete(job.id);
    }
    renderBulkToolbar();
  });

  td.appendChild(checkbox);
  return td;
}

function buildCompanyCell(job) {
  const td = document.createElement('td');
  td.className = 'company-cell';

  const name = document.createElement('div');
  name.className = 'company-name';
  name.textContent = job.company;
  td.appendChild(name);

  const sub = document.createElement('div');
  sub.className = 'muted';
  sub.textContent = [job.company_size, job.funding_stage].filter(Boolean).join(' · ') || '信息待补充';
  td.appendChild(sub);

  if (job.jd_quality) {
    const badge = document.createElement('span');
    badge.className = `quality-badge quality-${job.jd_quality}`;
    badge.textContent = `JD ${job.jd_quality}`;
    td.appendChild(badge);
  }
  return td;
}

function buildTitleCell(job) {
  const td = document.createElement('td');
  td.className = 'title-cell';

  const title = document.createElement('div');
  title.className = 'job-name';
  title.textContent = job.title;
  td.appendChild(title);

  const sub = document.createElement('div');
  sub.className = 'muted';
  sub.textContent = [job.job_type, job.collected_at].filter(Boolean).join(' · ') || '未记录';
  td.appendChild(sub);

  return td;
}

function buildMetaCell(job) {
  const td = document.createElement('td');
  td.className = 'meta-stack';
  const location = document.createElement('div');
  location.textContent = job.location || '—';
  td.appendChild(location);

  const source = document.createElement('div');
  source.className = 'muted';
  source.textContent = job.source || '本地导入';
  td.appendChild(source);
  return td;
}

function buildJdCell(job) {
  const td = document.createElement('td');

  const summaryLabel = document.createElement('div');
  summaryLabel.className = 'section-kicker';
  summaryLabel.textContent = job.jd_summary ? 'JD 摘要（ai生成）' : 'JD 摘要（未生成）';
  td.appendChild(summaryLabel);

  const summary = document.createElement('div');
  summary.className = 'muted';
  summary.textContent = job.jd_summary || '当前岗位还没有生成摘要。';
  td.appendChild(summary);

  if (job.jd_full) {
    const details = document.createElement('details');
    const detailSummary = document.createElement('summary');
    detailSummary.textContent = '展开原始 JD';
    details.appendChild(detailSummary);

    const full = document.createElement('div');
    full.textContent = trimText(job.jd_full, 360);
    details.appendChild(full);
    td.appendChild(details);
  }

  if (job.tags?.length) {
    const tags = document.createElement('div');
    tags.className = 'tag-cloud';
    job.tags.forEach((tag) => {
      const chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.textContent = tag;
      tags.appendChild(chip);
    });
    td.appendChild(tags);
  }

  if (job.fetch_error) {
    const error = document.createElement('div');
    error.className = 'muted';
    error.textContent = `抓取提示：${job.fetch_error}`;
    td.appendChild(error);
  }

  return td;
}

function buildLinkCell(job) {
  const td = document.createElement('td');
  td.className = 'link-cell';

  if (job.url) {
    const link = document.createElement('a');
    link.href = job.url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    link.className = 'job-link';
    link.textContent = '打开岗位';
    td.appendChild(link);
  } else {
    td.textContent = '—';
  }

  return td;
}

function buildStatusCell(job) {
  const td = document.createElement('td');
  td.className = 'status-cell';

  const select = document.createElement('select');
  STATUS_OPTIONS.forEach((status) => {
    const option = document.createElement('option');
    option.value = status;
    option.textContent = status;
    option.selected = status === job.status;
    select.appendChild(option);
  });

  select.addEventListener('change', async () => {
    await updateJob(job.id, { status: select.value, applied: job.applied });
  });
  td.appendChild(select);
  return td;
}

function buildAppliedCell(job) {
  const td = document.createElement('td');
  const wrapper = document.createElement('label');
  wrapper.className = 'check-field';

  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = Boolean(job.applied);
  checkbox.addEventListener('change', async () => {
    await updateJob(job.id, { applied: checkbox.checked, status: job.status });
  });

  const text = document.createElement('span');
  text.textContent = checkbox.checked ? '已投递' : '未投递';
  checkbox.addEventListener('change', () => {
    text.textContent = checkbox.checked ? '已投递' : '未投递';
  });

  wrapper.appendChild(checkbox);
  wrapper.appendChild(text);
  td.appendChild(wrapper);
  return td;
}

function buildActionCell(job) {
  const td = document.createElement('td');
  td.className = 'actions-cell';

  const deleteButton = document.createElement('button');
  deleteButton.type = 'button';
  deleteButton.className = 'button danger tiny';
  deleteButton.textContent = '删除';
  deleteButton.addEventListener('click', async () => {
    const confirmed = window.confirm(`确认删除 ${job.company} | ${job.title} 吗？这会同时回写 YAML。`);
    if (!confirmed) {
      return;
    }
    try {
      setButtonBusy(deleteButton, true);
      const result = await api(`/api/jobs/${job.id}`, { method: 'DELETE' });
      showToast(`已删除 ${result.job.company} | ${result.job.title}`);
      await loadJobs();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      setButtonBusy(deleteButton, false);
    }
  });

  td.appendChild(deleteButton);
  return td;
}

function buildTextCell(text) {
  const td = document.createElement('td');
  td.textContent = text;
  return td;
}

async function updateJob(jobId, payload) {
  try {
    await api(`/api/jobs/${jobId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    await loadJobs();
  } catch (error) {
    showToast(error.message, true);
  }
}

function syncSelectionWithVisibleJobs() {
  const visibleIds = new Set(state.jobs.map((job) => job.id));
  state.selectedJobIds = new Set(
    Array.from(state.selectedJobIds).filter((jobId) => visibleIds.has(jobId))
  );
}

function renderBulkToolbar() {
  const visibleIds = state.jobs.map((job) => job.id);
  const selectedCount = Array.from(state.selectedJobIds).filter((jobId) => visibleIds.includes(jobId)).length;
  const allSelected = visibleIds.length > 0 && selectedCount === visibleIds.length;

  document.querySelector('#bulkSelectionCount').textContent = `已选 ${selectedCount} 条`;
  document.querySelector('#bulkDeleteButton').disabled = selectedCount === 0;
  document.querySelector('#selectAllJobsCheckbox').checked = allSelected;
  document.querySelector('#selectAllJobsCheckbox').indeterminate = selectedCount > 0 && !allSelected;
}

function toggleSelectAllJobs(event) {
  const checked = event.target.checked;
  state.jobs.forEach((job) => {
    if (checked) {
      state.selectedJobIds.add(job.id);
    } else {
      state.selectedJobIds.delete(job.id);
    }
  });
  renderJobsTable();
}

async function bulkDeleteJobs() {
  const ids = Array.from(state.selectedJobIds);
  if (!ids.length) {
    showToast('请先选择要删除的岗位。', true);
    return;
  }

  const confirmed = window.confirm(`确认批量删除已选的 ${ids.length} 条岗位吗？这会同时回写 YAML。`);
  if (!confirmed) {
    return;
  }

  const button = document.querySelector('#bulkDeleteButton');
  try {
    setButtonBusy(button, true);
    const result = await api('/api/jobs/bulk-delete', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    });
    state.selectedJobIds.clear();
    showToast(`已批量删除 ${result.deleted} 条岗位。`);
    await loadJobs();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setButtonBusy(button, false);
  }
}

async function loadPrefs() {
  try {
    state.prefs = await api('/api/prefs');
    fillPrefsForm(state.prefs);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadApiConfig() {
  try {
    state.apiConfig = await api('/api/apis');
    fillApiConfigForm(state.apiConfig);
    renderAnalysisApiSummary();
  } catch (error) {
    showToast(error.message, true);
  }
}

function fillApiConfigForm(config) {
  const llm = config?.llm || {};
  const notion = config?.notion || {};
  document.querySelector('#apiLlmBaseUrl').value = llm.base_url || '';
  document.querySelector('#apiLlmModel').value = llm.model || '';
  document.querySelector('#apiLlmApiKey').value = llm.api_key || '';
  document.querySelector('#apiNotionApiKey').value = notion.api_key || '';
  document.querySelector('#apiNotionDatabaseId').value = notion.database_id || '';
}

function collectApiConfigFromForm() {
  return {
    llm: {
      base_url: document.querySelector('#apiLlmBaseUrl').value.trim(),
      model: document.querySelector('#apiLlmModel').value.trim(),
      api_key: document.querySelector('#apiLlmApiKey').value.trim(),
    },
    notion: {
      api_key: document.querySelector('#apiNotionApiKey').value.trim(),
      database_id: document.querySelector('#apiNotionDatabaseId').value.trim(),
    },
  };
}

async function saveApiConfig() {
  const button = document.querySelector('#saveApiConfigButton');
  try {
    setButtonBusy(button, true);
    state.apiConfig = await api('/api/apis', {
      method: 'PUT',
      body: JSON.stringify(collectApiConfigFromForm()),
    });
    fillApiConfigForm(state.apiConfig);
    renderAnalysisApiSummary();
    showToast('API 配置已保存到 api-config.yaml。');
    return state.apiConfig;
  } catch (error) {
    showToast(error.message, true);
    throw error;
  } finally {
    setButtonBusy(button, false);
  }
}

function fillPrefsForm(prefs) {
  document.querySelector('#prefsCities').value = joinLines(prefs.cities);
  document.querySelector('#prefsQueries').value = joinLines(prefs.queries);
  document.querySelector('#prefsScales').value = joinLines(prefs.scales);
  document.querySelector('#prefsPreferredTags').value = joinLines(prefs.preferred_tags);
  document.querySelector('#prefsJobType').value = prefs.job_type || '全职';
  document.querySelector('#prefsMinDay').value = prefs.salary?.min_day ?? 150;
  document.querySelector('#prefsMinMonth').value = prefs.salary?.min_month ?? 3000;
  document.querySelector('#prefsExcludeBigTech').value = joinLines(prefs.filters?.exclude_big_tech);
  document.querySelector('#prefsExcludeNonTech').value = joinLines(prefs.filters?.exclude_non_tech);
  document.querySelector('#prefsExcludeExtra').value = joinLines(prefs.filters?.exclude_extra);
  document.querySelector('#prefsEducation').value = prefs.profile?.education || '';
  document.querySelector('#prefsDuration').value = prefs.profile?.internship_duration || '';
  document.querySelector('#prefsProfileNote').value = prefs.profile?.note || '';
  syncPrefsSalaryInputsForJobType();
}

function collectPrefsFromForm() {
  return {
    cities: splitLines(document.querySelector('#prefsCities').value),
    queries: splitLines(document.querySelector('#prefsQueries').value),
    scales: splitLines(document.querySelector('#prefsScales').value),
    preferred_tags: splitLines(document.querySelector('#prefsPreferredTags').value),
    job_type: document.querySelector('#prefsJobType').value,
    salary: {
      min_day: Number(document.querySelector('#prefsMinDay').value || 0),
      min_month: Number(document.querySelector('#prefsMinMonth').value || 0),
    },
    filters: {
      exclude_big_tech: splitLines(document.querySelector('#prefsExcludeBigTech').value),
      exclude_non_tech: splitLines(document.querySelector('#prefsExcludeNonTech').value),
      exclude_extra: splitLines(document.querySelector('#prefsExcludeExtra').value),
    },
    profile: {
      education: document.querySelector('#prefsEducation').value.trim(),
      internship_duration: document.querySelector('#prefsDuration').value.trim(),
      note: document.querySelector('#prefsProfileNote').value.trim(),
    },
  };
}

async function savePrefs(silent = false) {
  const button = document.querySelector('#savePrefsButton');
  try {
    setButtonBusy(button, true);
    const payload = collectPrefsFromForm();
    state.prefs = await api('/api/prefs', {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    if (!silent) {
      showToast('偏好已保存到 internship-prefs.yaml。');
    }
    return state.prefs;
  } catch (error) {
    showToast(error.message, true);
    throw error;
  } finally {
    setButtonBusy(button, false);
  }
}

async function loadFetchStatus() {
  try {
    const status = await api('/api/fetch');
    renderFetchStatus(status);
    if (['running', 'cancelling'].includes(status.status)) {
      startFetchPolling();
    }
  } catch (error) {
    showToast(error.message, true);
  }
}

function startFetchPolling() {
  if (state.fetchPollTimer) {
    return;
  }
  state.fetchPollTimer = window.setInterval(async () => {
    try {
      const status = await api('/api/fetch');
      renderFetchStatus(status);
      if (!['running', 'cancelling'].includes(status.status)) {
        stopFetchPolling();
      }
    } catch (error) {
      stopFetchPolling();
      showToast(error.message, true);
    }
  }, 1500);
}

function stopFetchPolling() {
  if (!state.fetchPollTimer) {
    return;
  }
  window.clearInterval(state.fetchPollTimer);
  state.fetchPollTimer = null;
}

async function stopFetch() {
  if (!['running', 'cancelling'].includes(state.fetchStatus?.status)) {
    return;
  }

  const button = document.querySelector('#stopFetchButton');
  try {
    if (state.fetchStatus?.status !== 'cancelling') {
      setButtonBusy(button, true);
    }
    const status = await api('/api/fetch', { method: 'DELETE' });
    renderFetchStatus(status);
    startFetchPolling();
    showToast('已发送终止请求。');
  } catch (error) {
    showToast(error.message, true);
    setButtonBusy(button, false);
    button.disabled = false;
  }
}

function renderFetchStatus(status) {
  const previousStatus = state.fetchStatus?.status;
  const previousTaskId = state.fetchStatus?.task_id;
  state.fetchStatus = status;

  const labelMap = {
    idle: '空闲',
    running: '抓取中',
    cancelling: '终止中',
    cancelled: '已终止',
    completed: '已完成',
    failed: '失败',
  };
  document.querySelector('#fetchStatusLabel').textContent = labelMap[status.status] || '未知状态';

  const metaParts = [];
  if (status.total_steps) {
    metaParts.push(`步骤 ${status.current_step || 0}/${status.total_steps}`);
  }
  if (status.step_label) {
    metaParts.push(status.step_label);
  }
  if (status.status === 'running') {
    metaParts.push('BOSS 在独立 Chrome 抓取窗口中运行');
  }
  document.querySelector('#fetchStatusMeta').textContent = metaParts.join(' · ') || '当前没有抓取任务';
  document.querySelector('#fetchStatusHint').textContent =
    status.progress_message || (status.status === 'running' ? '正在持续拉取日志…' : '等待操作…');

  const progress = Number(status.overall_percent || 0);
  document.querySelector('#fetchProgressBar').style.width = `${progress}%`;

  const logs = status.logs?.length ? status.logs : ['等待操作…'];
  fetchLog.textContent = logs.join('\n');
  fetchLog.scrollTop = fetchLog.scrollHeight;

  const runFetchButton = document.querySelector('#runFetchButton');
  const stopFetchButton = document.querySelector('#stopFetchButton');
  if (['running', 'cancelling'].includes(status.status)) {
    setButtonBusy(runFetchButton, true);
  } else {
    setButtonBusy(runFetchButton, false);
  }

  if (status.status === 'cancelling') {
    setButtonBusy(stopFetchButton, true);
  } else {
    setButtonBusy(stopFetchButton, false);
    stopFetchButton.disabled = status.status !== 'running';
  }

  if (previousStatus === 'running' && status.status === 'completed') {
    showToast('抓取完成，岗位库已刷新。');
    loadJobs();
  }
  if (['running', 'cancelling'].includes(previousStatus) && status.status === 'cancelled') {
    showToast('抓取已终止。');
    loadJobs();
  }
  if (previousStatus === 'running' && status.status === 'failed') {
    showToast(status.error || '抓取失败，请查看日志。', true);
    loadJobs();
  }
  if (!previousTaskId && status.status === 'completed' && status.result?.sync) {
    loadJobs();
  }
}

function maskSecret(value) {
  const text = String(value || '').trim();
  if (!text) {
    return '未配置';
  }
  if (text.length <= 8) {
    return '已配置';
  }
  return `${text.slice(0, 4)}...${text.slice(-4)}`;
}

function renderAnalysisApiSummary() {
  const llm = state.apiConfig?.llm || {};
  document.querySelector('#analysisConfiguredBaseUrl').textContent = llm.base_url || '未配置';
  document.querySelector('#analysisConfiguredModel').textContent = llm.model || '未配置';
  document.querySelector('#analysisConfiguredApiKey').textContent = maskSecret(llm.api_key);
}

async function runAnalysis() {
  const button = document.querySelector('#runAnalysisButton');
  const question = document.querySelector('#analysisQuestion').value.trim();
  const llm = state.apiConfig?.llm || {};
  if (!question) {
    showToast('先输入分析问题。', true);
    return;
  }
  if (!llm.base_url || !llm.model || !llm.api_key) {
    showToast('先到 API 管理页补全模型配置。', true);
    activateTab('apis');
    return;
  }

  try {
    setButtonBusy(button, true);
    const result = await api('/api/analysis/query', {
      method: 'POST',
      body: JSON.stringify({
        question,
      }),
    });
    renderAnalysisResult(result);
    activateTab('analysis');
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setButtonBusy(button, false);
  }
}

function renderAnalysisResult(result) {
  state.lastSql = result.sql || '';
  document.querySelector('#analysisTitle').textContent = result.title || '分析结果';
  document.querySelector('#analysisAssumption').textContent = result.assumption || '未返回额外假设。';
  document.querySelector('#analysisInsight').textContent = result.insight || '暂无分析说明。';
  document.querySelector('#analysisSql').textContent = result.sql || '模型未生成 SQL。';

  const head = document.querySelector('#analysisTableHead');
  const body = document.querySelector('#analysisTableBody');
  head.innerHTML = '';
  body.innerHTML = '';

  const headRow = document.createElement('tr');
  (result.columns || []).forEach((column) => {
    const cell = document.createElement('th');
    cell.textContent = column;
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);

  const LONG_CELL_LIMIT = 80;
  (result.rows || []).forEach((row) => {
    const tr = document.createElement('tr');
    row.forEach((value) => {
      const td = document.createElement('td');
      const text = value == null ? '—' : String(value);
      if (text.length > LONG_CELL_LIMIT) {
        td.className = 'cell-long';
        const preview = document.createElement('span');
        preview.textContent = trimText(text, LONG_CELL_LIMIT);
        td.appendChild(preview);
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = '展开全文';
        details.appendChild(summary);
        const full = document.createElement('div');
        full.textContent = text;
        details.appendChild(full);
        td.appendChild(details);
      } else {
        td.textContent = text;
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });

  analysisResult.hidden = false;
}

function clearAnalysis() {
  document.querySelector('#analysisQuestion').value = '';
  document.querySelector('#analysisTitle').textContent = '分析结果';
  document.querySelector('#analysisAssumption').textContent = '';
  document.querySelector('#analysisInsight').textContent = '';
  document.querySelector('#analysisSql').textContent = '';
  document.querySelector('#analysisTableHead').innerHTML = '';
  document.querySelector('#analysisTableBody').innerHTML = '';
  analysisResult.hidden = true;
  state.lastSql = '';
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || '请求失败');
  }
  return payload;
}

function splitLines(value) {
  return value
    .split(/\n|,|，|、/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function joinLines(values = []) {
  return (values || []).join('\n');
}

function joinInline(values = []) {
  return (values || []).join(' / ');
}

function trimText(value, limit) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, limit)}...`;
}

function clampNumber(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function setButtonBusy(button, busy) {
  if (!button) {
    return;
  }
  button.disabled = busy;
  if (busy) {
    if (!button.dataset.originalLabel) {
      button.dataset.originalLabel = button.textContent;
    }
    button.textContent = '处理中…';
    return;
  }
  if (button.dataset.originalLabel) {
    button.textContent = button.dataset.originalLabel;
    delete button.dataset.originalLabel;
  }
}

let toastTimer;
function showToast(message, isError = false) {
  toast.hidden = false;
  toast.textContent = message;
  toast.style.background = isError ? 'rgba(146, 50, 44, 0.94)' : 'rgba(33, 48, 41, 0.92)';
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toast.hidden = true;
  }, 3200);
}
