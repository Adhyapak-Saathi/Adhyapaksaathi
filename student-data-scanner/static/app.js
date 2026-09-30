let queuedFiles = [];
let queueSeq = 0;
let processedDocuments = 0;
let cumulativeUncertain = new Set();
let documentTypes = new Set();
let draftConflicts = [];
let targetRow = null;
let forceNew = false;
let previewUrl = null;
let processing = false;
let matchBlocked = false;
let originalEnrollment = null;

const $ = id => document.getElementById(id);

function setStatus(id, msg, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg || "";
  el.className = "status" + (cls ? " " + cls : "");
}

function showNotice(message, type) {
  const el = $("saveNotice");
  if (!el) return;
  el.textContent = message;
  el.className = "notice show " + (type || "success");
  clearTimeout(showNotice._timer);
  showNotice._timer = setTimeout(() => {
    el.className = "notice";
  }, 5000);
}

function enrollmentFromData(data) {
  return {
    academic_year: String((data && data.academic_year) || ""),
    standard: String((data && data.standard) || ""),
    division: String((data && data.division) || ""),
    roll_number: String((data && data.roll_number) || "")
  };
}

function enrollmentChanged(data) {
  if (!originalEnrollment) return false;
  const now = enrollmentFromData(data);
  return ["academic_year","standard","division","roll_number"].some(
    k => String(now[k] || "") !== String(originalEnrollment[k] || "")
  );
}

function fillSelect(id, values, firstLabel) {
  const el = $(id);
  if (!el) return;
  const current = el.value;
  el.innerHTML = "";
  const first = document.createElement("option");
  first.value = "";
  first.textContent = firstLabel;
  el.appendChild(first);
  (values || []).forEach(v => {
    const opt = document.createElement("option");
    opt.value = String(v);
    opt.textContent = String(v);
    el.appendChild(opt);
  });
  if ([...el.options].some(o => o.value === current)) el.value = current;
}

async function loadFilterOptions() {
  try {
    const x = await apiFetch("/api/filter-options", {}, 15000);
    fillSelect("searchYear", x.academic_years || [], "બધા Academic Year");
    fillSelect("searchStandard", x.standards || [], "બધા ધોરણ");
    fillSelect("searchDivision", x.divisions || [], "બધા વર્ગ");
    fillSelect("downloadYear", x.academic_years || [], "બધા Academic Year");
    fillSelect("downloadStandard", x.standards || [], "બધા ધોરણ");
    fillSelect("downloadDivision", x.divisions || [], "બધા વર્ગ");
  } catch (_) {}
}

function collect() {
  const out = {};
  FIELD_KEYS.forEach(k => {
    const el = $("f_" + k);
    out[k] = el ? el.value.trim() : "";
  });
  return out;
}

function clearFields() {
  FIELD_KEYS.forEach(k => {
    const el = $("f_" + k);
    const wrap = $("wrap_" + k);
    if (el) el.value = "";
    if (wrap) wrap.classList.remove("scanned", "conflict");
  });
}

function compact(v) {
  return String(v || "").toLowerCase().replace(/[^0-9a-z\u0A80-\u0AFF\u0900-\u097F]+/g, "");
}

function labelFor(key) {
  return FIELD_LABELS[key] || key;
}

function applyIncoming(data) {
  FIELD_KEYS.forEach(k => {
    const incoming = String((data && data[k]) || "").trim();
    if (!incoming) return;

    const el = $("f_" + k);
    const wrap = $("wrap_" + k);
    const current = el.value.trim();

    if (!current) {
      el.value = incoming;
      if (wrap) wrap.classList.add("scanned");
      cumulativeUncertain.delete(k);
      return;
    }

    if (compact(current) === compact(incoming)) {
      if (wrap) wrap.classList.add("scanned");
      cumulativeUncertain.delete(k);
      return;
    }

    const exists = draftConflicts.some(c =>
      c.field === k && compact(c.existing) === compact(current) && compact(c.newValue) === compact(incoming)
    );
    if (!exists) {
      draftConflicts.push({ field: k, existing: current, newValue: incoming });
    }
    cumulativeUncertain.add(k);
    if (wrap) wrap.classList.add("conflict");
  });

  refreshSummary();
  renderReview();
}

