import os, json, re, base64, time, threading, zlib, struct, binascii
from datetime import datetime
from io import BytesIO
import pandas as pd
import requests
from flask import Flask, render_template, request, jsonify, send_file

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

FIELDS = [
    ("roll_number", "Roll Number"),
    ("gr_number", "GR Number"),
    ("cts_number", "CTS Number"),
    ("abha_number", "ABHA Number"),
    ("pen_number", "PEN Number"),
    ("apaar_number", "APAAR Number"),
    ("aadhaar_number", "Aadhaar Number"),
    ("aadhaar_according_name", "Aadhaar મુજબ નામ"),
    ("student_full_name", "વિદ્યાર્થીનું પૂરું નામ"),
    ("mother_name", "માતાનું નામ"),
    ("father_name", "પિતાનું નામ"),
    ("phone_number", "Phone Number"),
    ("address", "સરનામું"),
    ("admission_date", "Admission Date"),
    ("dob", "DOB"),
    ("sub_caste", "પેટા જાતિ"),
    ("bank_account_name", "Bank Account Name"),
    ("bank_account_number", "Bank Account Number"),
    ("bank_branch", "Bank Branch"),
    ("ifsc_code", "IFSC Code"),
    ("bank_name", "Bank Name"),
]
META_HEADERS = ["Verification Status", "Remarks", "Last Updated"]
HEADERS = [label for _, label in FIELDS] + META_HEADERS
KEY_TO_LABEL = dict(FIELDS)
LABEL_TO_KEY = {label: key for key, label in FIELDS}
ALL_KEYS = [key for key, _ in FIELDS]

DOC_ALLOWED = {
    "ABHA_CARD": {"abha_number", "student_full_name", "dob", "phone_number"},
    "AADHAAR_CARD": {"aadhaar_number", "aadhaar_according_name", "dob", "address"},
    "BANK_DOCUMENT": {"bank_account_name", "bank_account_number", "bank_branch", "ifsc_code", "bank_name"},
    "SCHOOL_RECORD": {
        "roll_number", "gr_number", "cts_number", "abha_number", "pen_number", "apaar_number",
        "aadhaar_number", "aadhaar_according_name", "student_full_name", "mother_name", "father_name",
        "phone_number", "address", "admission_date", "dob", "sub_caste"
    },
    "FORM": set(ALL_KEYS),
    "OTHER": set(ALL_KEYS),
}
DOC_TYPES = set(DOC_ALLOWED) | {"AUTO"}

class ProviderBusy(Exception):
    pass

class ProviderError(Exception):
    pass

class SheetConfigError(Exception):
    pass

def norm(v):
    return re.sub(r"\s+", " ", str(v or "").strip()).lower()

def compact(v):
    return re.sub(r"[^0-9a-zA-Z\u0A80-\u0AFF\u0900-\u097F]+", "", str(v or "").lower())

def only_digits(v):
    return re.sub(r"\D", "", str(v or ""))

def name_key(v):
    text = norm(v)
    tokens = re.findall(r"[\w\u0A80-\u0AFF\u0900-\u097F]+", text, flags=re.UNICODE)
    return " ".join(sorted(t for t in tokens if t))

def date_key(v):
    return only_digits(v)

def sanitize(data, allowed=None):
    out = {k: str((data or {}).get(k, "") or "").strip() for k in ALL_KEYS}
    if allowed is not None:
        for k in ALL_KEYS:
            if k not in allowed:
                out[k] = ""

    aadhaar = only_digits(out["aadhaar_number"])
    out["aadhaar_number"] = aadhaar if (not aadhaar or len(aadhaar) == 12) else ""

    abha = only_digits(out["abha_number"])
    if abha and len(abha) == 14:
        out["abha_number"] = f"{abha[:2]}-{abha[2:6]}-{abha[6:10]}-{abha[10:]}"
    elif abha:
        out["abha_number"] = ""

    phone = only_digits(out["phone_number"])
    out["phone_number"] = phone if (not phone or 10 <= len(phone) <= 13) else ""

    ifsc = out["ifsc_code"].replace(" ", "").upper()
    out["ifsc_code"] = ifsc if (not ifsc or re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc)) else ""

    for dk in ("dob", "admission_date"):
        val = out[dk]
        if val and not re.fullmatch(r"\d{2}-\d{2}-\d{4}", val):
            out[dk] = ""
    return out

