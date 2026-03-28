const els = {
  fileInput: document.getElementById("fileInput"),
  uploadBtn: document.getElementById("uploadBtn"),
  uploadInfo: document.getElementById("uploadInfo"),
  modelSelect: document.getElementById("modelSelect"),
  ocrPresetSelect: document.getElementById("ocrPresetSelect"),
  pageFrom: document.getElementById("pageFrom"),
  pageTo: document.getElementById("pageTo"),
  estimateBtn: document.getElementById("estimateBtn"),
  estimateBox: document.getElementById("estimateBox"),
  estimateUsd: document.getElementById("estimateUsd"),
  estimatePages: document.getElementById("estimatePages"),
  estimateLines: document.getElementById("estimateLines"),
  estimateInput: document.getElementById("estimateInput"),
  estimateOutput: document.getElementById("estimateOutput"),
  acceptWrap: document.getElementById("acceptWrap"),
  acceptEstimate: document.getElementById("acceptEstimate"),
  translateBtn: document.getElementById("translateBtn"),
  cancelJobBtn: document.getElementById("cancelJobBtn"),
  retrySuggestedBtn: document.getElementById("retrySuggestedBtn"),
  jobStatus: document.getElementById("jobStatus"),
  progressBar: document.getElementById("progressBar"),
  actualCost: document.getElementById("actualCost"),
  downloadBtn: document.getElementById("downloadBtn"),
  tabSavedBtn: document.getElementById("tabSavedBtn"),
  tabHistoryBtn: document.getElementById("tabHistoryBtn"),
  savedPanel: document.getElementById("savedPanel"),
  historyPanel: document.getElementById("historyPanel"),
  savedList: document.getElementById("savedList"),
  savedEmpty: document.getElementById("savedEmpty"),
  historySummary: document.getElementById("historySummary"),
  historyList: document.getElementById("historyList"),
  historyEmpty: document.getElementById("historyEmpty"),
};

const state = {
  fileId: null,
  pageCount: 0,
  apiKeyConfigured: false,
  estimatedCostUsd: null,
  jobId: null,
  estimateSignature: null,
  ocrPreset: "balanced",
  retrySuggestionPageFrom: null,
};

function setStatus(message) {
  els.jobStatus.textContent = message;
}