function refreshSummary() {
  const data = collect();
  const fieldCount = FIELD_KEYS.filter(k => data[k]).length;
  const reviewKeys = new Set([
    ...Array.from(cumulativeUncertain),
    ...draftConflicts.map(c => c.field)
  ]);
  $("docCount").textContent = String(processedDocuments);
  $("fieldCount").textContent = String(fieldCount);
  $("reviewCount").textContent = String(reviewKeys.size);
  $("saveBtn").disabled = fieldCount === 0 || processing || matchBlocked;
}

function renderReview() {
  const lines = [];
  if (cumulativeUncertain.size) {
    lines.push("ચકાસવા જેવા fields: " + Array.from(cumulativeUncertain).map(labelFor).join(", "));
  }
  if (draftConflicts.length) {
    lines.push("દસ્તાવેજો વચ્ચે ફરક:");
    draftConflicts.forEach(c => {
      lines.push("• " + labelFor(c.field) + ': "' + c.existing + '" ↔ "' + c.newValue + '"');
    });
    lines.push("જે value સાચી હોય તે ઉપરના fieldમાં manually રાખો.");
  }
  $("verify").textContent = lines.join("\n");
}

function fillExistingRecord(data) {
  clearFields();
  FIELD_KEYS.forEach(k => {
    const el = $("f_" + k);
    if (el) el.value = String((data && data[k]) || "");
  });
  cumulativeUncertain = new Set();
  draftConflicts = [];
  documentTypes = new Set();
  processedDocuments = 0;
  renderReview();
  refreshSummary();
}

async function loadStudentFromSearch(candidate) {
  const hasDraft = FIELD_KEYS.some(k => collect()[k]);
  if (hasDraft) {
    const ok = confirm("હાલની માહિતી બદલીને આ વિદ્યાર્થી ખોલવો છે?");
    if (!ok) return;
  }

  try {
    const record = await apiFetch("/api/student/" + encodeURIComponent(candidate.row), {}, 20000);
    clearQueue();
    fillExistingRecord(record.data || {});
    originalEnrollment = enrollmentFromData(record.data || {});
    targetRow = record.row;
    forceNew = false;
    matchBlocked = false;
    $("matchBox").innerHTML = '<div class="match okmatch"><b>વિદ્યાર્થી માહિતી લોડ થઈ.</b></div>';
    $("searchMenu").open = false;
    $("sheetMenu").open = false;
    $("fillMenu").open = true;
    $("fillMenu").scrollIntoView({ behavior: "smooth", block: "start" });
    refreshSummary();
  } catch (e) {
    showNotice("વિદ્યાર્થી માહિતી લોડ થઈ નથી: " + e.message, "error");
  }
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
  matchBlocked = false;
  renderMatch({ status: "matched", row: c.row, candidate: c, score: c.score || 0 });
  refreshSummary();
}

function chooseNewRow() {
  targetRow = null;
  forceNew = true;
  matchBlocked = false;
  $("matchBox").innerHTML = '<div class="match new">નવી વિદ્યાર્થી row બનાવાશે.</div>';
  refreshSummary();
}