def sheet_client():
    if gspread is None or Credentials is None:
        raise SheetConfigError("Google Sheets library ઉપલબ્ધ નથી.")
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sid = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not raw or not sid:
        raise SheetConfigError("Google Sheet configuration અધૂરી છે.")
    try:
        info = json.loads(raw)
    except Exception:
        try:
            info = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception as e:
            raise SheetConfigError("Service account JSON વાંચી શકાયું નથી.") from e
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds).open_by_key(sid)

def get_ws():
    ss = sheet_client()
    tab = os.getenv("GOOGLE_SHEET_TAB", "Student_Master").strip() or "Student_Master"
    try:
        return ss.worksheet(tab)
    except Exception as e:
        raise SheetConfigError(f'Google Sheet tab "{tab}" મળ્યો નથી.') from e

def ensure_sheet_schema(ws):
    current = ws.row_values(1)
    if not current:
        ws.update(values=[HEADERS], range_name="A1", value_input_option="USER_ENTERED")
        return
    if current[:len(HEADERS)] != HEADERS:
        raise SheetConfigError(
            "Google Sheet columns app schema સાથે match થતા નથી. Sheet auto-clear કરવામાં આવશે નહીં."
        )

def read_records():
    ws = get_ws()
    ensure_sheet_schema(ws)
    values = ws.get_all_values()
    records = []
    for rownum, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(HEADERS) - len(row))
        rec = {LABEL_TO_KEY[h]: padded[i] for i, h in enumerate(HEADERS[:-3])}
        rec["verification_status"] = padded[-3]
        rec["remarks"] = padded[-2]
        rec["last_updated"] = padded[-1]
        rec["_row"] = rownum
        if any(str(rec.get(k, "")).strip() for k in ALL_KEYS):
            records.append(rec)
    return ws, records

def record_values(rec):
    return [str(rec.get(k, "") or "") for k in ALL_KEYS] + [
        str(rec.get("verification_status", "") or ""),
        str(rec.get("remarks", "") or ""),
        str(rec.get("last_updated", "") or ""),
    ]

def candidate_summary(rec, score=0):
    return {
        "row": rec.get("_row"),
        "score": score,
        "gr_number": rec.get("gr_number", ""),
        "pen_number": rec.get("pen_number", ""),
        "cts_number": rec.get("cts_number", ""),
        "student_full_name": rec.get("student_full_name", ""),
        "aadhaar_according_name": rec.get("aadhaar_according_name", ""),
        "dob": rec.get("dob", ""),
        "father_name": rec.get("father_name", ""),
        "mother_name": rec.get("mother_name", ""),
    }

def score_match(scan, rec):
    exact_ids = [
        "pen_number", "cts_number", "gr_number", "aadhaar_number", "apaar_number", "abha_number"
    ]
    for k in exact_ids:
        a, b = compact(scan.get(k)), compact(rec.get(k))
        if a and b and a == b:
            return 1000

    scan_names = [scan.get("student_full_name", ""), scan.get("aadhaar_according_name", "")]
    rec_names = [rec.get("student_full_name", ""), rec.get("aadhaar_according_name", "")]
    scan_name_keys = {name_key(x) for x in scan_names if name_key(x)}
    rec_name_keys = {name_key(x) for x in rec_names if name_key(x)}
    name_exact = bool(scan_name_keys & rec_name_keys)

    score = 0
    if name_exact:
        score += 130
    if scan.get("dob") and rec.get("dob") and date_key(scan["dob"]) == date_key(rec["dob"]):
        score += 120
    if compact(scan.get("phone_number")) and compact(scan.get("phone_number")) == compact(rec.get("phone_number")):
        score += 90
    if name_key(scan.get("father_name")) and name_key(scan.get("father_name")) == name_key(rec.get("father_name")):
        score += 70
    if name_key(scan.get("mother_name")) and name_key(scan.get("mother_name")) == name_key(rec.get("mother_name")):
        score += 70
    return score

