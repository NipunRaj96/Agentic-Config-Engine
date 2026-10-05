/**
 * AI-Config-Engine TechOps Telemetry Console - Client Application
 * Handles WebSocket bidirectional streaming, step timeline, AST formatting, and batch controls.
 */

// ── Zero-Dependency SVG Icon Dictionary (100% Offline & Corporate Proxy Immune) ──
const SVG_ICONS = {
  schedule: `<svg class="w-3.5 h-3.5 text-on-surface-variant/70 inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`,
  running: `<svg class="w-3.5 h-3.5 text-primary-container animate-spin inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>`,
  check_circle: `<svg class="w-3.5 h-3.5 text-tertiary-container inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`,
  skipped: `<svg class="w-3.5 h-3.5 text-on-surface-variant/60 inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>`,
  failed: `<svg class="w-3.5 h-3.5 text-error inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
  circle: `<svg class="w-3.5 h-3.5 text-on-surface-variant inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/></svg>`,
  open_in_new: `<svg class="w-3.5 h-3.5 inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>`,
  play_arrow: `<svg class="w-5 h-5 fill-current inline-block align-middle" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>`,
  spinner_lg: `<svg class="w-5 h-5 animate-spin inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>`,
  info: `<svg class="w-5 h-5 text-primary-container inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
  error_lg: `<svg class="w-5 h-5 text-error inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
  check_lg: `<svg class="w-5 h-5 text-tertiary-container inline-block align-middle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`,
};

// ── Application State ────────────────────────────────────────────────────────
const AppState = {
  ws: null,
  isConnected: false,
  activeMode: 'single', // 'single' | 'batch'
  isRunning: false,
  currentCrawlerId: '',
  latestConfig: null,
  latestJperlConfig: null,
  latestXpathConfig: null,
  latestResult: null,
  selectedConfigType: 'xpath',
  extractedJobs: [],
  batchRows: [],
  batchResults: [],
  uploadedCsvText: '',
  uploadedCsvFilename: '',
  uploadedCsvRowsCount: 0,
  stepTimers: {},
  runStartTime: 0,
  runtimeInterval: null,
};

// ── Step Definitions (13 Steps) ──────────────────────────────────────────────
const STEPS = [
  { id: 1,  name: 'RobotChecker',        title: 'Robot Checker',       pendingDesc: 'Robots.txt check',          activeDesc: 'Checking robots.txt...',      defaultBadge: 'ALLOW' },
  { id: 2,  name: 'ATSFingerprinter',    title: 'ATS Fingerprinter',   pendingDesc: 'Scan ATS signatures',       activeDesc: 'Scanning 21 ATS patterns...', defaultBadge: '21 ATS' },
  { id: 3,  name: 'ConfigCacheStep',     title: 'Domain Cache',        pendingDesc: 'Check domain cache',        activeDesc: 'Checking SQLite cache...',    defaultBadge: 'CACHE' },
  { id: 4,  name: 'TrafficInterceptor',  title: 'Traffic Sniffer',     pendingDesc: 'Sniff XHR & network',       activeDesc: 'Sniffing live network...',    defaultBadge: 'CAPTURE' },
  { id: 5,  name: 'HeuristicRanker',     title: 'Heuristic Ranker',    pendingDesc: 'Score API feeds',           activeDesc: 'Ranking API payloads...',     defaultBadge: 'SCORE' },
  { id: 6,  name: 'SourceResolver',      title: 'Source Resolver',     pendingDesc: 'Arbitrate data source',     activeDesc: 'Resolving source feed...',    defaultBadge: 'SOURCE' },
  { id: 7,  name: 'JDStrategyDiscovery', title: 'JD Discovery',        pendingDesc: 'Discover JD routes',        activeDesc: 'Testing detail links...',     defaultBadge: 'SCHEMA' },
  { id: 8,  name: 'WPRestDetector',      title: 'WP REST Detect',      pendingDesc: 'WP REST scan',              activeDesc: 'Scanning WP REST API...',     defaultBadge: 'WP REST' },
  { id: 9,  name: 'SRPClassifier',       title: 'SRP Classifier',      pendingDesc: 'Classify SRP layout',       activeDesc: 'Classifying HTML layout...',  defaultBadge: 'CLASSIFY' },
  { id: 10, name: 'LLMReasoner',         title: 'LLM Reasoner',        pendingDesc: 'Reason JSON schema',        activeDesc: 'Reasoning JSON schema...',    defaultBadge: 'AI REASON' },
  { id: 11, name: 'LOCRGXGenerator',     title: 'LOCRGX Gen',          pendingDesc: 'Generating regex',          activeDesc: 'Generating PCRE regex...',    defaultBadge: 'LOCRGX' },
  { id: 12, name: 'XPathSRPGenerator',   title: 'XPath SRP Gen',       pendingDesc: 'Synthesize XPath',          activeDesc: 'Generating DOM XPath...',     defaultBadge: 'XPATH' },
  { id: 13, name: 'ConfigCompileStep',   title: 'Config Compiler',     pendingDesc: 'Compile JPERL config',      activeDesc: 'Compiling JPERL config...',   defaultBadge: 'COMPILE' },
];