function renderMatch(match) {
  const box = $("matchBox");
  box.innerHTML = "";

  if (match && match.status === "unavailable") {
    targetRow = null;
    forceNew = false;
    matchBlocked = false;
    box.innerHTML = '<div class="match warnmatch">વિદ્યાર્થી match હમણાં ઉપલબ્ધ નથી.</div>';
    refreshSummary();
    return;
  }

  if (!match || match.status === "none") {
    targetRow = null;
    forceNew = false;
    matchBlocked = false;
    box.innerHTML = '<div class="match new">વિદ્યાર્થી match મળ્યો નથી.</div>';
    refreshSummary();
    return;
  }

  if (match.status === "matched") {
    targetRow = match.row;
    forceNew = false;
    matchBlocked = false;
    const c = match.candidate || {};
    const div = document.createElement("div");
    div.className = "match okmatch";
    div.innerHTML = "<b>વિદ્યાર્થી મળ્યો</b><br>" + escapeHtml(candidateText(c));
    box.appendChild(div);
    refreshSummary();
    return;
  }

  if (match.status === "ambiguous") {
    targetRow = null;
    forceNew = false;
    matchBlocked = true;
    const head = document.createElement("div");
    head.className = "match warnmatch";
    head.innerHTML = "<b>એકથી વધુ વિદ્યાર્થી મળ્યા.</b><br>સાચો વિદ્યાર્થી પસંદ કરો.";
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
      throw new Error(
        response.status >= 500
          ? "Server responseમાં સમસ્યા આવી. આ દસ્તાવેજ save થયો નથી; ફરી પ્રયાસ કરો."
          : "Serverએ માન્ય response આપ્યો નથી."
      );
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
    const response = await fetch(url, Object.assign({}, options || {}, { signal: controller.signal }));
    return await parseApiResponse(response);
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error("Request બહુ સમય લઈ રહી છે. ફરી પ્રયાસ કરો.");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function fileKey(file) {
  return [file.name, file.size, file.lastModified, file.type].join("|");
}

function isSupported(file) {
  const type = String(file.type || "").toLowerCase();
  const name = String(file.name || "").toLowerCase();
  return type.startsWith("image/") || type === "application/pdf" || name.endsWith(".pdf");
}

function addFiles(fileList) {
  const existing = new Set(queuedFiles.map(x => fileKey(x.file)));
  let added = 0;
  let rejected = 0;

  Array.from(fileList || []).forEach(file => {
    if (queuedFiles.length >= 12) {
      rejected++;
      return;
    }
    if (!isSupported(file) || file.size > 15 * 1024 * 1024) {
      rejected++;
      return;
    }
    const key = fileKey(file);
    if (existing.has(key)) return;
    queuedFiles.push({ id: ++queueSeq, file, state: "pending", error: "" });
    existing.add(key);
    added++;
  });

  renderQueue();
  if (queuedFiles.length) {
    previewFile(queuedFiles[queuedFiles.length - 1].file);
    $("extractBtn").disabled = false;
  }

  let msg = added ? added + " document ઉમેરાયા." : "નવો document ઉમેરાયો નથી.";
  if (rejected) msg += " " + rejected + " unsupported/મોટા file છોડ્યા.";
  setStatus("scanStatus", msg, rejected ? "warn" : "ok");
}

function renderQueue() {
  const panel = $("queuePanel");
  const list = $("fileQueue");
  panel.hidden = queuedFiles.length === 0;
  list.innerHTML = "";

  queuedFiles.forEach(item => {
    const row = document.createElement("div");
    row.className = "file-item";
    const icon = item.file.type === "application/pdf" || item.file.name.toLowerCase().endsWith(".pdf") ? "📄" : "🖼️";
    const stateText = item.state === "done" ? "વાંચ્યું" : item.state === "reading" ? "વાંચી રહ્યું છે" : item.state === "error" ? "ફરી પ્રયાસ" : "તૈયાર";
    row.innerHTML =
      '<div class="file-meta"><span>' + icon + '</span><span class="file-name">' +
      escapeHtml(item.file.name || ("Document " + item.id)) +
      '</span></div><span class="file-state ' + (item.state === "done" ? "done" : item.state === "error" ? "error" : "") + '">' +
      escapeHtml(stateText) + "</span>";
    row.onclick = () => previewFile(item.file);
    list.appendChild(row);
  });

  $("extractBtn").disabled = processing || !queuedFiles.some(x => x.state !== "done");
  $("imageInfo").textContent = queuedFiles.length
    ? queuedFiles.length + " દસ્તાવેજ પસંદ"
    : "";
}

function clearQueue() {
  queuedFiles = [];
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  $("imagePreview").src = "";
  $("pdfPreview").src = "";
  $("imagePreview").style.display = "none";
  $("pdfPreview").style.display = "none";
  $("previewShell").hidden = true;
  renderQueue();
}

function previewFile(file) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);

  const isPdf = String(file.type || "").toLowerCase() === "application/pdf" || String(file.name || "").toLowerCase().endsWith(".pdf");
  $("previewShell").hidden = false;
  if (isPdf) {
    $("imagePreview").style.display = "none";
    $("pdfPreview").style.display = "block";
    $("pdfPreview").src = previewUrl + "#toolbar=0";
  } else {
    $("pdfPreview").style.display = "none";
    $("imagePreview").style.display = "block";
    $("imagePreview").src = previewUrl;
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
      reject(new Error("Photo વાંચી શકાયો નથી."));
    };
    img.src = url;
  });
}