def match_records(scan, records):
    scored = []
    for rec in records:
        s = score_match(scan, rec)
        if s:
            scored.append((s, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return {"status": "none", "candidates": []}

    top_score, top = scored[0]
    if top_score >= 1000:
        same = [x for x in scored if x[0] >= 1000]
        if len(same) == 1:
            return {"status": "matched", "row": top["_row"], "score": top_score, "candidate": candidate_summary(top, top_score)}
        return {"status": "ambiguous", "candidates": [candidate_summary(r, s) for s, r in same[:5]]}

    second = scored[1][0] if len(scored) > 1 else 0
    if top_score >= 220 and (second == 0 or top_score - second >= 60):
        return {"status": "matched", "row": top["_row"], "score": top_score, "candidate": candidate_summary(top, top_score)}
    if top_score >= 120:
        return {"status": "ambiguous", "candidates": [candidate_summary(r, s) for s, r in scored[:5]]}
    return {"status": "none", "candidates": []}

def normalize_doc_type(v):
    v = str(v or "OTHER").strip().upper().replace(" ", "_")
    return v if v in DOC_TYPES else "OTHER"

def filter_by_document(data, doc_type):
    doc = normalize_doc_type(doc_type)
    if doc == "AUTO":
        doc = "OTHER"
    return sanitize(data, DOC_ALLOWED.get(doc, set(ALL_KEYS)))

def extraction_schema(keys):
    data_props = {k: {"type": "string"} for k in keys}
    return {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": ["ABHA_CARD", "AADHAAR_CARD", "BANK_DOCUMENT", "SCHOOL_RECORD", "FORM", "OTHER"],
            },
            "data": {
                "type": "object",
                "properties": data_props,
                "required": list(keys),
                "additionalProperties": False,
            },
            "uncertain_fields": {
                "type": "array",
                "items": {"type": "string", "enum": list(keys)},
            },
        },
        "required": ["document_type", "data", "uncertain_fields"],
        "additionalProperties": False,
    }

def build_prompt(selected_doc, keys):
    labels = ", ".join(f"{k}={KEY_TO_LABEL[k]}" for k in keys)
    return f"""Read ONE photographed student/school document in Gujarati, English, or Hindi.
Selected document type: {selected_doc}

Return only fields that are explicitly visible on THIS document. Never use prior knowledge and never guess.
Allowed fields for this request: {labels}

Critical rules:
- CTS Number in this workflow means SSA/CTS AadhaarUID. It is NOT Aadhaar Card Number.
- Aadhaar Number means only the separate 12-digit Aadhaar card number.
- 'Aadhaar મુજબ નામ' means an exact name explicitly printed on Aadhaar or explicitly labelled as name as per Aadhaar.
- Do not create a separate 'Aadhaar Name' field.
- ABHA Number, PEN and APAAR are separate identifiers.
- On an ABHA card, ABHA Address is NOT a residential postal address; ignore it because this app has no ABHA Address field.
- Never infer father, mother, caste/sub-caste, Aadhaar, bank data, phone, or address from a person's name.
- Bank account holder name belongs only in bank_account_name, not student_full_name unless the document explicitly labels the person as the student.
- Dates must be DD-MM-YYYY only when a complete date is clear.
- If a field is unclear, return an empty string and add that field key to uncertain_fields.
- If selected type is not AUTO, document_type must reflect the real document but do not extract fields outside the allowed list.
"""

def gemini_key():
    return (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GEMINI_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
        or os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
    )