// ── Initialization ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  renderStepMatrix();
  initWebSocket();
  initDragAndDrop();
  initKeyboardShortcuts();
});

// ── WebSocket Connection & Heartbeat ─────────────────────────────────────────
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host || 'localhost:8000';
  const wsUrl = `${protocol}//${host}/ws/pipeline`;

  updateConnectionStatus('connecting', 'Connecting...');

  try {
    AppState.ws = new WebSocket(wsUrl);

    AppState.ws.onopen = () => {
      AppState.isConnected = true;
      updateConnectionStatus('connected', 'Gateway Active');
      // Heartbeat ping every 15 seconds
      setInterval(() => {
        if (AppState.ws && AppState.ws.readyState === WebSocket.OPEN) {
          AppState.ws.send(JSON.stringify({ action: 'ping', timestamp: Date.now() }));
        }
      }, 15000);
    };

    AppState.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleServerMessage(data);
      } catch (err) {
        console.error('Failed to parse WebSocket message:', err, event.data);
      }
    };

    AppState.ws.onclose = () => {
      AppState.isConnected = false;
      updateConnectionStatus('disconnected', 'Disconnected');
      // Auto-reconnect after 3s
      setTimeout(initWebSocket, 3000);
    };

    AppState.ws.onerror = (err) => {
      console.warn('WebSocket encountered error:', err);
      AppState.isConnected = false;
      updateConnectionStatus('error', 'Gateway Error');
    };
  } catch (err) {
    console.error('WebSocket initialization failed:', err);
    setTimeout(initWebSocket, 3000);
  }
}

function updateConnectionStatus(state, label) {
  const beacon = document.getElementById('connectionBeacon');
  const text = document.getElementById('connectionStatusText');
  if (!beacon || !text) return;

  text.innerText = label;
  if (state === 'connected') {
    beacon.className = 'status-beacon absolute inline-flex h-full w-full rounded-full bg-tertiary-container opacity-75';
    text.className = 'text-code-sm text-tertiary font-medium';
  } else if (state === 'connecting') {
    beacon.className = 'absolute inline-flex h-full w-full rounded-full bg-secondary opacity-75 animate-ping';
    text.className = 'text-code-sm text-secondary font-medium';
  } else {
    beacon.className = 'absolute inline-flex h-full w-full rounded-full bg-error opacity-75';
    text.className = 'text-code-sm text-error font-medium';
  }
}

// ── Server Event Dispatcher ──────────────────────────────────────────────────
function handleServerMessage(msg) {
  const type = msg.type;

  if (type === 'pong') {
    return;
  }

  if (type === 'pipeline_start') {
    resetStepMatrix();
    clearLogs();
    startRuntimeTimer();
    setRunButtonState(true);
    updateStatusPill('In Process', 'Running Pipeline...', 'secondary');
    appendLog(new Date().toISOString(), 'SYSTEM', `Started pipeline for ${msg.company_name} (${msg.crawler_id})`);
  } else if (type === 'step_start') {
    setStepState(msg.step_id, 'running', msg.stage_desc || msg.active_desc);
    appendLog(new Date().toISOString(), `STEP-${msg.step_id}`, `Starting ${msg.step_name}...`);
  } else if (type === 'step_finish') {
    setStepState(msg.step_id, msg.status, msg.stage_desc || msg.badge, msg.duration_ms);
    appendLog(new Date().toISOString(), `STEP-${msg.step_id}`, `Finished ${msg.step_name} -> ${msg.status.toUpperCase()} (${msg.badge}) in ${msg.duration_ms}ms`);
  } else if (type === 'log') {
    appendLog(msg.timestamp, msg.level, msg.message);
  } else if (type === 'pipeline_complete') {
    stopRuntimeTimer(msg.runtime_seconds);
    setRunButtonState(false);
    handleSinglePipelineResult(msg);
  } else if (type === 'batch_start') {
    startRuntimeTimer();
    setRunButtonState(true);
    showToast(`Started batch execution for ${msg.total_targets} targets`, 'info');
    appendLog(new Date().toISOString(), 'BATCH', `Starting batch crawl for ${msg.total_targets} records.`);
    document.getElementById('batchProgressSection')?.classList.remove('hidden');
  } else if (type === 'batch_row_start') {
    appendLog(new Date().toISOString(), `ROW-${msg.row_index}`, `[${msg.row_index}/${msg.total}] Running ${msg.company_name} (${msg.crawler_id}) -> ${msg.career_site_url}`);
  } else if (type === 'batch_row_complete') {
    updateBatchProgress(msg);
  } else if (type === 'batch_complete') {
    stopRuntimeTimer();
    setRunButtonState(false);
    showToast(`Batch completed: ${msg.passed_count} passed, ${msg.failed_count} failed (${msg.success_rate})`, 'success');
    appendLog(new Date().toISOString(), 'BATCH', `Batch processing finished: ${msg.passed_count} passed, ${msg.failed_count} failed.`);
    document.getElementById('btnDownloadBatchCsv')?.classList.remove('hidden');
    document.getElementById('btnDownloadReplayJson')?.classList.remove('hidden');
  } else if (type === 'error') {
    setRunButtonState(false);
    stopRuntimeTimer();
    showToast(msg.message, 'error');
    appendLog(new Date().toISOString(), 'ERROR', msg.message);
  }
}