async function prepareUpload(file) {
  const isPdf = String(file.type || "").toLowerCase() === "application/pdf" || String(file.name || "").toLowerCase().endsWith(".pdf");
  if (isPdf) {
    return { blob: file, name: file.name || "document.pdf", type: "application/pdf" };
  }

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

  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob(
      b => b ? resolve(b) : reject(new Error("Photo optimize થઈ શક્યો નથી.")),
      "image/jpeg",
      0.88
    );
  });
  return { blob, name: "scan.jpg", type: "image/jpeg" };
}

async function rematchDraft() {
  const data = collect();
  if (!FIELD_KEYS.some(k => data[k])) return;
  try {
    const match = await apiFetch("/api/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data })
    }, 20000);
    renderMatch(match);
  } catch (_) {
    renderMatch({ status: "unavailable", candidates: [] });
  }
}

async function processDocuments() {
  if (processing) return;
  const items = queuedFiles.filter(x => x.state !== "done");
  if (!items.length) return;

  processing = true;
  matchBlocked = false;
  $("extractBtn").disabled = true;
  $("progressWrap").hidden = false;
  setStatus("scanStatus", "માહિતી વાંચી રહી છે...", "busy");

  let success = 0;
  let failed = 0;

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    item.state = "reading";
    item.error = "";
    renderQueue();

    $("progressBar").style.width = Math.round((i / items.length) * 100) + "%";
    $("progressText").textContent = (i + 1) + " / " + items.length + " • " + (item.file.name || "Document");

    try {
      const prepared = await prepareUpload(item.file);
      const fd = new FormData();
      fd.append("image", prepared.blob, prepared.name);
      fd.append("doc_type", $("docType").value);

      const x = await apiFetch("/api/extract", { method: "POST", body: fd }, 60000);

      applyIncoming(x.data || {});
      (x.uncertain_fields || []).forEach(k => {
        if (!(x.data && x.data[k])) cumulativeUncertain.add(k);
      });
      if (x.document_type) documentTypes.add(x.document_type);
      processedDocuments++;
      item.state = "done";
      success++;
    } catch (e) {
      item.state = "error";
      item.error = e.message;
      failed++;
    }

    refreshSummary();
    renderReview();
    renderQueue();
    $("progressBar").style.width = Math.round(((i + 1) / items.length) * 100) + "%";
  }

  await rematchDraft();

  const message =
    success + " દસ્તાવેજ વાંચાયા." +
    (failed ? "\n" + failed + " દસ્તાવેજ ફરી પ્રયાસ માટે બાકી છે." : "");
  setStatus("scanStatus", message, failed ? "warn" : "ok");

  $("progressText").textContent = "પૂર્ણ";
  processing = false;
  $("extractBtn").disabled = !queuedFiles.some(x => x.state !== "done");
  refreshSummary();
}

$("camera").addEventListener("change", e => {
  addFiles(e.target.files);
  e.target.value = "";
});
$("gallery").addEventListener("change", e => {
  addFiles(e.target.files);
  e.target.value = "";
});