def ai_extract(image_bytes, mime_type, selected_doc):
    key = gemini_key()
    if not key:
        raise ProviderError("Gemini API key સેટ નથી.")

    selected_doc = normalize_doc_type(selected_doc)
    requested_keys = sorted(DOC_ALLOWED[selected_doc] if selected_doc != "AUTO" else set(ALL_KEYS))
    prompt = build_prompt(selected_doc, requested_keys)
    schema = extraction_schema(requested_keys)

    primary = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    fallback = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
    models = []
    for m in (primary, fallback):
        if m and m not in models:
            models.append(m)

    last = ""
    transient = {429, 500, 502, 503, 504}
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
                ]
            }],
            "generationConfig": {
                "thinkingConfig": {"thinkingLevel": "low"},
                "responseFormat": {
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": schema
                    }
                }
            },
        }
        started = time.monotonic()
        try:
            r = requests.post(
                url,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json=payload,
                timeout=(5, 18),
            )
        except requests.Timeout:
            last = f"{model}: timeout"
            continue
        except requests.RequestException as e:
            last = f"{model}: network error {e}"
            continue

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if r.status_code in transient:
            last = f"{model}: HTTP {r.status_code}"
            continue
        if r.status_code >= 300:
            try:
                detail = (r.json().get("error") or {}).get("message", "")
            except Exception:
                detail = r.text[:220]
            detail = re.sub(r"\s+", " ", str(detail or "")).strip()
            raise ProviderError(f"AI request failed ({model}, HTTP {r.status_code}): {detail[:220]}")

        try:
            obj = r.json()
            parts = obj.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            txt = "".join(p.get("text", "") for p in parts).strip()
            result = json.loads(txt)
        except Exception as e:
            raise ProviderError("AIએ માન્ય structured data પાછું આપ્યું નથી.") from e

        detected = normalize_doc_type(result.get("document_type") or selected_doc)
        if selected_doc != "AUTO":
            effective_doc = selected_doc
        else:
            effective_doc = detected if detected != "AUTO" else "OTHER"

        filtered = filter_by_document(result.get("data", {}), effective_doc)
        uncertain = [
            k for k in (result.get("uncertain_fields") or [])
            if k in DOC_ALLOWED.get(effective_doc, set(ALL_KEYS))
        ]
        return {
            "document_type": effective_doc,
            "data": filtered,
            "uncertain_fields": uncertain,
            "model_used": model,
            "latency_ms": elapsed_ms,
        }

    raise ProviderBusy("AI provider હાલમાં વ્યસ્ત છે. Primary અને fallback બંને સમયસર જવાબ આપી શક્યા નથી. ફરી પ્રયાસ કરો.")

def merge_into(existing, incoming, uncertain=None, source_doc=""):
    merged = dict(existing or {})
    conflicts = []
    for k in ALL_KEYS:
        new = str((incoming or {}).get(k, "") or "").strip()
        old = str(merged.get(k, "") or "").strip()
        if not new:
            continue
        if not old:
            merged[k] = new
        elif compact(old) != compact(new):
            conflicts.append({"field": k, "label": KEY_TO_LABEL[k], "existing": old, "scanned": new})

    notes = []
    old_remarks = str(merged.get("remarks", "") or "").strip()
    if old_remarks:
        notes.append(old_remarks)
    if source_doc:
        notes.append("Source: " + source_doc)
    if uncertain:
        notes.append("Uncertain: " + ", ".join(uncertain))
    if conflicts:
        notes.append("Conflicts: " + " | ".join(
            f'{c["label"]}: existing="{c["existing"]}" scanned="{c["scanned"]}"' for c in conflicts
        ))

    merged["verification_status"] = "VERIFY" if conflicts or uncertain else "OK"
    merged["remarks"] = " || ".join(notes)
    merged["last_updated"] = datetime.now().isoformat(timespec="seconds")
    return merged, conflicts

def find_record_by_row(records, rownum):
    for rec in records:
        if int(rec.get("_row", 0) or 0) == int(rownum or 0):
            return rec
    return None