// ── Step Matrix Rendering & State Management ─────────────────────────────────
function renderStepMatrix() {
  const container = document.getElementById('stepMatrixGrid');
  if (!container) return;

  container.innerHTML = STEPS.map((step) => `
    <div id="stepCard_${step.id}" class="step-item step-pending p-3 rounded-xl bg-surface-container-lowest border border-white/[0.08] flex flex-col justify-between hover:border-primary-container/50 transition-all shadow-sm min-h-[86px]">
      <div class="flex items-center justify-between text-code-sm mb-1">
        <span class="text-label-caps text-on-surface-variant/60 font-mono font-bold">${String(step.id).padStart(2, '0')}</span>
        <div class="flex items-center gap-1.5">
          <span id="stepTime_${step.id}" class="text-on-surface-variant/60 font-mono text-[10px]">--</span>
          <span id="stepIcon_${step.id}" class="flex items-center">${SVG_ICONS.schedule}</span>
        </div>
      </div>
      <div id="stepName_${step.id}" class="text-code-sm text-on-surface font-semibold truncate" title="${step.name}">${step.title || step.name}</div>
      <div id="stepDesc_${step.id}" class="text-[11px] font-mono leading-tight mt-1 truncate text-on-surface-variant/70" title="${step.pendingDesc}">${step.pendingDesc}</div>
    </div>
  `).join('');
}

function resetStepMatrix() {
  STEPS.forEach((step) => {
    const card = document.getElementById(`stepCard_${step.id}`);
    const icon = document.getElementById(`stepIcon_${step.id}`);
    const descElem = document.getElementById(`stepDesc_${step.id}`);
    const timeElem = document.getElementById(`stepTime_${step.id}`);
    if (card) card.className = 'step-item step-pending p-3 rounded-xl bg-surface-container-lowest border border-white/[0.08] flex flex-col justify-between shadow-sm min-h-[86px]';
    if (icon) icon.innerHTML = SVG_ICONS.schedule;
    if (descElem) {
      descElem.innerText = step.pendingDesc;
      descElem.title = step.pendingDesc;
      descElem.className = 'text-[11px] font-mono leading-tight mt-1 truncate text-on-surface-variant/70';
    }
    if (timeElem) {
      timeElem.innerText = '--';
      timeElem.className = 'text-on-surface-variant/60 font-mono text-[10px]';
    }
  });
}

function setStepState(stepId, status, descText = null, durationMs = null) {
  const card = document.getElementById(`stepCard_${stepId}`);
  const icon = document.getElementById(`stepIcon_${stepId}`);
  const descElem = document.getElementById(`stepDesc_${stepId}`);
  const timeElem = document.getElementById(`stepTime_${stepId}`);
  const stepDef = STEPS.find(s => s.id === stepId) || {};

  if (!card) return;

  if (status === 'running') {
    card.className = 'step-item step-running p-3 rounded-xl bg-surface-container-lowest border border-primary-container shadow-md flex flex-col justify-between min-h-[86px]';
    if (icon) icon.innerHTML = SVG_ICONS.running;
    if (descElem) {
      descElem.innerText = descText || stepDef.activeDesc || 'Processing...';
      descElem.title = descElem.innerText;
      descElem.className = 'text-[11px] font-mono leading-tight mt-1 truncate text-primary-container font-medium animate-pulse';
    }
    if (timeElem) {
      timeElem.innerText = '...';
      timeElem.className = 'text-primary-container font-mono text-[10px]';
    }
  } else if (status === 'done') {
    card.className = 'step-item step-done p-3 rounded-xl bg-surface-container-lowest border border-tertiary-container/40 flex flex-col justify-between shadow-sm min-h-[86px]';
    if (icon) icon.innerHTML = SVG_ICONS.check_circle;
    if (descElem) {
      descElem.innerText = descText || 'Completed';
      descElem.title = descElem.innerText;
      descElem.className = 'text-[11px] font-mono leading-tight mt-1 truncate text-tertiary-fixed font-medium';
    }
    if (timeElem && durationMs !== null) {
      timeElem.innerText = `${durationMs}ms`;
      timeElem.className = 'text-tertiary-fixed-dim font-mono text-[10px]';
    }
  } else if (status === 'skipped') {
    card.className = 'step-item step-skipped p-3 rounded-xl bg-surface-container-lowest border border-white/[0.08] opacity-60 flex flex-col justify-between shadow-sm min-h-[86px]';
    if (icon) icon.innerHTML = SVG_ICONS.skipped;
    if (descElem) {
      descElem.innerText = descText || 'Skipped';
      descElem.title = descElem.innerText;
      descElem.className = 'text-[11px] font-mono leading-tight mt-1 truncate text-on-surface-variant/70 italic';
    }
    if (timeElem && durationMs !== null) {
      timeElem.innerText = `${durationMs}ms`;
      timeElem.className = 'text-on-surface-variant/60 font-mono text-[10px]';
    }
  } else if (status === 'failed') {
    card.className = 'step-item step-failed p-3 rounded-xl bg-surface-container-lowest border border-error flex flex-col justify-between shadow-md min-h-[86px]';
    if (icon) icon.innerHTML = SVG_ICONS.failed;
    if (descElem) {
      descElem.innerText = descText || 'Failed';
      descElem.title = descElem.innerText;
      descElem.className = 'text-[11px] font-mono leading-tight mt-1 truncate text-error font-medium';
    }
    if (timeElem && durationMs !== null) {
      timeElem.innerText = `${durationMs}ms`;
      timeElem.className = 'text-error font-mono text-[10px]';
    }
  }
}

