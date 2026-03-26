const els = {
  fileInput: document.getElementById("fileInput"),
  uploadBtn: document.getElementById("uploadBtn"),
  uploadInfo: document.getElementById("uploadInfo"),
  modelSelect: document.getElementById("modelSelect"),
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
  jobStatus: document.getElementById("jobStatus"),
  progressBar: document.getElementById("progressBar"),
  actualCost: document.getElementById("actualCost"),
  downloadBtn: document.getElementById("downloadBtn"),
  savedList: document.getElementById("savedList"),
  savedEmpty: document.getElementById("savedEmpty"),
};

const state = {
  fileId: null,
  pageCount: 0,
  apiKeyConfigured: false,
  estimatedCostUsd: null,
  jobId: null,
  estimateSignature: null,
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
  els.uploadInfo.textContent = `Uploaded: ${data.filename} | Pages: ${data.pages} | Lines: ${data.segments} | Source tokens: ${data.source_tokens}`;
  els.estimateBtn.disabled = false;
  els.translateBtn.disabled = true;
  els.acceptEstimate.checked = false;
  els.acceptWrap.classList.add("hidden");
  els.estimateBox.classList.add("hidden");
  els.downloadBtn.classList.add("hidden");
  els.progressBar.style.width = "0%";
  els.actualCost.textContent = "";
  setStatus("Ready to estimate.");
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
  });
}

function invalidateEstimateIfInputsChanged() {
  state.estimatedCostUsd = null;
  state.estimateSignature = null;
  els.acceptEstimate.checked = false;
  els.translateBtn.disabled = true;
  els.acceptWrap.classList.add("hidden");
  els.estimateBox.classList.add("hidden");
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
    alert("Model or page range changed. Please estimate again before translating.");
    return;
  }

  const confirmed = window.confirm(
    `Estimated cost is $${Number(state.estimatedCostUsd).toFixed(6)} USD.\nSelected pages: ${pageRange.pageFrom ?? 1} - ${pageRange.pageTo ?? state.pageCount} / ${state.pageCount}.\nDo you accept and start translation?`
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
  setStatus("Job started...");
  pollJobStatus();
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
    setStatus(`${data.status} | ${progress.toFixed(2)}% | pages ${data.page_from}-${data.page_to} | ${data.message || ""}`);
    els.actualCost.textContent = `Estimate: $${Number(data.estimated_cost_usd).toFixed(6)} | Actual so far: $${Number(data.actual_cost_usd).toFixed(6)}`;

    if (data.status === "done" && data.download_ready) {
      els.downloadBtn.href = `/api/download/${state.jobId}`;
      if (data.translated_filename) {
        els.downloadBtn.download = data.translated_filename;
      }
      els.downloadBtn.classList.remove("hidden");
      setStatus("Done. You can download the translated PDF.");
      loadSavedDownloads().catch((err) => {
        setStatus(`Done, but could not refresh saved files: ${err.message}`);
      });
      return;
    }

    if (data.status === "failed") {
      setStatus(`Failed: ${data.message}`);
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
els.modelSelect.addEventListener("change", invalidateEstimateIfInputsChanged);
els.pageFrom.addEventListener("input", invalidateEstimateIfInputsChanged);
els.pageTo.addEventListener("input", invalidateEstimateIfInputsChanged);

loadModels().catch((err) => {
  setStatus(`Could not load models: ${err.message}`);
});

loadSavedDownloads().catch((err) => {
  setStatus(`Could not load saved downloads: ${err.message}`);
});