@app.route("/")
def index():
    return render_template("index.html", fields=FIELDS)

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "version": "2026.09.29.4",
        "gemini_key_set": bool(gemini_key()),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        "gemini_fallback_model": os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash"),
        "google_sheet_configured": bool(
            os.getenv("GOOGLE_SHEET_ID", "").strip()
            and os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        ),
        "aadhaar_name_removed": True,
        "cts_rule": "CTS Number = SSA AadhaarUID; Aadhaar Number is separate",
    })

@app.route("/api/search")
def search():
    q = norm(request.args.get("q", ""))
    if not q:
        return jsonify([])
    try:
        _, records = read_records()
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    out = []
    fields = [
        "gr_number", "pen_number", "cts_number", "abha_number", "aadhaar_number",
        "apaar_number", "student_full_name", "aadhaar_according_name", "father_name", "mother_name"
    ]
    for rec in records:
        hay = " | ".join(norm(rec.get(k)) for k in fields)
        if q in hay:
            out.append(candidate_summary(rec))
        if len(out) >= 30:
            break
    return jsonify(out)

@app.route("/api/extract", methods=["POST"])
def extract():
    f = request.files.get("image")
    selected_doc = request.form.get("doc_type", "AUTO")
    if not f:
        return jsonify({"error": "Image missing"}), 400
    if not (f.mimetype or "").startswith("image/"):
        return jsonify({"error": "ફક્ત image file scan કરી શકાય છે."}), 415

    image_bytes = f.read()
    if not image_bytes:
        return jsonify({"error": "ખાલી image file છે."}), 400
    if len(image_bytes) > 8 * 1024 * 1024:
        return jsonify({"error": "Image બહુ મોટી છે. ફરી ફોટો લો."}), 413

    try:
        result = ai_extract(image_bytes, f.mimetype or "image/jpeg", selected_doc)
        _, records = read_records()
        result["match"] = match_records(result["data"], records)
        return jsonify(result)
    except ProviderBusy as e:
        return jsonify({"error": str(e), "retryable": True, "code": "AI_BUSY"}), 503
    except ProviderError as e:
        return jsonify({"error": str(e), "retryable": False, "code": "AI_ERROR"}), 502
    except SheetConfigError as e:
        return jsonify({"error": str(e), "retryable": False, "code": "SHEET_CONFIG"}), 503
    except Exception as e:
        app.logger.exception("extract failed")
        return jsonify({"error": "Scannerમાં internal error આવ્યો. ફરી પ્રયાસ કરો.", "code": "INTERNAL"}), 500

@app.route("/api/upsert", methods=["POST"])
def upsert():
    payload = request.get_json(silent=True) or {}
    incoming = sanitize(payload.get("data", {}))
    uncertain = [k for k in (payload.get("uncertain_fields") or []) if k in ALL_KEYS]
    source_doc = normalize_doc_type(payload.get("document_type", "OTHER"))
    requested_row = payload.get("target_row")
    force_new = bool(payload.get("force_new"))

    if not any(incoming.values()):
        return jsonify({"error": "Save કરવા માટે કોઈ data નથી."}), 400

    try:
        ws, records = read_records()
        target = None
        if requested_row:
            target = find_record_by_row(records, requested_row)
            if not target:
                return jsonify({"error": "પસંદ કરેલો વિદ્યાર્થી row હવે મળતો નથી. ફરી scan/search કરો."}), 409
        elif not force_new:
            match = match_records(incoming, records)
            if match["status"] == "matched":
                target = find_record_by_row(records, match["row"])
            elif match["status"] == "ambiguous":
                return jsonify({
                    "error": "એકથી વધુ શક્ય વિદ્યાર્થીઓ મળ્યા. સાચો વિદ્યાર્થી પસંદ કરો.",
                    "code": "AMBIGUOUS",
                    "candidates": match["candidates"],
                }), 409

        if target:
            merged, conflicts = merge_into(target, incoming, uncertain, source_doc)
            rownum = target["_row"]
            ws.update(
                values=[record_values(merged)],
                range_name=f"A{rownum}:X{rownum}",
                value_input_option="USER_ENTERED",
            )
            action = "updated"
        else:
            merged, conflicts = merge_into({}, incoming, uncertain, source_doc)
            ws.append_row(record_values(merged), value_input_option="USER_ENTERED")
            rownum = len(records) + 2
            action = "created"

        return jsonify({
            "ok": True,
            "action": action,
            "row": rownum,
            "status": merged["verification_status"],
            "conflicts": conflicts,
            "sheet_synced": True,
        })
    except SheetConfigError as e:
        return jsonify({"error": str(e)}), 503
    except Exception:
        app.logger.exception("upsert failed")
        return jsonify({"error": "Google Sheet save દરમિયાન error આવ્યો."}), 500