// ── Single Pipeline Result Handler ───────────────────────────────────────────
function handleSinglePipelineResult(res) {
  AppState.latestResult = res;
  AppState.latestConfig = res.config;
  AppState.latestJperlConfig = res.jperl_config;
  AppState.latestXpathConfig = res.xpath_config;
  AppState.extractedJobs = res.extracted_jobs || [];

  // Dual config toolbar visibility
  const dualToolbar = document.getElementById('dualConfigToolbar');
  const hasBoth = res.has_both || (res.jperl_config && res.xpath_config);
  if (dualToolbar) {
    if (hasBoth) {
      dualToolbar.classList.remove('hidden');
      selectConfigDisplay(res.primary_config_type || 'xpath');
    } else {
      dualToolbar.classList.add('hidden');
    }
  }

  // 1. Update Telemetry Strip
  const pathElem = document.getElementById('telemetryPathText');
  if (pathElem) {
    if (hasBoth) {
      pathElem.innerText = 'XPATH (SRP) + JPERL (LOCRGX)';
    } else {
      const path = res.primary_config_type ? res.primary_config_type.toUpperCase() : (res.site_type || 'JPERL');
      pathElem.innerText = `${path} ${res.crawler_type || ''}`.trim();
    }
  }

  const tokenElem = document.getElementById('telemetryTokensText');
  if (tokenElem) {
    tokenElem.innerText = `${res.total_tokens || 0} (${res.tokens_per_job || 0} / job)`;
  }

  const confElem = document.getElementById('telemetryPrecisionText');
  const confBar = document.getElementById('telemetryPrecisionBar');
  if (confElem) confElem.innerText = res.confidence_percent || '0%';
  if (confBar) confBar.style.width = res.confidence_percent || '0%';

  updateStatusPill(res.tech_status, res.sub_tech_comment || res.tech_status, res.tech_status === 'Done' ? 'tertiary' : 'error');

  // 2. Render JSON Config AST Viewer
  renderConfigJson(AppState.latestConfig);

  // 3. Render Extracted Jobs Table
  renderExtractedJobs(res.extracted_jobs);

  // Switch to config tab automatically
  switchResultTab('config');

  if (res.tech_status === 'Done') {
    showToast(`Configuration successfully synthesized! Confidence: ${res.confidence_percent}`, 'success');
  } else {
    showToast(`Pipeline terminated: ${res.tech_status} (${res.tech_comments || 'Review logs'})`, 'error');
  }
}

function selectConfigDisplay(type) {
  AppState.selectedConfigType = type;
  const btnXpath = document.getElementById('btnSelectConfig_xpath');
  const btnJperl = document.getElementById('btnSelectConfig_jperl');

  if (type === 'xpath') {
    if (btnXpath) btnXpath.className = 'px-2.5 py-1 rounded-md text-[11px] font-mono text-primary font-medium bg-primary-container/20 border border-primary-container/30 transition-all';
    if (btnJperl) btnJperl.className = 'px-2.5 py-1 rounded-md text-[11px] font-mono text-on-surface-variant hover:text-on-surface transition-all';
    AppState.latestConfig = AppState.latestXpathConfig || AppState.latestResult?.config;
    renderConfigJson(AppState.latestConfig);
  } else if (type === 'jperl') {
    if (btnJperl) btnJperl.className = 'px-2.5 py-1 rounded-md text-[11px] font-mono text-primary font-medium bg-primary-container/20 border border-primary-container/30 transition-all';
    if (btnXpath) btnXpath.className = 'px-2.5 py-1 rounded-md text-[11px] font-mono text-on-surface-variant hover:text-on-surface transition-all';
    AppState.latestConfig = AppState.latestJperlConfig || AppState.latestResult?.config;
    renderConfigJson(AppState.latestConfig);
  }
}

