const state = {
  documentVersion: 0,
  localDirty: false,
  pollHandle: null,
  saveHandle: null,
};

const texEditor = document.getElementById("tex-editor");
const recordState = document.getElementById("recording-state");
const elapsed = document.getElementById("elapsed");
const recordingPath = document.getElementById("recording-path");
const transcriptPath = document.getElementById("transcript-path");
const transcriptList = document.getElementById("transcript-list");
const transcriptCount = document.getElementById("transcript-count");
const summaryTargetStatus = document.getElementById("summary-target-status");
const summaryTargetDetail = document.getElementById("summary-target-detail");
const warningText = document.getElementById("warning-text");
const meterBar = document.getElementById("meter-bar");
const documentPath = document.getElementById("document-path");
const logOutput = document.getElementById("log-output");
const compileOutput = document.getElementById("compile-output");
const deviceSelect = document.getElementById("device-select");
const transcriberSelect = document.getElementById("transcriber-select");
const autoReflectToggle = document.getElementById("auto-reflect-toggle");
const startButton = document.getElementById("start-button");
const stopButton = document.getElementById("stop-button");
const summaryButton = document.getElementById("summary-button");
const saveButton = document.getElementById("save-button");
const compileButton = document.getElementById("compile-button");
const summaryButtonText = "報告事項へ要約反映";

