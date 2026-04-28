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
const warningText = document.getElementById("warning-text");
const meterBar = document.getElementById("meter-bar");
const documentPath = document.getElementById("document-path");
const logOutput = document.getElementById("log-output");
const compileOutput = document.getElementById("compile-output");
const deviceSelect = document.getElementById("device-select");
const transcriberSelect = document.getElementById("transcriber-select");
const llmProviderSelect = document.getElementById("llm-provider-select");
const autoReflectToggle = document.getElementById("auto-reflect-toggle");
const startButton = document.getElementById("start-button");
const stopButton = document.getElementById("stop-button");
const saveButton = document.getElementById("save-button");
const compileButton = document.getElementById("compile-button");

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

async function loadLlmProviders() {
  const data = await api("/api/llm/providers");
  llmProviderSelect.innerHTML = "";

  for (const provider of data.providers) {
    const option = document.createElement("option");
    option.value = provider.id;
    option.disabled = !provider.configured;
    const status = provider.configured ? "" : " [not configured]";
    option.textContent = `${provider.label}: ${provider.model}${status}`;
    llmProviderSelect.appendChild(option);
  }

  if (data.selected) {
    llmProviderSelect.value = data.selected;
  }
  updateLlmProviderVisibility();
}

function updateLlmProviderVisibility() {
  const apiSelected = transcriberSelect.value === "api";
  llmProviderSelect.hidden = !apiSelected;
  llmProviderSelect.disabled = !apiSelected;
}

async function saveDocument() {
  await api("/api/document", {
    method: "POST",
    body: JSON.stringify({ text: texEditor.value }),
  });
  state.localDirty = false;
  await refreshDocument(true);
}

async function startRecording() {
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

llmProviderSelect.addEventListener("change", async () => {
  try {
    await api("/api/llm/provider", {
      method: "POST",
      body: JSON.stringify({ provider: llmProviderSelect.value }),
    });
  } catch (error) {
    alert(error.message);
    await loadLlmProviders();
  }
});

transcriberSelect.addEventListener("change", updateLlmProviderVisibility);

async function boot() {
  await loadDevices();
  await loadLlmProviders();
  await refreshDocument(true);
  await refreshStatus();
  state.pollHandle = setInterval(async () => {
    try {
      await refreshStatus();
    } catch (error) {
      logOutput.textContent += `\nstatus failed: ${error.message}`;
    }
  }, 2000);
}

boot();