function updateStatusPill(status, subComment, colorType = 'tertiary') {
  const pill = document.getElementById('telemetryStatusPill');
  const icon = document.getElementById('telemetryStatusIcon');
  const text = document.getElementById('telemetryStatusText');
  if (!pill || !text) return;

  text.innerText = `${status} / ${subComment || 'Done'}`;
  if (colorType === 'tertiary') {
    pill.className = 'flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-tertiary-container/10 border border-tertiary-container/30';
    if (icon) icon.innerHTML = SVG_ICONS.check_circle;
    text.className = 'text-code-sm text-tertiary font-semibold';
  } else if (colorType === 'secondary') {
    pill.className = 'flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-secondary-container/20 border border-secondary/30';
    if (icon) icon.innerHTML = SVG_ICONS.running;
    text.className = 'text-code-sm text-secondary font-semibold';
  } else {
    pill.className = 'flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-error-container/20 border border-error/30';
    if (icon) icon.innerHTML = SVG_ICONS.failed;
    text.className = 'text-code-sm text-error font-semibold';
  }
}

// ── JSON Syntax Highlighting ─────────────────────────────────────────────────
function renderConfigJson(configObj) {
  const container = document.getElementById('configViewerContent');
  if (!container) return;

  if (!configObj) {
    container.innerHTML = '<div class="p-8 text-center text-on-surface-variant">No configuration generated yet. Fill the form and run pipeline.</div>';
    return;
  }

  const jsonStr = JSON.stringify(configObj, null, 2);
  const highlighted = syntaxHighlightJson(jsonStr);
  const lines = highlighted.split('\n');

  const rowsHtml = lines.map((line, idx) => `
    <tr>
      <td class="text-white/20 pr-4 select-none text-right w-8 font-mono">${String(idx + 1).padStart(2, '0')}</td>
      <td class="font-mono whitespace-pre">${line}</td>
    </tr>
  `).join('');

  container.innerHTML = `<table class="w-full border-collapse"><tbody>${rowsHtml}</tbody></table>`;
}

function syntaxHighlightJson(json) {
  json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return json.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    (match) => {
      let cls = 'syntax-num';
      if (/^"/.test(match)) {
        if (/:$/.test(match)) {
          cls = 'syntax-key';
        } else {
          cls = 'syntax-string';
        }
      } else if (/true|false/.test(match)) {
        cls = 'syntax-bool';
      } else if (/null/.test(match)) {
        cls = 'syntax-null';
      }
      return `<span class="${cls}">${match}</span>`;
    }
  );
}

