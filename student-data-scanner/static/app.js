let currentFile = null;
let preparedBlob = null;
let currentUncertain = [];
let currentDocumentType = "OTHER";
let targetRow = null;
let forceNew = false;

const $ = id => document.getElementById(id);

function setStatus(id, msg, cls) {
  const el = $(id);
  el.textContent = msg || "";
  el.className = "status" + (cls ? " " + cls : "");
}

function clearFields() {
  FIELD_KEYS.forEach(k => {
    const el = $("f_" + k);
    if (el) el.value = "";
    const wrap = $("wrap_" + k);
    if (wrap) wrap.classList.remove("scanned");
  });
}

function collect() {
  const out = {};
  FIELD_KEYS.forEach(k => {
    const el = $("f_" + k);
    out[k] = el ? el.value.trim() : "";
  });
  return out;
}

function fillScanned(data) {
  clearFields();
  FIELD_KEYS.forEach(k => {
    const val = (data && data[k]) || "";
    const el = $("f_" + k);
    if (el) el.value = val;
    if (val) {
      const wrap = $("wrap_" + k);
      if (wrap) wrap.classList.add("scanned");
    }
  });
}

function candidateText(c) {
  const name = c.student_full_name || c.aadhaar_according_name || "(નામ નથી)";
  return name +
    " | GR " + (c.gr_number || "-") +
    " | PEN " + (c.pen_number || "-") +
    " | CTS " + (c.cts_number || "-") +
    " | DOB " + (c.dob || "-");
}

function chooseCandidate(c) {
  targetRow = c.row;
  forceNew = false;
  $("selected").textContent = "પસંદ કરેલ વિદ્યાર્થી: " + candidateText(c);
  renderMatch({
    status: "matched",
    row: c.row,
    candidate: c,
    score: c.score || 0
  });
  $("saveBtn").disabled = !hasScannedData();
}

function chooseNewRow() {
  targetRow = null;
  forceNew = true;
  $("selected").textContent = "નવી row તરીકે save કરવાનું પસંદ કર્યું.";
  const box = $("matchBox");
  box.innerHTML = '<div class="match new">નવો વિદ્યાર્થી તરીકે નવી row બનાવાશે.</div>';
  $("saveBtn").disabled = !hasScannedData();
}

function renderMatch(match) {
  const box = $("matchBox");
  box.innerHTML = "";
  targetRow = null;
  forceNew = false;

  if (!match || match.status === "none") {
    box.innerHTML = '<div class="match new"><b>Auto-match:</b> existing studentનો મજબૂત match મળ્યો નથી. Save કરશો તો નવી row બનશે.</div>';
    forceNew = true;
    $("saveBtn").disabled = !hasScannedData();
    return;
  }

  if (match.status === "matched") {
    targetRow = match.row;
    const c = match.candidate || {};
    const div = document.createElement("div");
    div.className = "match okmatch";
    div.innerHTML = "<b>Auto-match મળ્યો:</b><br>" + escapeHtml(candidateText(c));
    box.appendChild(div);
    $("saveBtn").disabled = !hasScannedData();
    return;
  }

  if (match.status === "ambiguous") {
    const head = document.createElement("div");
    head.className = "match warnmatch";
    head.innerHTML = "<b>એકથી વધુ શક્ય વિદ્યાર્થી મળ્યા.</b><br>સાચો વિદ્યાર્થી પસંદ કરો અથવા નવી row બનાવો.";
    box.appendChild(head);

    (match.candidates || []).forEach(c => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "candidateBtn";
      b.textContent = candidateText(c);
      b.onclick = () => chooseCandidate(c);
      box.appendChild(b);
    });

    const n = document.createElement("button");
    n.type = "button";
    n.className = "secondary";
    n.textContent = "આ નવો વિદ્યાર્થી છે — નવી row બનાવો";
    n.onclick = chooseNewRow;
    box.appendChild(n);
    $("saveBtn").disabled = true;
  }
}