function formatBytes(size) {
  const n = Number(size || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatUsd(value) {
  const n = Number(value || 0);
  return `$${n.toFixed(6)}`;
}

function formatDuration(seconds) {
  const n = Math.max(0, Number(seconds || 0));
  if (n < 60) return `${n.toFixed(1)}s`;
  const mins = Math.floor(n / 60);
  const secs = Math.floor(n % 60);
  if (mins < 60) return `${mins}m ${secs}s`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hours}h ${remMins}m`;
}

function switchTab(tab) {
  const showSaved = tab === "saved";
  els.savedPanel.classList.toggle("hidden", !showSaved);
  els.historyPanel.classList.toggle("hidden", showSaved);
  els.tabSavedBtn.classList.toggle("tabBtnActive", showSaved);
  els.tabHistoryBtn.classList.toggle("tabBtnActive", !showSaved);
}

function invalidateEstimateIfInputsChanged() {
  state.estimatedCostUsd = null;
  state.estimateSignature = null;
  els.acceptEstimate.checked = false;
  els.translateBtn.disabled = true;
  els.acceptWrap.classList.add("hidden");
  els.estimateBox.classList.add("hidden");
  state.retrySuggestionPageFrom = null;
  els.retrySuggestedBtn.classList.add("hidden");
}

function resetRetrySuggestion() {
  state.retrySuggestionPageFrom = null;
  els.retrySuggestedBtn.classList.add("hidden");
}

function parseOptionalPageValue(rawValue) {
  if (!rawValue || !rawValue.trim()) {
    return null;
  }
  const parsed = Number(rawValue);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error("Page numbers must be integers >= 1.");
  }
  return parsed;
}

function getCurrentRange() {
  const pageFrom = parseOptionalPageValue(els.pageFrom.value);
  const pageTo = parseOptionalPageValue(els.pageTo.value);
  if (pageFrom !== null && pageTo !== null && pageFrom > pageTo) {
    throw new Error("From page cannot be greater than To page.");
  }
  return { pageFrom, pageTo };
}

function buildEstimateSignature({ model, pageFrom, pageTo }) {
  const resolvedFrom = pageFrom === null ? 1 : pageFrom;
  const resolvedTo = pageTo === null ? state.pageCount : pageTo;
  return JSON.stringify({
    fileId: state.fileId,
    model,
    pageFrom: resolvedFrom,
    pageTo: resolvedTo,
    ocrPreset: state.ocrPreset,
  });
}

async function loadSavedDownloads() {
  const res = await fetch("/api/downloads");
  if (!res.ok) {
    throw new Error("Cannot load saved files list.");
  }
  const data = await res.json();
  const items = Array.isArray(data.items) ? data.items : [];

  els.savedList.innerHTML = "";
  els.savedEmpty.classList.toggle("hidden", items.length > 0);

  for (const item of items) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = item.download_url;
    a.download = item.filename;
    a.textContent = item.filename;

    const meta = document.createElement("span");
    meta.className = "savedMeta";
    meta.textContent = `${formatBytes(item.size_bytes)} | ${item.updated_at}`;

    li.appendChild(a);
    li.appendChild(meta);
    els.savedList.appendChild(li);
  }
}

async function loadHistory() {
  const res = await fetch("/api/history");
  if (!res.ok) {
    throw new Error("Cannot load translation history.");
  }
  const data = await res.json();
  const summary = data.summary || {};
  const items = Array.isArray(data.items) ? data.items : [];

  els.historySummary.textContent =
    `Total spend: ${formatUsd(summary.total_actual_cost_usd)} | ` +
    `Jobs: ${summary.total_jobs || 0} (done ${summary.successful_jobs || 0}, failed ${summary.failed_jobs || 0}, cancelled ${summary.cancelled_jobs || 0}) | ` +
    `Lines: ${summary.total_lines_translated || 0} | ` +
    `Output: ${formatBytes(summary.total_output_size_bytes || 0)} | ` +
    `Avg: T ${formatDuration(summary.average_translate_duration_seconds || 0)} / R ${formatDuration(summary.average_render_duration_seconds || 0)} / S ${formatDuration(summary.average_save_duration_seconds || 0)}`;

  els.historyList.innerHTML = "";
  els.historyEmpty.classList.toggle("hidden", items.length > 0);

  for (const item of items) {
    const li = document.createElement("li");
    const statusClass = item.status === "done" ? "historyStatusDone" : "historyStatusFailed";
    const fileLabel = item.translated_filename || item.original_filename || item.job_id;
    const title = document.createElement("div");
    title.className = `historyTitle ${statusClass}`;
    title.textContent = `[${String(item.status || "unknown").toUpperCase()}] ${fileLabel}`;

    const meta = document.createElement("div");
    meta.className = "historyMeta";
    meta.textContent =
      `Estimate ${formatUsd(item.estimated_cost_usd)} -> Actual ${formatUsd(item.actual_cost_usd)} | ` +
      `Duration ${formatDuration(item.duration_seconds)} | Lines ${item.selected_segments || 0} | ` +
      `Pages ${item.page_from || "-"}-${item.page_to || "-"} | Size ${formatBytes(item.output_size_bytes || 0)} | ` +
      `Finished ${item.finished_at || "-"}`;

    const model = document.createElement("div");
    model.className = "historyMeta";
    model.textContent =
      `Model ${item.model || "-"} | Mode ${String(item.extraction_mode || "text").toUpperCase()} | OCR preset ${item.ocr_preset || "-"} | ` +
      `Phase ${item.phase_last || "-"} | T ${formatDuration(item.translate_duration_seconds)} / R ${formatDuration(item.render_duration_seconds)} / S ${formatDuration(item.save_duration_seconds)}`;

    li.appendChild(title);
    li.appendChild(meta);
    li.appendChild(model);

    if (item.timeout_reason || item.retry_suggestion_page_from) {
      const extra = document.createElement("div");
      extra.className = "historyMeta";
      const timeoutText = item.timeout_reason ? `Timeout: ${item.timeout_reason}` : "";
      const retryText = item.retry_suggestion_page_from ? ` Retry suggestion from page ${item.retry_suggestion_page_from}.` : "";
      extra.textContent = `${timeoutText}${retryText}`.trim();
      li.appendChild(extra);
    }

    els.historyList.appendChild(li);
  }
}

async function loadModels() {
  const res = await fetch("/api/models");
  const data = await res.json();
  state.apiKeyConfigured = Boolean(data.openai_api_key_configured);

  els.modelSelect.innerHTML = "";
  for (const model of data.models) {
    const opt = document.createElement("option");
    opt.value = model.id;
    opt.textContent = `${model.id} | in $${model.input_per_million}/1M | out $${model.output_per_million}/1M`;
    if (model.id === data.default_model) {
      opt.selected = true;
    }
    els.modelSelect.appendChild(opt);
  }

  els.ocrPresetSelect.innerHTML = "";
  const presets = Array.isArray(data.ocr_presets) ? data.ocr_presets : [];
  for (const preset of presets) {
    const opt = document.createElement("option");
    opt.value = preset.id;
    opt.textContent = `${preset.label} (dpi ${preset.dpi}, conf ${preset.min_confidence})`;
    if (preset.id === data.default_ocr_preset) {
      opt.selected = true;
    }
    els.ocrPresetSelect.appendChild(opt);
  }
  state.ocrPreset = els.ocrPresetSelect.value || data.default_ocr_preset || "balanced";

  if (!state.apiKeyConfigured) {
    setStatus("Chua co OPENAI_API_KEY trong .env. Ban van estimate duoc, nhung chua the translate.");
  }
}

async function uploadPdf() {
  const file = els.fileInput.files[0];
  if (!file) {
    alert("Please select a PDF file.");
    return;
  }

  const formData = new FormData();
  formData.append("file", file);
  formData.append("ocr_preset", state.ocrPreset);

  els.uploadInfo.textContent = "Uploading and parsing PDF...";
  const res = await fetch("/api/upload", {
    method: "POST",
    body: formData,
  });

  const data = await res.json();
  if (!res.ok) {
    els.uploadInfo.textContent = data.detail || "Upload failed.";
    return;
  }

  state.fileId = data.file_id;
  state.pageCount = Number(data.pages || 0);
  state.estimatedCostUsd = null;
  state.jobId = null;
  state.estimateSignature = null;
  els.pageFrom.value = "";
  els.pageTo.value = "";
  const extractionMode = String(data.extraction_mode || "text").toUpperCase();
  const ocrSuffix = data.extraction_mode === "ocr" ? ` | OCR pages: ${data.ocr_pages || 0}` : "";
  els.uploadInfo.textContent = `Uploaded: ${data.filename} | Pages: ${data.pages} | Lines: ${data.segments} | Source tokens: ${data.source_tokens} | Mode: ${extractionMode} | OCR preset: ${data.ocr_preset || state.ocrPreset}${ocrSuffix}`;
  els.estimateBtn.disabled = false;
  els.translateBtn.disabled = true;
  els.cancelJobBtn.classList.add("hidden");
  resetRetrySuggestion();
  els.acceptEstimate.checked = false;
  els.acceptWrap.classList.add("hidden");
  els.estimateBox.classList.add("hidden");
  els.downloadBtn.classList.add("hidden");
  els.progressBar.style.width = "0%";
  els.actualCost.textContent = "";
  setStatus("Ready to estimate.");
}

async function estimateCost() {
  if (!state.fileId) {
    alert("Upload a PDF first.");
    return;
  }

  const model = els.modelSelect.value;
  let pageRange;
  try {
    pageRange = getCurrentRange();
  } catch (err) {
    alert(err.message);
    return;
  }

  const res = await fetch("/api/estimate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file_id: state.fileId,
      model,
      page_from: pageRange.pageFrom,
      page_to: pageRange.pageTo,
    }),
  });

  const data = await res.json();
  if (!res.ok) {
    alert(data.detail || "Estimate failed.");
    return;
  }

  state.estimatedCostUsd = data.estimated_cost_usd;
  state.estimateSignature = buildEstimateSignature({
    model,
    pageFrom: data.page_from,
    pageTo: data.page_to,
  });
  els.estimateUsd.textContent = Number(data.estimated_cost_usd).toFixed(6);
  els.estimatePages.textContent = `${data.page_from} - ${data.page_to}`;
  els.estimateLines.textContent = data.selected_segments;
  els.estimateInput.textContent = data.source_tokens;
  els.estimateOutput.textContent = data.estimated_output_tokens;
  els.estimateBox.classList.remove("hidden");
  els.acceptWrap.classList.remove("hidden");
  els.translateBtn.disabled = !els.acceptEstimate.checked;
  setStatus("Estimate ready. Confirm to continue.");
}

function updateTranslateButtonState() {
  const enabled = Boolean(state.fileId && state.estimatedCostUsd !== null && els.acceptEstimate.checked);
  els.translateBtn.disabled = !enabled;
}

async function startTranslation() {
  if (!state.fileId || state.estimatedCostUsd === null) {
    alert("Please upload and estimate cost first.");
    return;
  }
  if (!state.apiKeyConfigured) {
    alert("OPENAI_API_KEY is missing in .env. Please set it before translating.");
    return;
  }

  let pageRange;
  try {
    pageRange = getCurrentRange();
  } catch (err) {
    alert(err.message);
    return;
  }

  const currentSignature = buildEstimateSignature({
    model: els.modelSelect.value,
    pageFrom: pageRange.pageFrom,
    pageTo: pageRange.pageTo,
  });
  if (currentSignature !== state.estimateSignature) {
    alert("Model, OCR preset, or page range changed. Please estimate again before translating.");
    return;
  }

  const confirmed = window.confirm(
    `Estimated cost is $${Number(state.estimatedCostUsd).toFixed(6)} USD.\nSelected pages: ${pageRange.pageFrom ?? 1} - ${pageRange.pageTo ?? state.pageCount} / ${state.pageCount}.\nOCR preset: ${state.ocrPreset}.\nDo you accept and start translation?`
  );
  if (!confirmed) {
    return;
  }

  const res = await fetch("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file_id: state.fileId,
      model: els.modelSelect.value,
      accepted_estimate_usd: state.estimatedCostUsd,
      accept_estimate: true,
      page_from: pageRange.pageFrom,
      page_to: pageRange.pageTo,
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.detail || "Could not start translation.");
    return;
  }

  state.jobId = data.job_id;
  els.downloadBtn.classList.add("hidden");
  els.cancelJobBtn.classList.remove("hidden");
  resetRetrySuggestion();
  setStatus("Job started...");
  pollJobStatus();
}

function applyRetrySuggestion() {
  const suggested = Number(state.retrySuggestionPageFrom || 0);
  if (!suggested || !Number.isFinite(suggested)) {
    return;
  }
  els.pageFrom.value = String(Math.max(1, Math.floor(suggested)));
  const currentTo = parseOptionalPageValue(els.pageTo.value || "") || null;
  if (currentTo !== null && currentTo < suggested) {
    els.pageTo.value = "";
  }
  invalidateEstimateIfInputsChanged();
  setStatus(`Retry suggestion applied from page ${suggested}. Please estimate again.`);
}

async function cancelCurrentJob() {
  if (!state.jobId) {
    return;
  }
  const confirmed = window.confirm("Cancel current translation job?");
  if (!confirmed) {
    return;
  }

  const res = await fetch(`/api/job/${state.jobId}/cancel`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) {
    alert(data.detail || "Could not cancel job.");
    return;
  }
  setStatus(data.message || "Cancellation requested.");
}

async function pollJobStatus() {
  if (!state.jobId) {
    return;
  }

  const tick = async () => {
    const res = await fetch(`/api/job/${state.jobId}`);
    const data = await res.json();

    if (!res.ok) {
      setStatus(data.detail || "Error while reading job status.");
      return;
    }

    const progress = Number(data.progress || 0);
    els.progressBar.style.width = `${Math.max(0, Math.min(100, progress))}%`;
    const mode = String(data.extraction_mode || "text").toUpperCase();
    const phase = String(data.phase || "pending");
    setStatus(
      `${data.status} | ${progress.toFixed(2)}% | phase ${phase} (${Number(data.phase_progress || 0).toFixed(1)}%) | mode ${mode} | pages ${data.page_from}-${data.page_to} | ${data.message || ""}`
    );
    els.actualCost.textContent =
      `Estimate: ${formatUsd(data.estimated_cost_usd)} | Actual so far: ${formatUsd(data.actual_cost_usd)} | ` +
      `T ${formatDuration(data.translate_duration_seconds)} / R ${formatDuration(data.render_duration_seconds)} / S ${formatDuration(data.save_duration_seconds)}`;

    if (data.status === "done" && data.download_ready) {
      els.cancelJobBtn.classList.add("hidden");
      resetRetrySuggestion();
      els.downloadBtn.href = `/api/download/${state.jobId}`;
      if (data.translated_filename) {
        els.downloadBtn.download = data.translated_filename;
      }
      els.downloadBtn.classList.remove("hidden");
      setStatus("Done. You can download the translated PDF.");
      loadSavedDownloads().catch((err) => {
        setStatus(`Done, but could not refresh saved files: ${err.message}`);
      });
      loadHistory().catch((err) => {
        setStatus(`Done, but could not refresh history: ${err.message}`);
      });
      return;
    }

    if (data.status === "failed") {
      els.cancelJobBtn.classList.add("hidden");
      state.retrySuggestionPageFrom = data.retry_suggestion_page_from || null;
      els.retrySuggestedBtn.classList.toggle("hidden", !state.retrySuggestionPageFrom);
      const retryHint = data.retry_suggestion_page_from
        ? ` Suggested retry start page: ${data.retry_suggestion_page_from}.`
        : "";
      const timeoutHint = data.timeout_reason ? ` Timeout: ${data.timeout_reason}.` : "";
      setStatus(`Failed: ${data.message || "Unknown error."}${timeoutHint}${retryHint}`);
      loadHistory().catch((err) => {
        setStatus(`Failed, and could not refresh history: ${err.message}`);
      });
      return;
    }

    if (data.status === "cancelled") {
      els.cancelJobBtn.classList.add("hidden");
      state.retrySuggestionPageFrom = data.retry_suggestion_page_from || null;
      els.retrySuggestedBtn.classList.toggle("hidden", !state.retrySuggestionPageFrom);
      const retryHint = data.retry_suggestion_page_from
        ? ` Suggested retry start page: ${data.retry_suggestion_page_from}.`
        : "";
      setStatus(`Cancelled.${retryHint}`);
      loadHistory().catch((err) => {
        setStatus(`Cancelled, and could not refresh history: ${err.message}`);
      });
      return;
    }

    setTimeout(tick, 1200);
  };

  tick();
}

els.uploadBtn.addEventListener("click", uploadPdf);
els.estimateBtn.addEventListener("click", estimateCost);
els.acceptEstimate.addEventListener("change", updateTranslateButtonState);
els.translateBtn.addEventListener("click", startTranslation);
els.cancelJobBtn.addEventListener("click", cancelCurrentJob);
els.retrySuggestedBtn.addEventListener("click", applyRetrySuggestion);
els.modelSelect.addEventListener("change", invalidateEstimateIfInputsChanged);
els.ocrPresetSelect.addEventListener("change", () => {
  state.ocrPreset = els.ocrPresetSelect.value || "balanced";
  invalidateEstimateIfInputsChanged();
});
els.pageFrom.addEventListener("input", invalidateEstimateIfInputsChanged);
els.pageTo.addEventListener("input", invalidateEstimateIfInputsChanged);
els.tabSavedBtn.addEventListener("click", () => switchTab("saved"));
els.tabHistoryBtn.addEventListener("click", () => switchTab("history"));

loadModels().catch((err) => {
  setStatus(`Could not load models: ${err.message}`);
});

loadSavedDownloads().catch((err) => {
  setStatus(`Could not load saved downloads: ${err.message}`);
});

loadHistory().catch((err) => {
  setStatus(`Could not load history: ${err.message}`);
});