// ── Extracted Jobs Table ─────────────────────────────────────────────────────
function renderExtractedJobs(jobs) {
  const tbody = document.getElementById('extractedJobsTableBody');
  const countBadge = document.getElementById('extractedJobsTabCount');
  const totalCountSpan = document.getElementById('extractedJobsTotalCount');
  if (!tbody) return;

  const count = (jobs && jobs.length) || 0;
  if (countBadge) countBadge.innerText = `(${count})`;
  if (totalCountSpan) totalCountSpan.innerText = `Showing ${count} extracted job records`;

  if (!jobs || jobs.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" class="py-8 text-center text-on-surface-variant">
          No extracted job payload available.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = jobs.map((job, idx) => `
    <tr class="hover:bg-white/[0.02] transition-colors">
      <td class="py-3.5 px-5">
        <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-tertiary-container/15 text-tertiary-fixed-dim text-label-caps font-semibold">
          <span class="w-1.5 h-1.5 rounded-full bg-tertiary-container"></span>
          ACTIVE
        </span>
      </td>
      <td class="py-3.5 px-5 font-medium text-on-surface">
        <div>${escapeHtml(job.JOBTITLE || 'Untitled Position')}</div>
        ${job.JOBDESC ? `<div class="text-xs text-on-surface-variant truncate max-w-md mt-0.5">${escapeHtml(job.JOBDESC.substring(0, 100))}...</div>` : ''}
      </td>
      <td class="py-3.5 px-5 font-mono text-primary-container font-semibold">${escapeHtml(job.JOBID || `JOB-${idx + 1}`)}</td>
      <td class="py-3.5 px-5 text-on-surface-variant">${escapeHtml(job.LOCATION || 'N/A')}</td>
      <td class="py-3.5 px-5 text-right">
        ${job.JOBLINK ? `
          <a href="${escapeHtml(job.JOBLINK)}" target="_blank" rel="noopener noreferrer" class="px-3 py-1.5 rounded-xl bg-surface-container hover:bg-primary-container/20 text-primary-container text-code-sm border border-white/[0.08] transition-colors inline-flex items-center gap-1.5">
            <span>Visit Link</span>
            <span class="flex items-center">${SVG_ICONS.open_in_new}</span>
          </a>
        ` : '<span class="text-on-surface-variant/40">No Link</span>'}
      </td>
    </tr>
  `).join('');
}

// ── Batch Ingestion Controller ───────────────────────────────────────────────
function updateBatchProgress(msg) {
  const row = msg.result;
  AppState.batchResults.push(row);

  const progressSection = document.getElementById('batchProgressSection');
  const progressBar = document.getElementById('batchProgressBar');
  const progressText = document.getElementById('batchProgressPercentText');
  const countText = document.getElementById('batchProgressCountText');
  const tableBody = document.getElementById('batchResultsTableBody');

  if (progressSection) progressSection.classList.remove('hidden');
  if (progressBar) progressBar.style.width = `${msg.progress_percent}%`;
  if (progressText) progressText.innerText = `${msg.progress_percent}%`;
  if (countText) countText.innerText = `${msg.row_index} of ${msg.total} Processed (${msg.passed_count} Done, ${msg.failed_count} Failed)`;

  if (tableBody) {
    const isDone = row.techStatus === 'Done';
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-white/[0.02] transition-colors border-b border-white/[0.05]';
    tr.innerHTML = `
      <td class="py-3 px-4 font-mono text-xs text-primary-container font-semibold">${escapeHtml(row.crawlerId)}</td>
      <td class="py-3 px-4 text-xs text-on-surface font-medium">${escapeHtml(row.companyName)}</td>
      <td class="py-3 px-4">
        <span class="px-2 py-0.5 rounded-full text-label-caps font-semibold ${isDone ? 'bg-tertiary-container/15 text-tertiary-fixed border border-tertiary-container/30' : 'bg-error-container/20 text-error border border-error/30'}">
          ${escapeHtml(row.techStatus)}
        </span>
      </td>
      <td class="py-3 px-4 text-xs text-on-surface-variant truncate max-w-xs">${escapeHtml(row.subTechComment || row.techComments || '-')}</td>
      <td class="py-3 px-4 text-xs text-on-surface font-mono">${escapeHtml(row.primary_config_type || row.crawlerType || '-')}</td>
      <td class="py-3 px-4 text-xs text-primary-fixed-dim font-bold">${Math.round(row.confidence * 100)}%</td>
    `;
    tableBody.prepend(tr);
  }
}

// ── Form Actions & Execution ─────────────────────────────────────────────────
function triggerRunPipeline() {
  if (AppState.isRunning) {
    showToast('Pipeline is already executing.', 'info');
    return;
  }

  if (!AppState.isConnected) {
    showToast('WebSocket connection is not established. Retrying...', 'error');
    initWebSocket();
    return;
  }

  const forceRefresh = document.getElementById('forceRefreshCheckbox')?.checked || false;

  if (AppState.activeMode === 'single') {
    const crawlerId = document.getElementById('crawlerIdInput')?.value.trim();
    const companyName = document.getElementById('companyNameInput')?.value.trim();
    const siteId = document.getElementById('siteIdInput')?.value.trim();
    const careerUrl = document.getElementById('careerSiteUrlInput')?.value.trim();
    const jobsOnPage = parseInt(document.getElementById('jobsOnPageInput')?.value, 10) || 0;
    const integrationLink = document.getElementById('integrationLinkInput')?.value.trim() || null;

    if (!careerUrl) {
      showToast('Please enter the Career Site Target URL.', 'error');
      document.getElementById('careerSiteUrlInput')?.focus();
      return;
    }

    const payload = {
      action: 'run_single',
      force_refresh: forceRefresh,
      data: {
        crawler_id: crawlerId || 'crw_sample',
        company_name: companyName || 'Target Company',
        site_id: siteId || `${crawlerId || 'crw'}_SRP`,
        career_site_url: careerUrl,
        jobs_on_career_page: jobsOnPage,
        integration_link: integrationLink,
      },
    };

    AppState.ws.send(JSON.stringify(payload));
  } else if (AppState.activeMode === 'batch') {
    if (!AppState.uploadedCsvText) {
      showToast('Please upload a CSV file with target career sites first.', 'error');
      return;
    }

    const payload = {
      action: 'run_batch',
      force_refresh: forceRefresh,
      csv_data: AppState.uploadedCsvText,
    };

    AppState.batchResults = [];
    const tableBody = document.getElementById('batchResultsTableBody');
    if (tableBody) tableBody.innerHTML = '';

    switchResultTab('batch_table');
    AppState.ws.send(JSON.stringify(payload));
  }
}

function resetForm() {
  if (AppState.activeMode === 'single') {
    document.getElementById('crawlerIdInput').value = '';
    document.getElementById('companyNameInput').value = '';
    document.getElementById('siteIdInput').value = '';
    document.getElementById('careerSiteUrlInput').value = '';
    document.getElementById('jobsOnPageInput').value = '10';
    document.getElementById('integrationLinkInput').value = '';
    showToast('Single Job input fields reset.', 'info');
  } else {
    AppState.uploadedCsvText = '';
    AppState.uploadedCsvFilename = '';
    AppState.uploadedCsvRowsCount = 0;
    document.getElementById('batchFilePreview')?.classList.add('hidden');
    document.getElementById('batchDropzone')?.classList.remove('hidden');
    const fileInput = document.getElementById('batchCsvFileInput');
    if (fileInput) fileInput.value = '';
    showToast('Uploaded batch CSV removed.', 'info');
  }
}

function switchMode(mode) {
  AppState.activeMode = mode;
  const select = document.getElementById('modeSelect');
  if (select && select.value !== mode) {
    select.value = mode;
  }

  const formSingle = document.getElementById('singleJobForm');
  const formBatch = document.getElementById('batchCsvForm');
  const tabBtnConfig = document.getElementById('tabBtn_config');
  const tabBtnJobs = document.getElementById('tabBtn_jobs');
  const tabBtnBatchTable = document.getElementById('tabBtn_batch_table');

  if (mode === 'single') {
    formSingle?.classList.remove('hidden');
    formBatch?.classList.add('hidden');
    tabBtnConfig?.classList.remove('hidden');
    tabBtnJobs?.classList.remove('hidden');
    tabBtnBatchTable?.classList.add('hidden');
    switchResultTab('config');
  } else {
    formSingle?.classList.add('hidden');
    formBatch?.classList.remove('hidden');
    tabBtnConfig?.classList.add('hidden');
    tabBtnJobs?.classList.add('hidden');
    tabBtnBatchTable?.classList.remove('hidden');
    switchResultTab('batch_table');
  }
}

function onModeSelectChange(selectElem) {
  switchMode(selectElem.value);
}

function switchResultTab(tab) {
  const tabs = ['config', 'jobs', 'batch_table', 'logs'];
  tabs.forEach((t) => {
    const btn = document.getElementById(`tabBtn_${t}`);
    const pane = document.getElementById(`tabPane_${t}`);
    if (btn) {
      if (t === tab) {
        btn.className = 'px-4 py-2.5 border-b-2 border-primary-container text-primary-container font-medium text-code-sm flex items-center gap-2';
      } else {
        btn.className = 'px-4 py-2.5 border-b-2 border-transparent text-on-surface-variant hover:text-on-surface font-medium text-code-sm flex items-center gap-2 transition-colors';
      }
    }
    if (pane) {
      if (t === tab) pane.classList.remove('hidden');
      else pane.classList.add('hidden');
    }
  });
}

// ── Drag & Drop CSV Ingestion ────────────────────────────────────────────────
function initDragAndDrop() {
  const dropzone = document.getElementById('batchDropzone');
  const fileInput = document.getElementById('batchCsvFileInput');

  if (!dropzone || !fileInput) return;

  dropzone.addEventListener('click', () => fileInput.click());

  dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('border-primary-container', 'bg-primary-container/5');
  });

  dropzone.addEventListener('dragleave', () => {
    dropzone.classList.remove('border-primary-container', 'bg-primary-container/5');
  });

  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('border-primary-container', 'bg-primary-container/5');
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleCsvFile(e.dataTransfer.files[0]);
    }
  });

  fileInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length > 0) {
      handleCsvFile(e.target.files[0]);
    }
  });
}