function hasScannedData() {
  const data = collect();
  return FIELD_KEYS.some(k => data[k]);
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

async function parseApiResponse(response) {
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (_) {
      const e = new Error(
        response.status >= 500
          ? "Server request timeout થયો. Data save થયો નથી. ફરી પ્રયાસ કરો."
          : "Serverએ માન્ય response આપ્યો નથી."
      );
      e.status = response.status;
      throw e;
    }
  }

  if (!response.ok) {
    const e = new Error(data.error || ("Request failed: HTTP " + response.status));
    e.status = response.status;
    e.data = data;
    throw e;
  }
  return data;
}

async function apiFetch(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs || 65000);
  try {
    const r = await fetch(url, Object.assign({}, options || {}, { signal: controller.signal }));
    return await parseApiResponse(r);
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error("Request બહુ સમય લઈ રહી છે. Data save થયો નથી. ફરી Scan દબાવો.");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("Image વાંચી શકાયું નથી."));
    };
    img.src = url;
  });
}

async function prepareImage(file) {
  const img = await loadImage(file);
  const maxSide = 1800;
  const scale = Math.min(1, maxSide / Math.max(img.naturalWidth || img.width, img.naturalHeight || img.height));
  const w = Math.max(1, Math.round((img.naturalWidth || img.width) * scale));
  const h = Math.max(1, Math.round((img.naturalHeight || img.height) * scale));
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d", { alpha: false });
  ctx.drawImage(img, 0, 0, w, h);
  return await new Promise((resolve, reject) => {
    canvas.toBlob(
      blob => blob ? resolve(blob) : reject(new Error("Image compress થઈ શક્યું નથી.")),
      "image/jpeg",
      0.88
    );
  });
}

function resetMatch() {
  targetRow = null;
  forceNew = false;
  $("matchBox").innerHTML = "";
  $("selected").textContent = "";
}

function useImageFile(file) {
  if (!file) return;
  if (!String(file.type || "").startsWith("image/")) {
    setStatus("scanStatus", "ફક્ત photo/image પસંદ કરો.", "err");
    return;
  }

  currentFile = file;
  preparedBlob = null;
  currentUncertain = [];
  currentDocumentType = "OTHER";
  clearFields();
  resetMatch();

  $("preview").src = URL.createObjectURL(file);
  $("preview").style.display = "block";
  $("extractBtn").disabled = false;
  $("saveBtn").disabled = true;
  $("verify").textContent = "";
  $("imageInfo").textContent = "Original: " + Math.round(file.size / 1024) + " KB";
  setStatus("scanStatus", "ફોટો તૈયાર છે. “AI થી ડેટા વાંચો” દબાવો.", "ok");
}

$("camera").addEventListener("change", e => useImageFile(e.target.files && e.target.files[0]));
$("gallery").addEventListener("change", e => useImageFile(e.target.files && e.target.files[0]));

async function extractImage() {
  if (!currentFile) return;

  setStatus("scanStatus", "ફોટો optimize કરીને AI document વાંચી રહ્યું છે...", "busy");
  $("extractBtn").disabled = true;
  $("saveBtn").disabled = true;
  resetMatch();
  clearFields();

  try {
    if (!preparedBlob) preparedBlob = await prepareImage(currentFile);
    $("imageInfo").textContent =
      "Original: " + Math.round(currentFile.size / 1024) +
      " KB • Scan upload: " + Math.round(preparedBlob.size / 1024) + " KB";

    const fd = new FormData();
    fd.append("image", preparedBlob, "scan.jpg");
    fd.append("doc_type", $("docType").value);

    const x = await apiFetch("/api/extract", { method: "POST", body: fd }, 65000);
    fillScanned(x.data || {});
    currentUncertain = x.uncertain_fields || [];
    currentDocumentType = x.document_type || $("docType").value || "OTHER";

    const count = FIELD_KEYS.filter(k => x.data && x.data[k]).length;
    const verifyText = currentUncertain.length
      ? "VERIFY જરૂરી: " + currentUncertain.join(", ")
      : "AIએ uncertain field નોંધ્યું નથી.";
    $("verify").textContent = verifyText;

    renderMatch(x.match || { status: "none" });

    setStatus(
      "scanStatus",
      "Document: " + currentDocumentType +
      "\nમળેલા fields: " + count +
      "\nModel: " + (x.model_used || "-") +
      " • " + (x.latency_ms ? (x.latency_ms / 1000).toFixed(1) + " sec" : ""),
      currentUncertain.length ? "warn" : "ok"
    );
  } catch (e) {
    const extra = e.data && e.data.retryable
      ? "\nઆ temporary AI/provider સમસ્યા છે; થોડા સેકન્ડ પછી ફરી દબાવો."
      : "";
    setStatus("scanStatus", e.message + extra, "err");
    $("saveBtn").disabled = true;
  } finally {
    $("extractBtn").disabled = false;
  }
}