FIELD_KEYS.forEach(k => {
  const el = $("f_" + k);
  if (!el) return;
  el.addEventListener("input", () => {
    draftConflicts = draftConflicts.filter(c => c.field !== k);
    cumulativeUncertain.delete(k);
    const wrap = $("wrap_" + k);
    if (wrap) wrap.classList.remove("conflict");
    renderReview();
    refreshSummary();
  });
});

async function savePayload(allowEnrollmentChange) {
  const data = collect();
  const x = await apiFetch("/api/upsert", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      data,
      uncertain_fields: Array.from(cumulativeUncertain),
      document_types: Array.from(documentTypes),
      target_row: targetRow,
      force_new: forceNew,
      allow_enrollment_change: !!allowEnrollmentChange
    })
  }, 35000);
  return x;
}

async function saveScan() {
  const data = collect();
  if (!FIELD_KEYS.some(k => data[k])) {
    setStatus("scanStatus", "Save કરવા માટે માહિતી નથી.", "err");
    return;
  }

  $("saveBtn").disabled = true;
  setStatus("scanStatus", "માહિતી save થઈ રહી છે...", "busy");

  try {
    let allowEnrollmentChange = false;

    if (targetRow && enrollmentChanged(data)) {
      allowEnrollmentChange = confirm(
        "Academic Year / Standard / Division / Roll Number બદલાયું છે.\n\nઆને Promotion / Class Change તરીકે Save કરવું છે?"
      );
      if (!allowEnrollmentChange) {
        setStatus("scanStatus", "Promotion/Class Change cancel કર્યું.", "warn");
        refreshSummary();
        return;
      }
    }

    let x;
    try {
      x = await savePayload(allowEnrollmentChange);
    } catch (e) {
      if (e.data && e.data.code === "PROMOTION_CONFIRM_REQUIRED") {
        const ok = confirm(
          "આ વિદ્યાર્થીનું ધોરણ/વર્ગ/Academic Year બદલાઈ રહ્યું છે.\nજૂની enrollment history સાચવીને આગળ વધવું છે?"
        );
        if (!ok) throw e;
        x = await savePayload(true);
      } else {
        throw e;
      }
    }

    let msg = x.action === "created"
      ? "નવી વિદ્યાર્થી માહિતી save થઈ."
      : "વિદ્યાર્થી માહિતી update થઈ.";

    if (x.enrollment_changed) {
      msg += x.history_saved === false
        ? "\nPromotion/Class Change save થયું, પરંતુ history નોંધ ચકાસવી જરૂરી છે."
        : "\nPromotion/Class Change historyમાં નોંધાયું.";
    }
    if (x.status === "VERIFY") msg += "\nકેટલીક માહિતી ચકાસવી જરૂરી છે.";
    if (x.conflicts && x.conflicts.length) {
      msg += "\n" + x.conflicts.length + " માહિતીમાં ફરક મળ્યો.";
    }

    targetRow = x.row;
    forceNew = false;

    if (x.sheet_verified === true) {
      if (x.saved_data) {
        fillExistingRecord(x.saved_data);
        originalEnrollment = enrollmentFromData(x.saved_data);
      }
      showNotice("Google Sheetમાં માહિતી સફળતાપૂર્વક Save થઈ.", "success");
      setStatus("scanStatus", msg, (x.conflicts && x.conflicts.length) ? "warn" : "ok");
      loadFilterOptions();
    } else {
      showNotice("Google Sheetમાં Saveની પુષ્ટિ થઈ નથી.", "error");
      setStatus("scanStatus", "Saveની પુષ્ટિ થઈ નથી.", "err");
    }
  } catch (e) {
    if (e.data && e.data.code === "AMBIGUOUS") {
      renderMatch({ status: "ambiguous", candidates: e.data.candidates || [] });
    }
    setStatus("scanStatus", e.message, "err");
    showNotice("માહિતી Google Sheetમાં Save થઈ નથી.", "error");
  } finally {
    refreshSummary();
  }
}