function handleCsvFile(file) {
  if (!file.name.endsWith('.csv')) {
    showToast('Please select a valid .csv file.', 'error');
    return;
  }

  const reader = new FileReader();
  reader.onload = (e) => {
    const text = e.target.result;
    AppState.uploadedCsvText = text;
    AppState.uploadedCsvFilename = file.name;

    const lines = text.trim().split('\n').filter((l) => l.trim().length > 0);
    const rowCount = Math.max(0, lines.length - 1);
    AppState.uploadedCsvRowsCount = rowCount;

    document.getElementById('batchFilenameText').innerText = file.name;
    document.getElementById('batchFileMetaText').innerText = `${rowCount} TARGETS • ${(file.size / 1024).toFixed(1)} KB`;
    document.getElementById('batchDropzone').classList.add('hidden');
    document.getElementById('batchFilePreview').classList.remove('hidden');

    showToast(`Loaded ${file.name} with ${rowCount} target sites.`, 'success');
  };
  reader.readAsText(file);
}

function removeUploadedCsv() {
  AppState.uploadedCsvText = '';
  AppState.uploadedCsvFilename = '';
  AppState.uploadedCsvRowsCount = 0;
  document.getElementById('batchFilePreview')?.classList.add('hidden');
  document.getElementById('batchDropzone')?.classList.remove('hidden');
  const fileInput = document.getElementById('batchCsvFileInput');
  if (fileInput) fileInput.value = '';
}