ALIASES = {
    "roll_number": ["roll number", "roll no", "rollno", "roll"],
    "gr_number": ["gr number", "grno", "gr no"],
    "cts_number": ["cts number", "cts no", "aadhaaruid", "aadhaar uid"],
    "abha_number": ["abha number", "abha no"],
    "pen_number": ["pen number", "student pen", "pen no", "pen"],
    "apaar_number": ["apaar number", "apaar id", "apaar no", "apaar"],
    "aadhaar_number": ["aadhaar number", "aadhaar no", "aadhar number", "aadhar no"],
    "aadhaar_according_name": ["name as per aadhaar", "aadhaar name", "aadhar name", "aadhaar મુજબ નામ", "as per aadhaar name"],
    "student_full_name": ["student full name", "studentname", "student name", "name"],
    "mother_name": ["mothername", "mother name"],
    "father_name": ["fathername", "father name"],
    "phone_number": ["phone number", "mobile", "mobile number", "phone"],
    "address": ["address", "સરનામું"],
    "admission_date": ["doa", "admission date", "date of admission"],
    "dob": ["dob", "date of birth"],
    "sub_caste": ["sub caste", "subcaste", "પેટા જાતિ"],
    "bank_account_name": ["bank account name", "account holder name"],
    "bank_account_number": ["bank account number", "account number"],
    "bank_branch": ["bank branch", "branch"],
    "ifsc_code": ["ifsc", "ifsc code"],
    "bank_name": ["bank name"],
}

@app.route("/api/import", methods=["POST"])
def import_master():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "File missing"}), 400
    raw = f.read()
    name = (f.filename or "").lower()
    try:
        df = pd.read_csv(BytesIO(raw), dtype=str).fillna("") if name.endswith(".csv") else pd.read_excel(BytesIO(raw), dtype=str).fillna("")
    except Exception as e:
        return jsonify({"error": f"File read failed: {e}"}), 400

    cmap = {norm(c): c for c in df.columns}
    chosen = {}
    for key, opts in ALIASES.items():
        for alias in opts:
            if norm(alias) in cmap:
                chosen[key] = cmap[norm(alias)]
                break

    if not chosen:
        return jsonify({"error": "આ fileમાંથી કોઈ ઓળખીતું column map થયું નથી."}), 400

    try:
        ws, records = read_records()
        by_row = {r["_row"]: r for r in records}
        inserted = updated = skipped_ambiguous = conflicts_count = 0

        for _, row in df.iterrows():
            incoming = sanitize({
                k: (str(row[chosen[k]]).strip() if k in chosen else "")
                for k in ALL_KEYS
            })
            if not any(incoming.values()):
                continue

            match = match_records(incoming, list(by_row.values()))
            if match["status"] == "ambiguous":
                skipped_ambiguous += 1
                continue

            if match["status"] == "matched":
                rec = by_row[match["row"]]
                merged, conflicts = merge_into(rec, incoming, [], "IMPORT")
                merged["_row"] = rec["_row"]
                by_row[rec["_row"]] = merged
                updated += 1
                conflicts_count += len(conflicts)
            else:
                new_rownum = (max(by_row.keys()) + 1) if by_row else 2
                merged, conflicts = merge_into({}, incoming, [], "IMPORT")
                merged["_row"] = new_rownum
                by_row[new_rownum] = merged
                inserted += 1

        ordered = [by_row[k] for k in sorted(by_row)]
        matrix = [HEADERS] + [record_values(r) for r in ordered]
        ws.update(values=matrix, range_name="A1", value_input_option="USER_ENTERED")

        return jsonify({
            "ok": True,
            "inserted": inserted,
            "updated": updated,
            "skipped_ambiguous": skipped_ambiguous,
            "conflicts": conflicts_count,
            "mapped_columns": chosen,
        })
    except Exception as e:
        app.logger.exception("import failed")
        return jsonify({"error": f"Import save failed: {e}"}), 500