function startNewStudent() {
  if (FIELD_KEYS.some(k => collect()[k])) {
    const ok = confirm("હાલની સંકલિત માહિતી સાફ કરીને નવો વિદ્યાર્થી શરૂ કરવો છે?");
    if (!ok) return;
  }

  queuedFiles = [];
  processedDocuments = 0;
  cumulativeUncertain = new Set();
  documentTypes = new Set();
  draftConflicts = [];
  targetRow = null;
  forceNew = false;
  processing = false;
  matchBlocked = false;
  originalEnrollment = null;

  clearQueue();
  clearFields();
  $("matchBox").innerHTML = "";
  $("verify").textContent = "";
  $("progressWrap").hidden = true;
  $("progressBar").style.width = "0";
  $("progressText").textContent = "";
  setStatus("scanStatus", "", "");
  refreshSummary();
}

async function searchStudent() {
  const q = $("q").value.trim();
  const year = $("searchYear").value;
  const standard = $("searchStandard").value;
  const division = $("searchDivision").value;
  if (!q && !year && !standard && !division) return;

  $("results").innerHTML = '<p class="muted">શોધી રહ્યું છે...</p>';

  try {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (year) params.set("academic_year", year);
    if (standard) params.set("standard", standard);
    if (division) params.set("division", division);
    const items = await apiFetch("/api/search?" + params.toString(), {}, 20000);
    $("results").innerHTML = "";
    if (!items.length) {
      $("results").innerHTML = '<p class="muted">વિદ્યાર્થી મળ્યો નથી.</p>';
      return;
    }
    items.forEach(c => {
      const d = document.createElement("button");
      d.type = "button";
      d.className = "result result-btn";
      d.innerHTML = "<b>" + escapeHtml(c.student_full_name || c.aadhaar_according_name || "વિદ્યાર્થી") + "</b>" +
        "<small>" + escapeHtml(
          (c.academic_year || "-") + " • Std " + (c.standard || "-") + "-" + (c.division || "-") +
          " • GR " + (c.gr_number || "-") +
          " • PEN " + (c.pen_number || "-") +
          " • DOB " + (c.dob || "-")
        ) + "</small>";
      d.onclick = () => loadStudentFromSearch(c);
      $("results").appendChild(d);
    });
  } catch (e) {
    $("results").innerHTML = '<p class="errorText">' + escapeHtml(e.message) + "</p>";
  }
}

function downloadData() {
  const params = new URLSearchParams();
  const year = $("downloadYear").value;
  const standard = $("downloadStandard").value;
  const division = $("downloadDivision").value;
  if (year) params.set("academic_year", year);
  if (standard) params.set("standard", standard);
  if (division) params.set("division", division);
  const suffix = params.toString() ? "?" + params.toString() : "";
  window.location.href = "/api/export.xlsx" + suffix;
}

async function importMaster() {
  const file = $("masterFile").files && $("masterFile").files[0];
  if (!file) return;

  setStatus("importStatus", "File import થઈ રહી છે...", "busy");
  const fd = new FormData();
  fd.append("file", file);

  try {
    const x = await apiFetch("/api/import", { method: "POST", body: fd }, 90000);
    const msg =
      "Import પૂર્ણ.\nનવી entries: " + x.inserted +
      "\nUpdate: " + x.updated +
      "\nReview: " + (x.skipped_ambiguous + x.conflicts);
    setStatus("importStatus", msg, (x.skipped_ambiguous || x.conflicts) ? "warn" : "ok");
    loadFilterOptions();
  } catch (e) {
    setStatus("importStatus", e.message, "err");
  }
}

document.querySelectorAll(".task-panel").forEach(panel => {
  panel.addEventListener("toggle", () => {
    if (!panel.open) return;
    document.querySelectorAll(".task-panel").forEach(other => {
      if (other !== panel) other.open = false;
    });
  });
});

refreshSummary();
loadFilterOptions();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js?v=9").catch(() => {});
}