function formatHms(totalSeconds) {
  const total = Math.max(0, Math.floor(totalSeconds || 0));
  const hours = String(Math.floor(total / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const seconds = String(total % 60).padStart(2, "0");
  return `${hours}:${minutes}:${seconds}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
}

function renderTranscript(entries) {
  transcriptCount.textContent = `${entries.length} segments`;
  transcriptList.innerHTML = "";
  for (const entry of entries.slice().reverse()) {
    const wrapper = document.createElement("article");
    wrapper.className = "transcript-entry";
    wrapper.innerHTML = `
      <header>
        <span>segment ${String(entry.index).padStart(3, "0")}</span>
        <span>${formatHms(entry.start_sec)} - ${formatHms(entry.end_sec)}</span>
      </header>
      <div>${entry.text ? entry.text.replaceAll("\n", "<br>") : "(no speech)"}</div>
    `;
    transcriptList.appendChild(wrapper);
  }
}

async function refreshDocument(force = false) {
  const data = await api("/api/document");
  documentPath.textContent = data.path;
  if (force || (!state.localDirty && data.version !== state.documentVersion)) {
    texEditor.value = data.text;
    state.localDirty = false;
  }
  state.documentVersion = data.version;
}

async function refreshStatus() {
  const data = await api("/api/status");
  recordState.textContent = data.recording_active ? "状態: 録音中" : "状態: 待機中";
  elapsed.textContent = formatHms(data.elapsed_seconds);
  recordingPath.textContent = data.recording_path || "-";
  transcriptPath.textContent = data.transcript_path || "-";
  warningText.textContent = data.status_warning || "警告なし";
  meterBar.style.width = `${Math.min(100, (data.current_rms / 2500) * 100)}%`;
  autoReflectToggle.checked = data.auto_reflect;
  renderTranscript(data.transcript_entries || []);
  logOutput.textContent = (data.logs || []).join("\n");
  compileOutput.textContent = data.compile_log || "";

  if (!state.localDirty && data.document_version !== state.documentVersion) {
    await refreshDocument();
  }
  await refreshReportSummaryTarget();
}

async function loadDevices() {
  const data = await api("/api/devices");
  deviceSelect.innerHTML = "";
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "Default Input Device";
  deviceSelect.appendChild(defaultOption);

  for (const device of data.devices.filter((item) => item.max_input_channels > 0)) {
    const option = document.createElement("option");
    option.value = String(device.id);
    option.textContent = `${device.id}: ${device.name}${device.is_default_input ? " [default]" : ""}`;
    deviceSelect.appendChild(option);
    if (device.is_default_input) {
      deviceSelect.value = String(device.id);
    }
  }
}

function appendClientLog(message) {
  logOutput.textContent += `${logOutput.textContent ? "\n" : ""}${message}`;
}

function renderReportSummaryTarget(data) {
  if (!data.ok) {
    summaryTargetStatus.textContent = "挿入先未検出";
    summaryTargetDetail.textContent = [
      `ファイル: ${data.document_path || "-"}`,
      `理由: ${data.reason || "不明"}`,
      `文字起こし: ${data.transcript_entry_count || 0} segments / ${data.transcript_text_chars || 0}文字`,
    ].join("\n");
    return;
  }

  const modeText =
    data.mode === "fill-empty-items" ? "既存の空欄へ入力" : "空欄がないため末尾へ追加";
  summaryTargetStatus.textContent = `${data.line}行目 / ${modeText}`;
  const targetLines = (data.target_lines || [])
    .map((target) => `  - ${target.line}行目: ${target.preview || "\\item"}`)
    .join("\n");
  const detailLines = [
    `ファイル: ${data.document_path}`,
    `最初の入力先: ${data.line}行目 / ${modeText}`,
    `説明: ${data.detail}`,
    `目印行: ${data.preview || "(空行)"}`,
    `報告事項行: ${data.report_line ? `${data.report_line}行目` : "報告事項内"}`,
    `空の \\item 数: ${data.blank_item_count}`,
    `文字起こし: ${data.transcript_entry_count} segments / ${data.transcript_text_chars}文字`,
    `前回の自動要約項目数: ${data.report_summary_count}`,
  ];
  if (targetLines) {
    detailLines.push(`入力予定の空欄:\n${targetLines}`);
  }
  if (!data.transcript_text_chars) {
    detailLines.push("注意: 要約対象の文字起こしがまだありません。録音後に反映してください。");
  }
  summaryTargetDetail.textContent = detailLines.join("\n");
}

async function refreshReportSummaryTarget() {
  try {
    const data = await api("/api/report-summary/target");
    renderReportSummaryTarget(data);
  } catch (error) {
    summaryTargetStatus.textContent = "確認失敗";
    summaryTargetDetail.textContent = `挿入先の確認に失敗しました: ${error.message}`;
  }
}

function selectedApiProvider() {
  const [mode, provider] = transcriberSelect.value.split(":");
  if (mode !== "api") {
    return null;
  }
  return provider || "openai";
}

async function syncSelectedApiProvider() {
  const provider = selectedApiProvider();
  if (!provider) {
    return;
  }
  await api("/api/llm/provider", {
    method: "POST",
    body: JSON.stringify({ provider }),
  });
}

async function saveDocument() {
  const result = await api("/api/document", {
    method: "POST",
    body: JSON.stringify({ text: texEditor.value, version: state.documentVersion }),
  });
  state.documentVersion = result.version;
  state.localDirty = false;
  await refreshDocument(true);
}

async function startRecording() {
  await syncSelectedApiProvider();
  await api("/api/recording/start", {
    method: "POST",
    body: JSON.stringify({
      device: deviceSelect.value === "" ? null : Number(deviceSelect.value),
      transcriber: transcriberSelect.value,
      auto_reflect: autoReflectToggle.checked,
      language: "ja",
    }),
  });
  await refreshStatus();
}

async function stopRecording() {
  await api("/api/recording/stop", { method: "POST", body: "{}" });
  await refreshStatus();
}

async function compileDocument() {
  const result = await api("/api/compile", { method: "POST", body: "{}" });
  compileOutput.textContent = result.log || "";
}

async function summarizeReportItems() {
  await syncSelectedApiProvider();
  if (state.localDirty) {
    await saveDocument();
  }
  await refreshReportSummaryTarget();
  const provider = selectedApiProvider();
  const result = await api("/api/report-summary", {
    method: "POST",
    body: JSON.stringify(provider ? { provider } : {}),
  });
  appendClientLog(`report summary inserted: ${result.item_count} items`);
  if (result.version) {
    state.documentVersion = result.version;
  }
  await refreshDocument(true);
  await refreshStatus();
  await refreshReportSummaryTarget();
}

function scheduleSave() {
  clearTimeout(state.saveHandle);
  state.saveHandle = setTimeout(async () => {
    try {
      await saveDocument();
    } catch (error) {
      logOutput.textContent += `\nsave failed: ${error.message}`;
    }
  }, 900);
}

texEditor.addEventListener("input", () => {
  state.localDirty = true;
  scheduleSave();
});

startButton.addEventListener("click", async () => {
  try {
    await startRecording();
  } catch (error) {
    alert(error.message);
  }
});

stopButton.addEventListener("click", async () => {
  try {
    await stopRecording();
  } catch (error) {
    alert(error.message);
  }
});

summaryButton.addEventListener("click", async () => {
  summaryButton.disabled = true;
  summaryButton.textContent = "要約中...";
  try {
    await summarizeReportItems();
  } catch (error) {
    appendClientLog(`report summary failed: ${error.message}`);
    alert(error.message);
  } finally {
    summaryButton.disabled = false;
    summaryButton.textContent = summaryButtonText;
  }
});

saveButton.addEventListener("click", async () => {
  try {
    await saveDocument();
  } catch (error) {
    alert(error.message);
  }
});

compileButton.addEventListener("click", async () => {
  try {
    await compileDocument();
  } catch (error) {
    alert(error.message);
  }
});

autoReflectToggle.addEventListener("change", async () => {
  try {
    await api("/api/auto-reflect", {
      method: "POST",
      body: JSON.stringify({ enabled: autoReflectToggle.checked }),
    });
  } catch (error) {
    alert(error.message);
  }
});

transcriberSelect.addEventListener("change", async () => {
  try {
    await syncSelectedApiProvider();
  } catch (error) {
    alert(error.message);
  }
});

async function boot() {
  try {
    await refreshDocument(true);
  } catch (error) {
    appendClientLog(`document load failed: ${error.message}`);
  }

  try {
    await loadDevices();
  } catch (error) {
    deviceSelect.innerHTML = "";
    const defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "Default Input Device";
    deviceSelect.appendChild(defaultOption);
    appendClientLog(`device load failed: ${error.message}`);
  }

  try {
    await refreshStatus();
  } catch (error) {
    appendClientLog(`status failed: ${error.message}`);
  }

  state.pollHandle = setInterval(async () => {
    try {
      await refreshStatus();
    } catch (error) {
      appendClientLog(`status failed: ${error.message}`);
    }
  }, 2000);
}

boot();