@app.route("/api/export.xlsx")
def export_xlsx():
    try:
        _, records = read_records()
        rows = []
        for rec in records:
            row = {KEY_TO_LABEL[k]: rec.get(k, "") for k in ALL_KEYS}
            row.update({
                "Verification Status": rec.get("verification_status", ""),
                "Remarks": rec.get("remarks", ""),
                "Last Updated": rec.get("last_updated", ""),
            })
            rows.append(row)
        df = pd.DataFrame(rows, columns=HEADERS)
        out = BytesIO()
        df.to_excel(out, index=False)
        out.seek(0)
        return send_file(
            out,
            as_attachment=True,
            download_name="student_master_export.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/selftest")
def selftest():
    checks = {}

    checks["schema_no_duplicate_aadhaar_name"] = "aadhaar_name" not in ALL_KEYS
    abha_probe = filter_by_document({
        "abha_number": "91-7747-7278-4187",
        "student_full_name": "Test Student",
        "aadhaar_number": "123456789012",
        "father_name": "SHOULD NOT PASS",
        "address": "SHOULD NOT PASS",
    }, "ABHA_CARD")
    checks["abha_whitelist_blocks_aadhaar"] = not abha_probe["aadhaar_number"]
    checks["abha_whitelist_blocks_parent"] = not abha_probe["father_name"]
    checks["abha_whitelist_blocks_address"] = not abha_probe["address"]
    checks["abha_keeps_abha"] = abha_probe["abha_number"] == "91-7747-7278-4187"

    probe = sanitize({
        "cts_number": "240706123456789012",
        "aadhaar_number": "123456789012",
        "phone_number": "9876543210",
        "ifsc_code": "SBIN0001234",
    })
    checks["cts_kept_separate"] = probe["cts_number"] == "240706123456789012"
    checks["aadhaar_12_digit"] = probe["aadhaar_number"] == "123456789012"
    checks["phone_valid"] = probe["phone_number"] == "9876543210"
    checks["ifsc_valid"] = probe["ifsc_code"] == "SBIN0001234"

    fake_records = [{
        "_row": 2,
        "student_full_name": "Makwana Meet Mukeshbhai",
        "dob": "26-09-2011",
        **{k: "" for k in ALL_KEYS if k not in {"student_full_name", "dob"}}
    }]
    m = match_records({"student_full_name": "Meet Mukeshbhai Makwana", "dob": "26-09-2011"}, fake_records)
    checks["name_order_plus_dob_matches"] = m.get("status") == "matched"

    live = request.args.get("live") == "1"
    write = request.args.get("write") == "1"
    checks["gemini_key_set"] = bool(gemini_key())
    checks["google_sheet_configured"] = bool(
        os.getenv("GOOGLE_SHEET_ID", "").strip() and os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    )

    if live:
        try:
            ws, _ = read_records()
            checks["sheet_headers_exact"] = ws.row_values(1)[:len(HEADERS)] == HEADERS
            if write:
                dummy = {k: "" for k in ALL_KEYS}
                dummy["gr_number"] = "SELFTEST-GR"
                dummy["student_full_name"] = "DUMMY SELF TEST"
                merged, _ = merge_into({}, dummy, [], "SELFTEST")
                ws.append_row(record_values(merged), value_input_option="USER_ENTERED")
                vals = ws.get_all_values()
                found = None
                for i in range(len(vals) - 1, 0, -1):
                    row = vals[i]
                    if len(row) > 8 and row[1] == "SELFTEST-GR" and row[8] == "DUMMY SELF TEST":
                        found = i + 1
                        break
                if found:
                    ws.delete_rows(found)
                    checks["sheet_write_and_cleanup"] = True
                else:
                    checks["sheet_write_and_cleanup"] = False
        except Exception as e:
            checks["sheet_live_error"] = str(e)
            checks["sheet_headers_exact"] = False

        try:
            key = gemini_key()
            if not key:
                raise RuntimeError("key missing")
            model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": "Reply with exactly OK"}]}],
                    "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}},
                },
                timeout=(4, 10),
            )
            checks["gemini_http_status"] = r.status_code
            checks["gemini_live_ok"] = 200 <= r.status_code < 300
        except Exception as e:
            checks["gemini_live_ok"] = False
            checks["gemini_live_error"] = str(e)

    checks["ok"] = all(v is True for k, v in checks.items() if k not in {"gemini_key_set", "google_sheet_configured"} and isinstance(v, bool))
    return jsonify(checks)