// ── Log Stream Management ────────────────────────────────────────────────────
function appendLog(timestamp, level, message) {
  const terminal = document.getElementById('logsTerminalContent');
  if (!terminal) return;

  const row = document.createElement('div');
  row.className = 'flex items-start gap-2.5 leading-relaxed';

  let tagColor = 'text-secondary';
  if (level === 'ERROR') tagColor = 'text-error font-bold';
  else if (level === 'SYSTEM' || level === 'SUCCESS') tagColor = 'text-tertiary-container font-bold';
  else if (level === 'BATCH') tagColor = 'text-primary-container font-bold';

  row.innerHTML = `
    <span class="text-on-surface-variant/60 select-none font-mono text-[11px]">[${timestamp}]</span>
    <span class="${tagColor} select-none font-mono text-[11px]">[${level}]</span>
    <span class="text-on-surface font-mono text-xs flex-1 break-words">${escapeHtml(message)}</span>
  `;

  terminal.appendChild(row);

  // Auto scroll
  terminal.scrollTop = terminal.scrollHeight;
}

function clearLogs() {
  const terminal = document.getElementById('logsTerminalContent');
  if (terminal) terminal.innerHTML = '';
}

// ── Runtime & Button Helpers ─────────────────────────────────────────────────
function startRuntimeTimer() {
  AppState.runStartTime = Date.now();
  const timerElem = document.getElementById('telemetryRuntimeText');
  if (AppState.runtimeInterval) clearInterval(AppState.runtimeInterval);
  AppState.runtimeInterval = setInterval(() => {
    const elapsed = ((Date.now() - AppState.runStartTime) / 1000).toFixed(2);
    if (timerElem) timerElem.innerText = `${elapsed}s`;
  }, 100);
}

function stopRuntimeTimer(finalSeconds = null) {
  if (AppState.runtimeInterval) clearInterval(AppState.runtimeInterval);
  const timerElem = document.getElementById('telemetryRuntimeText');
  if (timerElem && finalSeconds !== null) {
    timerElem.innerText = `${finalSeconds}s`;
  }
}

function setRunButtonState(isRunning) {
  AppState.isRunning = isRunning;
  const btn = document.getElementById('btnRunPipeline');
  if (!btn) return;

  if (isRunning) {
    btn.disabled = true;
    btn.innerHTML = `
      <span class="flex items-center">${SVG_ICONS.spinner_lg}</span>
      <span>Synthesizing...</span>
    `;
    btn.classList.add('opacity-80');
  } else {
    btn.disabled = false;
    btn.innerHTML = `
      <span class="flex items-center">${SVG_ICONS.play_arrow}</span>
      <span>Run Pipeline</span>
      <span class="ml-1 text-code-sm px-2 py-0.5 rounded-full bg-surface-container-lowest/20 font-normal">⌘↵</span>
    `;
    btn.classList.remove('opacity-80');
  }
}

// ── Copy & Download Utilities ────────────────────────────────────────────────
function copyGeneratedConfig() {
  if (!AppState.latestConfig) {
    showToast('No configuration to copy.', 'info');
    return;
  }
  const str = JSON.stringify(AppState.latestConfig, null, 2);
  navigator.clipboard.writeText(str).then(() => {
    const label = document.getElementById('configCopyLabel');
    if (label) {
      label.innerText = 'Copied!';
      setTimeout(() => { label.innerText = 'Copy Config'; }, 1500);
    }
    showToast('Configuration JSON copied to clipboard!', 'success');
  });
}

function copyCrawlerId() {
  const input = document.getElementById('crawlerIdInput');
  if (!input || !input.value) return;
  navigator.clipboard.writeText(input.value).then(() => {
    const feedback = document.getElementById('copyFeedbackText');
    if (feedback) {
      feedback.innerText = 'COPIED!';
      feedback.classList.add('text-tertiary-container');
      setTimeout(() => {
        feedback.innerText = 'copy';
        feedback.classList.remove('text-tertiary-container');
      }, 1500);
    }
  });
}

function downloadResultsCsv() {
  window.location.href = '/api/batch/download/results';
}

function downloadReplayLog() {
  window.location.href = '/api/batch/download/replay';
}

function incrementJobs() {
  const input = document.getElementById('jobsOnPageInput');
  let val = parseInt(input.value, 10) || 0;
  input.value = val + 5;
}

function decrementJobs() {
  const input = document.getElementById('jobsOnPageInput');
  let val = parseInt(input.value, 10) || 5;
  if (val > 5) input.value = val - 5;
}

// ── Keyboard Shortcuts ───────────────────────────────────────────────────────
function initKeyboardShortcuts() {
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      triggerRunPipeline();
    }
  });
}

// ── Toast Notifications ──────────────────────────────────────────────────────
function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const toast = document.createElement('div');
  const iconSvg = type === 'success' ? SVG_ICONS.check_lg : type === 'error' ? SVG_ICONS.error_lg : SVG_ICONS.info;
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span class="flex items-center">${iconSvg}</span>
    <span class="font-medium flex-1">${escapeHtml(message)}</span>
  `;

  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(8px)';
    setTimeout(() => toast.remove(), 200);
  }, 3500);
}

function escapeHtml(str) {
  if (typeof str !== 'string') return String(str || '');
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