async function saveScan() {
  if (!hasScannedData()) {
    setStatus("scanStatus", "Save કરવા માટે scan data નથી.", "err");
    return;
  }

  $("saveBtn").disabled = true;
  setStatus("scanStatus", "Google Sheetમાં save થઈ રહ્યું છે...", "busy");

  try {
    const x = await apiFetch("/api/upsert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: collect(),
        uncertain_fields: currentUncertain,
        document_type: currentDocumentType,
        target_row: targetRow,
        force_new: forceNew
      })
    }, 30000);

    let msg = x.action === "created"
      ? "નવી student row Google Sheetમાં બનાવી."
      : "Existing studentની row update કરી.";
    msg += "\nSheet row: " + x.row + " • Status: " + x.status;

    if (x.conflicts && x.conflicts.length) {
      msg += "\n" + x.conflicts.length + " conflict મળ્યા; existing value overwrite નથી કરી.";
    }
    setStatus("scanStatus", msg, x.conflicts && x.conflicts.length ? "warn" : "ok");
    targetRow = x.row;
    forceNew = false;
    $("saveBtn").disabled = false;
  } catch (e) {
    if (e.data && e.data.code === "AMBIGUOUS") {
      renderMatch({ status: "ambiguous", candidates: e.data.candidates || [] });
    }
    setStatus("scanStatus", e.message, "err");
    if (!(e.data && e.data.code === "AMBIGUOUS")) {
      $("saveBtn").disabled = false;
    }
  }
}

async function searchStudent() {
  const q = $("q").value.trim();
  if (!q) return;
  $("results").innerHTML = '<p class="muted">શોધી રહ્યું છે...</p>';

  try {
    const items = await apiFetch("/api/search?q=" + encodeURIComponent(q), {}, 20000);
    $("results").innerHTML = "";
    if (!items.length) {
      $("results").innerHTML = '<p class="muted">કોઈ match મળ્યો નથી.</p>';
      return;
    }
    items.forEach(c => {
      const d = document.createElement("div");
      d.className = "result";
      d.textContent = candidateText(c);
      d.onclick = () => chooseCandidate(c);
      $("results").appendChild(d);
    });
  } catch (e) {
    $("results").innerHTML = '<p class="errorText">' + escapeHtml(e.message) + "</p>";
  }
}

function clearScan() {
  currentFile = null;
  preparedBlob = null;
  currentUncertain = [];
  currentDocumentType = "OTHER";
  targetRow = null;
  forceNew = false;

  $("camera").value = "";
  $("gallery").value = "";
  $("preview").src = "";
  $("preview").style.display = "none";
  $("imageInfo").textContent = "";
  $("extractBtn").disabled = true;
  $("saveBtn").disabled = true;
  $("verify").textContent = "";
  $("matchBox").innerHTML = "";
  $("selected").textContent = "";
  clearFields();
  setStatus("scanStatus", "", "");
}

async function importMaster() {
  const file = $("masterFile").files && $("masterFile").files[0];
  if (!file) return;

  setStatus("importStatus", "Import Google Sheet masterમાં merge થઈ રહ્યું છે...", "busy");
  const fd = new FormData();
  fd.append("file", file);

  try {
    const x = await apiFetch("/api/import", { method: "POST", body: fd }, 90000);
    let msg =
      "Import પૂર્ણ.\nનવી rows: " + x.inserted +
      "\nUpdate rows: " + x.updated +
      "\nAmbiguous skip: " + x.skipped_ambiguous +
      "\nConflicts: " + x.conflicts;
    setStatus("importStatus", msg, (x.skipped_ambiguous || x.conflicts) ? "warn" : "ok");
  } catch (e) {
    setStatus("importStatus", e.message, "err");
  }
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js?v=4").catch(() => {});
}