def _test_png_bytes(width=64, height=32):
    # Tiny valid white RGB PNG created without Pillow.
    raw = b"".join(b"\x00" + (b"\xff\xff\xff" * width) for _ in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )

def startup_smoke_test():
    results = {}
    try:
        with app.test_client() as client:
            r = client.get("/")
            results["home_200"] = r.status_code == 200
            results["home_has_version"] = b"2026.09.29.4" in r.data
            h = client.get("/api/health")
            results["health_200"] = h.status_code == 200 and bool(h.get_json())
            s = client.get("/api/selftest")
            sj = s.get_json() or {}
            results["local_selftest_200"] = s.status_code == 200
            results["local_selftest_ok"] = bool(sj.get("ok"))
    except Exception as e:
        results["error"] = str(e)
    print("[STARTUP_SMOKE] " + json.dumps(results, ensure_ascii=False), flush=True)
    return results

def background_external_selftest():
    time.sleep(3)
    results = {}

    # Google Sheet read/write/cleanup test.
    try:
        ws, _ = read_records()
        results["sheet_headers"] = ws.row_values(1)[:len(HEADERS)] == HEADERS
        stamp = str(int(time.time()))
        dummy = {k: "" for k in ALL_KEYS}
        dummy["gr_number"] = "SELFTEST-" + stamp
        dummy["student_full_name"] = "DUMMY SELF TEST"
        merged, _ = merge_into({}, dummy, [], "SELFTEST")
        ws.append_row(record_values(merged), value_input_option="USER_ENTERED")
        vals = ws.get_all_values()
        found = None
        for i in range(len(vals) - 1, 0, -1):
            row = vals[i]
            if len(row) > 8 and row[1] == "SELFTEST-" + stamp and row[8] == "DUMMY SELF TEST":
                found = i + 1
                break
        if found:
            ws.delete_rows(found)
            results["sheet_write_cleanup"] = True
        else:
            results["sheet_write_cleanup"] = False
    except Exception as e:
        results["sheet_error"] = str(e)
        results["sheet_write_cleanup"] = False

    # Exact image-extraction path test using a harmless blank PNG.
    try:
        x = ai_extract(_test_png_bytes(), "image/png", "ABHA_CARD")
        results["ai_image_pipeline"] = isinstance(x.get("data"), dict)
        results["ai_model_used"] = x.get("model_used", "")
        # Whitelist must prevent unrelated ABHA scan fields.
        results["ai_abha_scope_safe"] = all(
            not x["data"].get(k)
            for k in ("aadhaar_number", "aadhaar_according_name", "father_name", "mother_name", "address")
        )
    except ProviderBusy as e:
        results["ai_image_pipeline"] = False
        results["ai_provider_busy"] = str(e)
    except Exception as e:
        results["ai_image_pipeline"] = False
        results["ai_error"] = str(e)

    print("[EXTERNAL_SELFTEST] " + json.dumps(results, ensure_ascii=False), flush=True)

STARTUP_SMOKE = startup_smoke_test()
threading.Thread(target=background_external_selftest, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
