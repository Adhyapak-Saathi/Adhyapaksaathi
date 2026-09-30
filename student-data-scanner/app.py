import os, json, re, base64, time, threading, zlib, struct, binascii
from datetime import datetime
from io import BytesIO
import pandas as pd
import requests
import pymupdf as fitz
from flask import Flask, render_template, request, jsonify, send_file

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

FIELDS = [
    ("academic_year", "Academic Year"),
    ("standard", "Standard"),
    ("division", "Division"),
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

_RECORD_CACHE = {"ts": 0.0, "records": []}
_GSHEET_CACHE = {"ss": None, "main_ws": None, "history_ws": None}
_CACHE_LOCK = threading.Lock()

DOC_ALLOWED = {
    "ABHA_CARD": {"abha_number", "student_full_name", "dob", "phone_number"},
    "AADHAAR_CARD": {"aadhaar_number", "aadhaar_according_name", "dob", "address"},
    "BANK_DOCUMENT": {"bank_account_name", "bank_account_number", "bank_branch", "ifsc_code", "bank_name"},
    "SCHOOL_RECORD": {
        "academic_year", "standard", "division", "roll_number", "gr_number", "cts_number", "abha_number", "pen_number", "apaar_number",
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

    ay = out.get("academic_year", "").replace("–", "-").replace("—", "-").strip()
    m = re.fullmatch(r"(20\d{2})\s*-\s*(?:20)?(\d{2})", ay)
    if m:
        out["academic_year"] = f"{m.group(1)}-{m.group(2)}"
    elif ay:
        out["academic_year"] = ay

    std = only_digits(out.get("standard", ""))
    out["standard"] = std if (not std or 1 <= int(std) <= 12) else ""
    out["division"] = re.sub(r"[^A-Za-z0-9]", "", out.get("division", "")).upper()[:4]
    return out

def sheet_client():
    if gspread is None or Credentials is None:
        raise SheetConfigError("Google Sheets library ઉપલબ્ધ નથી.")

    with _CACHE_LOCK:
        cached = _GSHEET_CACHE.get("ss")
    if cached is not None:
        return cached

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
    ss = gspread.authorize(creds).open_by_key(sid)
    with _CACHE_LOCK:
        _GSHEET_CACHE["ss"] = ss
    return ss

def get_ws():
    with _CACHE_LOCK:
        cached = _GSHEET_CACHE.get("main_ws")
    if cached is not None:
        return cached

    ss = sheet_client()
    tab = os.getenv("GOOGLE_SHEET_TAB", "Student_Master").strip() or "Student_Master"
    try:
        ws = ss.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound as e:
        raise SheetConfigError(f'Google Sheet tab "{tab}" મળ્યો નથી.') from e

    with _CACHE_LOCK:
        _GSHEET_CACHE["main_ws"] = ws
    return ws

def ensure_sheet_schema(ws):
    current = ws.row_values(1)
    if not current:
        ws.update(values=[HEADERS], range_name="A1", value_input_option="USER_ENTERED")
        return
    if current[:len(HEADERS)] != HEADERS:
        raise SheetConfigError(
            "Google Sheet columns app schema સાથે match થતા નથી. Sheet auto-clear કરવામાં આવશે નહીં."
        )

HISTORY_HEADERS = [
    "Recorded At", "GR Number", "PEN Number", "CTS Number", "Student Name",
    "Academic Year", "Standard", "Division", "Roll Number", "Action"
]

def get_history_ws():
    with _CACHE_LOCK:
        cached = _GSHEET_CACHE.get("history_ws")
    if cached is not None:
        return cached

    ss = sheet_client()
    try:
        ws = ss.worksheet("Enrollment_History")
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title="Enrollment_History", rows=2000, cols=len(HISTORY_HEADERS))

    current = ws.row_values(1)
    if current[:len(HISTORY_HEADERS)] != HISTORY_HEADERS:
        ws.update(values=[HISTORY_HEADERS], range_name="A1", value_input_option="RAW")

    with _CACHE_LOCK:
        _GSHEET_CACHE["history_ws"] = ws
    return ws

def append_enrollment_history(rec, action):
    values = [
        datetime.now().isoformat(timespec="seconds"),
        str(rec.get("gr_number", "") or ""),
        str(rec.get("pen_number", "") or ""),
        str(rec.get("cts_number", "") or ""),
        str(rec.get("student_full_name", "") or rec.get("aadhaar_according_name", "") or ""),
        str(rec.get("academic_year", "") or ""),
        str(rec.get("standard", "") or ""),
        str(rec.get("division", "") or ""),
        str(rec.get("roll_number", "") or ""),
        action,
    ]
    get_history_ws().append_row(values, value_input_option="RAW")

def enrollment_tuple(rec):
    return tuple(str((rec or {}).get(k, "") or "").strip() for k in ("academic_year","standard","division","roll_number"))

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

def cached_records(max_age=45):
    now = time.monotonic()
    with _CACHE_LOCK:
        if _RECORD_CACHE["records"] and (now - _RECORD_CACHE["ts"] <= max_age):
            return [dict(r) for r in _RECORD_CACHE["records"]]
    _, records = read_records()
    with _CACHE_LOCK:
        _RECORD_CACHE["ts"] = now
        _RECORD_CACHE["records"] = [dict(r) for r in records]
    return records

def invalidate_record_cache():
    with _CACHE_LOCK:
        _RECORD_CACHE["ts"] = 0.0
        _RECORD_CACHE["records"] = []

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
        "academic_year": rec.get("academic_year", ""),
        "standard": rec.get("standard", ""),
        "division": rec.get("division", ""),
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
    return f"""Read ONE student/school document supplied as an image or PDF in Gujarati, English, or Hindi.
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
- Return all textual values in English script. Transliterate personal/place/proper names into natural English spelling; translate ordinary address/descriptive words into English. Never translate or alter identifiers, account numbers, dates, phone numbers or IFSC codes.
- Dates must be DD-MM-YYYY only when a complete date is clear.
- If a field is unclear, return an empty string and add that field key to uncertain_fields.
- If selected type is not AUTO, document_type must reflect the real document but do not extract fields outside the allowed list.
- When multiple page images are supplied, they are pages of ONE PDF in page order. Read all pages together as one document.
- If a multi-page PDF clearly contains more than one document type, set document_type to OTHER and extract only fields explicitly visible anywhere in that PDF.
"""

def render_pdf_pages(pdf_bytes, max_pages=10):
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ProviderError("PDF file ખૂલી શક્યું નથી.") from e
    try:
        if doc.needs_pass:
            raise ProviderError("Password-protected PDF વાંચી શકાતું નથી.")
        if doc.page_count < 1:
            raise ProviderError("PDFમાં કોઈ page મળ્યો નથી.")
        if doc.page_count > max_pages:
            raise ProviderError(f"PDFમાં {doc.page_count} pages છે. એક વખતમાં વધુમાં વધુ {max_pages} pages રાખો.")
        pages = []
        matrix = fitz.Matrix(1.65, 1.65)
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            page_bytes = pix.tobytes("jpeg")
            pages.append(page_bytes)
        return pages
    finally:
        doc.close()

def gemini_key():
    return (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GEMINI_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
        or os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
    )

def extract_document_data(file_bytes, mime_type, selected_doc):
    key = gemini_key()
    if not key:
        raise ProviderError("Document reading service configured નથી.")

    selected_doc = normalize_doc_type(selected_doc)
    requested_keys = sorted(DOC_ALLOWED[selected_doc] if selected_doc != "AUTO" else set(ALL_KEYS))
    prompt = build_prompt(selected_doc, requested_keys)
    schema = extraction_schema(requested_keys)

    if mime_type == "application/pdf":
        page_images = render_pdf_pages(file_bytes)
        media_parts = [
            {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(page).decode("ascii")}}
            for page in page_images
        ]
        source_page_count = len(page_images)
    else:
        media_parts = [
            {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(file_bytes).decode("ascii")}}
        ]
        source_page_count = 1

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
                "parts": [{"text": prompt}] + media_parts
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
                timeout=(5, min(38, 18 + (source_page_count * 4))),
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
            raise ProviderError(f"Document reading failed ({model}, HTTP {r.status_code}): {detail[:220]}")

        try:
            obj = r.json()
            parts = obj.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            txt = "".join(p.get("text", "") for p in parts).strip()
            result = json.loads(txt)
        except Exception as e:
            raise ProviderError("Document readerએ માન્ય structured data પાછું આપ્યું નથી.") from e

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
            "source_pages": source_page_count,
        }

    raise ProviderBusy("Document reading service હાલમાં વ્યસ્ત છે. થોડા સેકન્ડ પછી ફરી પ્રયાસ કરો.")

ENGLISH_TEXT_KEYS = {
    "aadhaar_according_name", "student_full_name", "mother_name", "father_name",
    "address", "sub_caste", "bank_account_name", "bank_branch", "bank_name"
}

def has_non_english_letters(value):
    return any(ord(ch) > 127 and ch.isalpha() for ch in str(value or ""))

def convert_record_to_english(data):
    pending = {
        k: str((data or {}).get(k, "") or "").strip()
        for k in ENGLISH_TEXT_KEYS
        if str((data or {}).get(k, "") or "").strip()
        and has_non_english_letters((data or {}).get(k, ""))
    }
    if not pending:
        return dict(data or {}), False

    key = gemini_key()
    if not key:
        raise ProviderError("English conversion service configured નથી.")

    props = {k: {"type": "string"} for k in pending}
    schema = {
        "type": "object",
        "properties": props,
        "required": list(pending.keys()),
        "additionalProperties": False,
    }
    prompt = """Convert the supplied student-record text values to English script only.
Rules:
- Personal, parent, caste, bank and place names: transliterate faithfully; do not invent or expand names.
- Address/descriptive words: translate to clear English while preserving all place names, house numbers, PIN codes and numbers.
- Preserve meaning and spelling as closely as possible.
- Return only the requested JSON fields, no notes.
Input:
""" + json.dumps(pending, ensure_ascii=False)

    primary = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    fallback = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
    last = ""
    for model in dict.fromkeys([primary, fallback]):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "thinkingConfig": {"thinkingLevel": "low"},
                        "responseFormat": {
                            "text": {
                                "mimeType": "APPLICATION_JSON",
                                "schema": schema
                            }
                        }
                    },
                },
                timeout=(5, 14),
            )
        except requests.RequestException as e:
            last = str(e)
            continue
        if r.status_code in {429, 500, 502, 503, 504}:
            last = f"HTTP {r.status_code}"
            continue
        if r.status_code >= 300:
            raise ProviderError("English conversion પૂર્ણ થઈ શક્યું નથી.")
        try:
            obj = r.json()
            parts = obj.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            translated = json.loads("".join(p.get("text", "") for p in parts).strip())
        except Exception as e:
            raise ProviderError("English conversion response માન્ય નથી.") from e

        out = dict(data or {})
        for k, original in pending.items():
            converted = str(translated.get(k, "") or "").strip()
            if converted:
                out[k] = converted
            else:
                out[k] = original
        return out, True

    raise ProviderBusy("English conversion service હાલમાં ઉપલબ્ધ નથી. ફરી પ્રયાસ કરો.")

def verify_sheet_row(ws, rownum, expected):
    expected_values = [str(expected.get(k, "") or "") for k in ALL_KEYS]
    for _ in range(3):
        row = ws.row_values(int(rownum))
        actual = (row + [""] * len(HEADERS))[:len(ALL_KEYS)]
        if actual == expected_values:
            return True
        time.sleep(0.35)
    return False

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
    def add_note(note):
        note = str(note or "").strip()
        if note and note not in notes:
            notes.append(note)

    old_remarks = str(merged.get("remarks", "") or "").strip()
    for note in old_remarks.split(" || "):
        add_note(note)
    if source_doc:
        add_note("Source: " + source_doc)
    if uncertain:
        add_note("Uncertain: " + ", ".join(sorted(set(uncertain))))
    if conflicts:
        add_note("Conflicts: " + " | ".join(
            f'{c["label"]}: existing="{c["existing"]}" scanned="{c["scanned"]}"' for c in conflicts
        ))

    old_status = str((existing or {}).get("verification_status", "") or "").strip().upper()
    merged["verification_status"] = "VERIFY" if conflicts or uncertain or old_status == "VERIFY" else "OK"
    remarks = " || ".join(notes)
    merged["remarks"] = remarks[-3500:] if len(remarks) > 3500 else remarks
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
        "version": "2026.09.29.7",
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

@app.route("/api/filter-options")
def filter_options():
    try:
        records = cached_records()
        years = sorted({str(r.get("academic_year","")).strip() for r in records if str(r.get("academic_year","")).strip()}, reverse=True)
        standards = sorted({str(r.get("standard","")).strip() for r in records if str(r.get("standard","")).strip()}, key=lambda x: int(x) if x.isdigit() else 99)
        divisions = sorted({str(r.get("division","")).strip() for r in records if str(r.get("division","")).strip()})
        return jsonify({"academic_years": years, "standards": standards, "divisions": divisions})
    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.route("/api/search")
def search():
    raw_q = str(request.args.get("q", "") or "").strip()
    year_filter = str(request.args.get("academic_year", "") or "").strip()
    standard_filter = str(request.args.get("standard", "") or "").strip()
    division_filter = str(request.args.get("division", "") or "").strip().upper()
    if not raw_q and not any([year_filter, standard_filter, division_filter]):
        return jsonify([])

    queries = [norm(raw_q)] if raw_q else []
    if raw_q and has_non_english_letters(raw_q):
        try:
            translated, _ = convert_record_to_english({"student_full_name": raw_q})
            q2 = norm(translated.get("student_full_name", ""))
            if q2 and q2 not in queries:
                queries.append(q2)
        except Exception:
            pass

    try:
        records = cached_records()
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    out = []
    fields = [
        "gr_number", "pen_number", "cts_number", "abha_number", "aadhaar_number",
        "apaar_number", "student_full_name", "aadhaar_according_name", "father_name", "mother_name"
    ]
    for rec in records:
        if year_filter and str(rec.get("academic_year","")).strip() != year_filter:
            continue
        if standard_filter and str(rec.get("standard","")).strip() != standard_filter:
            continue
        if division_filter and str(rec.get("division","")).strip().upper() != division_filter:
            continue

        if queries:
            hay = " | ".join(norm(rec.get(k)) for k in fields)
            matched = False
            for q in queries:
                if q and q in hay:
                    matched = True
                    break
                tokens = [t for t in re.split(r"\s+", q) if len(t) >= 2]
                if tokens and all(t in hay for t in tokens):
                    matched = True
                    break
            if not matched:
                continue

        out.append(candidate_summary(rec))
        if len(out) >= 50:
            break
    return jsonify(out)

@app.route("/api/student/<int:rownum>")
def student_record(rownum):
    try:
        records = cached_records()
        rec = find_record_by_row(records, rownum)
        if not rec:
            return jsonify({"error": "વિદ્યાર્થી મળ્યો નથી."}), 404
        return jsonify({
            "row": rec["_row"],
            "data": {k: rec.get(k, "") for k in ALL_KEYS},
            "verification_status": rec.get("verification_status", ""),
            "remarks": rec.get("remarks", ""),
            "last_updated": rec.get("last_updated", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.route("/api/extract", methods=["POST"])
def extract():
    f = request.files.get("image")
    selected_doc = request.form.get("doc_type", "AUTO")
    if not f:
        return jsonify({"error": "Image missing"}), 400
    mime = (f.mimetype or "").lower()
    if not (mime.startswith("image/") or mime == "application/pdf"):
        return jsonify({"error": "ફક્ત Photo/Image અથવા PDF document અપલોડ કરી શકાય છે."}), 415

    file_bytes = f.read()
    if not file_bytes:
        return jsonify({"error": "ખાલી document file છે."}), 400
    if len(file_bytes) > 15 * 1024 * 1024:
        return jsonify({"error": "Document 15 MBથી મોટું છે. નાનું file પસંદ કરો."}), 413

    try:
        result = extract_document_data(file_bytes, mime or "image/jpeg", selected_doc)
        try:
            records = cached_records()
            result["match"] = match_records(result["data"], records)
        except Exception as match_error:
            result["match"] = {"status": "unavailable", "candidates": []}
            result["match_warning"] = "વિદ્યાર્થી match હમણાં ઉપલબ્ધ નથી."
            app.logger.warning("auto-match unavailable: %s", match_error)
        return jsonify(result)
    except ProviderBusy as e:
        return jsonify({"error": str(e), "retryable": True, "code": "Document_BUSY"}), 503
    except ProviderError as e:
        return jsonify({"error": str(e), "retryable": False, "code": "Document_ERROR"}), 502
    except SheetConfigError as e:
        return jsonify({"error": str(e), "retryable": False, "code": "SHEET_CONFIG"}), 503
    except Exception as e:
        app.logger.exception("extract failed")
        return jsonify({"error": "Scannerમાં internal error આવ્યો. ફરી પ્રયાસ કરો.", "code": "INTERNAL"}), 500

@app.route("/api/match", methods=["POST"])
def match_current_data():
    payload = request.get_json(silent=True) or {}
    data = sanitize(payload.get("data", {}))
    if not any(data.values()):
        return jsonify({"status": "none", "candidates": []})
    try:
        records = cached_records()
        return jsonify(match_records(data, records))
    except Exception as e:
        app.logger.warning("match unavailable: %s", e)
        return jsonify({"status": "unavailable", "candidates": []}), 200

@app.route("/api/upsert", methods=["POST"])
def upsert():
    payload = request.get_json(silent=True) or {}
    incoming = sanitize(payload.get("data", {}))
    uncertain = [k for k in (payload.get("uncertain_fields") or []) if k in ALL_KEYS]
    source_docs = payload.get("document_types") or [payload.get("document_type", "OTHER")]
    clean_sources = []
    for source in source_docs:
        source = normalize_doc_type(source)
        if source and source not in clean_sources:
            clean_sources.append(source)
    source_doc = ", ".join(clean_sources) if clean_sources else "OTHER"
    requested_row = payload.get("target_row")
    force_new = bool(payload.get("force_new"))
    allow_enrollment_change = bool(payload.get("allow_enrollment_change"))

    if not any(incoming.values()):
        return jsonify({"error": "Save કરવા માટે કોઈ data નથી."}), 400

    try:
        incoming, english_converted = convert_record_to_english(incoming)
        incoming = sanitize(incoming)
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

        enrollment_changed = False
        history_entries = []
        if target:
            old_enrollment = enrollment_tuple(target)
            incoming_enrollment = enrollment_tuple(incoming)
            proposed = list(old_enrollment)
            for idx, val in enumerate(incoming_enrollment):
                if val:
                    proposed[idx] = val
            proposed = tuple(proposed)
            enrollment_changed = proposed != old_enrollment and any(incoming_enrollment)

            if enrollment_changed and any(old_enrollment) and not allow_enrollment_change:
                return jsonify({
                    "error": "Academic Year / Standard / Division / Roll Number બદલાઈ રહ્યા છે. Promotion/Class Change તરીકે પુષ્ટિ કરો.",
                    "code": "PROMOTION_CONFIRM_REQUIRED",
                    "current_enrollment": {
                        "academic_year": old_enrollment[0], "standard": old_enrollment[1],
                        "division": old_enrollment[2], "roll_number": old_enrollment[3]
                    },
                    "new_enrollment": {
                        "academic_year": proposed[0], "standard": proposed[1],
                        "division": proposed[2], "roll_number": proposed[3]
                    }
                }), 409

            merge_incoming = dict(incoming)
            if enrollment_changed and allow_enrollment_change:
                for k in ("academic_year","standard","division","roll_number"):
                    if merge_incoming.get(k):
                        target[k] = merge_incoming[k]
                        merge_incoming[k] = ""

            merged, conflicts = merge_into(target, merge_incoming, uncertain, source_doc)
            rownum = target["_row"]

            if enrollment_changed and allow_enrollment_change:
                old_record = dict(target)
                old_record["academic_year"], old_record["standard"], old_record["division"], old_record["roll_number"] = old_enrollment
                history_entries.append((old_record, "PROMOTED_FROM"))

            ws.update(
                values=[record_values(merged)],
                range_name=f"A{rownum}:AA{rownum}",
                value_input_option="RAW",
            )
            action = "updated"

            if enrollment_changed and allow_enrollment_change:
                history_entries.append((dict(merged), "PROMOTED_TO"))
            elif enrollment_changed and not any(old_enrollment):
                history_entries.append((dict(merged), "INITIAL"))
        else:
            merged, conflicts = merge_into({}, incoming, uncertain, source_doc)
            append_result = ws.append_row(record_values(merged), value_input_option="RAW")
            updated_range = str(((append_result or {}).get("updates") or {}).get("updatedRange", ""))
            match_row = re.search(r"!A(\d+):", updated_range)
            rownum = int(match_row.group(1)) if match_row else (len(records) + 2)
            action = "created"
            if any(enrollment_tuple(merged)):
                history_entries.append((dict(merged), "INITIAL"))

        sheet_verified = verify_sheet_row(ws, rownum, merged)
        if not sheet_verified:
            return jsonify({
                "error": "માહિતી લખવાની પ્રક્રિયા પૂર્ણ થઈ પરંતુ Google Sheetમાં તેની પુષ્ટિ થઈ શકી નથી.",
                "code": "SAVE_NOT_CONFIRMED"
            }), 502

        history_saved = True
        if history_entries:
            try:
                for history_rec, history_action in history_entries:
                    append_enrollment_history(history_rec, history_action)
            except Exception as history_error:
                history_saved = False
                app.logger.exception("enrollment history save failed")

        invalidate_record_cache()
        return jsonify({
            "ok": True,
            "sheet_verified": True,
            "english_converted": english_converted,
            "saved_data": {k: merged.get(k, "") for k in ALL_KEYS},
            "action": action,
            "row": rownum,
            "status": merged["verification_status"],
            "conflicts": conflicts,
            "sheet_synced": True,
            "enrollment_changed": enrollment_changed,
            "history_saved": history_saved,
        })
    except ProviderBusy as e:
        return jsonify({"error": str(e), "code": "ENGLISH_CONVERSION_BUSY"}), 503
    except ProviderError as e:
        return jsonify({"error": str(e), "code": "ENGLISH_CONVERSION_ERROR"}), 502
    except SheetConfigError as e:
        return jsonify({"error": str(e)}), 503
    except Exception:
        app.logger.exception("upsert failed")
        return jsonify({"error": "Google Sheet save દરમિયાન error આવ્યો."}), 500

ALIASES = {
    "academic_year": ["academic year", "year", "school year", "session"],
    "standard": ["standard", "std", "class", "grade"],
    "division": ["division", "section", "div"],
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
        ws.update(values=matrix, range_name="A1", value_input_option="RAW")
        invalidate_record_cache()

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
        year_filter = str(request.args.get("academic_year", "") or "").strip()
        standard_filter = str(request.args.get("standard", "") or "").strip()
        division_filter = str(request.args.get("division", "") or "").strip().upper()
        _, records = read_records()

        filtered = []
        for rec in records:
            if year_filter and str(rec.get("academic_year","")).strip() != year_filter:
                continue
            if standard_filter and str(rec.get("standard","")).strip() != standard_filter:
                continue
            if division_filter and str(rec.get("division","")).strip().upper() != division_filter:
                continue
            filtered.append(rec)

        rows = []
        for rec in filtered:
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

        suffix = "_".join(x for x in [year_filter, ("Std"+standard_filter if standard_filter else ""), division_filter] if x)
        filename = "student_master" + (("_" + suffix) if suffix else "") + ".xlsx"
        return send_file(
            out,
            as_attachment=True,
            download_name=filename,
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

def _test_pdf_bytes():
    doc = fitz.open()
    texts = [
        "Student Name Test Student\nABHA Number 91-1234-5678-9012\nDOB 01-01-2010",
        "School Record\nGR Number 12345\nPEN Number 12345678901",
        "Additional Page\nPhone 9876543210",
    ]
    for txt in texts:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), txt, fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data

def startup_smoke_test():
    results = {}
    try:
        with app.test_client() as client:
            home = client.get("/")
            results["home_200"] = home.status_code == 200
            results["home_has_title"] = b"Student Record Capture" in home.data
            health_response = client.get("/api/health")
            results["health_200"] = health_response.status_code == 200 and bool(health_response.get_json())
            local = client.get("/api/selftest")
            local_json = local.get_json() or {}
            results["local_selftest_200"] = local.status_code == 200
            results["local_selftest_ok"] = bool(local_json.get("ok"))
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
        ws.append_row(record_values(merged), value_input_option="RAW")
        vals = ws.get_all_values()
        found = None
        for i in range(len(vals) - 1, 0, -1):
            row = vals[i]
            if len(row) > 8 and row[4] == "SELFTEST-" + stamp and row[11] == "DUMMY SELF TEST":
                found = i + 1
                break
        if found:
            results["sheet_readback_verified"] = verify_sheet_row(ws, found, merged)
            ws.delete_rows(found)
            results["sheet_write_cleanup"] = True
        else:
            results["sheet_write_cleanup"] = False
    except Exception as e:
        results["sheet_error"] = str(e)
        results["sheet_write_cleanup"] = False

    # Exact image-extraction path test using a harmless blank PNG.
    try:
        x = extract_document_data(_test_png_bytes(), "image/png", "ABHA_CARD")
        results["document_image_pipeline"] = isinstance(x.get("data"), dict)
        results["reader_model_used"] = x.get("model_used", "")
        # Whitelist must prevent unrelated ABHA scan fields.
        results["abha_scope_safe"] = all(
            not x["data"].get(k)
            for k in ("aadhaar_number", "aadhaar_according_name", "father_name", "mother_name", "address")
        )
    except ProviderBusy as e:
        results["document_image_pipeline"] = False
        results["reader_provider_busy"] = str(e)
    except Exception as e:
        results["document_image_pipeline"] = False
        results["reader_error"] = str(e)

    try:
        pdf_result = extract_document_data(_test_pdf_bytes(), "application/pdf", "ABHA_CARD")
        results["document_pdf_pipeline"] = isinstance(pdf_result.get("data"), dict)
        results["pdf_abha_scope_safe"] = all(
            not pdf_result["data"].get(k)
            for k in ("aadhaar_number", "aadhaar_according_name", "father_name", "mother_name", "address")
        )
    except ProviderBusy as e:
        results["document_pdf_pipeline"] = False
        results["pdf_provider_busy"] = str(e)
    except Exception as e:
        results["document_pdf_pipeline"] = False
        results["pdf_error"] = str(e)

    try:
        converted, changed = convert_record_to_english({
            "student_full_name": "ઠાકોર ચંદ્રિકાબેન",
            "address": "અમદાવાદ ગુજરાત"
        })
        results["english_conversion"] = bool(
            changed
            and converted.get("student_full_name")
            and converted.get("address")
            and not has_non_english_letters(converted.get("student_full_name"))
            and not has_non_english_letters(converted.get("address"))
        )
    except Exception as e:
        results["english_conversion"] = False
        results["english_conversion_error"] = str(e)

    print("[EXTERNAL_SELFTEST] " + json.dumps(results, ensure_ascii=False), flush=True)


def background_extended_qa():
    if os.getenv("RUN_EXTENDED_QA", "").strip() != "1":
        return

    time.sleep(12)
    results = {}
    stamp = str(int(time.time()))
    qa_gr = "QA-" + stamp
    qa_pen = "9" + stamp[-10:]
    qa_cts = "24" + stamp[-16:]
    qa_aadhaar = ("7" + stamp * 2)[:12]

    def cleanup():
        try:
            ws, _ = read_records()
            values = ws.get_all_values()
            rows = []
            for idx, row in enumerate(values[1:], start=2):
                if len(row) > 4 and str(row[4]).strip() == qa_gr:
                    rows.append(idx)
            for rownum in reversed(rows):
                ws.delete_rows(rownum)
            invalidate_record_cache()
        except Exception as e:
            results["cleanup_master_error"] = str(e)

        try:
            hws = get_history_ws()
            values = hws.get_all_values()
            rows = []
            for idx, row in enumerate(values[1:], start=2):
                if len(row) > 1 and str(row[1]).strip() == qa_gr:
                    rows.append(idx)
            for rownum in reversed(rows):
                hws.delete_rows(rownum)
        except Exception as e:
            results["cleanup_history_error"] = str(e)

    try:
        cleanup()

        with app.test_client() as client:
            # 1) Partial first save.
            first_payload = {
                "data": {
                    "academic_year": "2026-27",
                    "standard": "10",
                    "division": "C",
                    "roll_number": "98",
                    "gr_number": qa_gr,
                    "pen_number": qa_pen,
                    "student_full_name": "QA Test Student",
                    "dob": "01-01-2011"
                },
                "document_types": ["FORM"],
                "uncertain_fields": []
            }
            r1 = client.post("/api/upsert", json=first_payload)
            j1 = r1.get_json() or {}
            results["partial_save_http"] = r1.status_code
            results["partial_save_ok"] = bool(
                r1.status_code == 200
                and j1.get("ok")
                and j1.get("sheet_verified")
                and j1.get("action") == "created"
            )
            first_row = j1.get("row")

            # 2) Same student, second partial save without explicit target row.
            second_payload = {
                "data": {
                    "gr_number": qa_gr,
                    "cts_number": qa_cts,
                    "aadhaar_number": qa_aadhaar,
                    "father_name": "Test Father",
                    "phone_number": "9876543210"
                },
                "document_types": ["SCHOOL_RECORD", "AADHAAR_CARD"],
                "uncertain_fields": []
            }
            r2 = client.post("/api/upsert", json=second_payload)
            j2 = r2.get_json() or {}
            results["second_partial_http"] = r2.status_code
            results["same_row_updated"] = bool(
                r2.status_code == 200
                and j2.get("ok")
                and j2.get("sheet_verified")
                and j2.get("action") == "updated"
                and j2.get("row") == first_row
            )

            # 3) Verify no duplicate and both old+new fields coexist.
            ws, records = read_records()
            qa_records = [r for r in records if str(r.get("gr_number", "")).strip() == qa_gr]
            results["no_duplicate_row"] = len(qa_records) == 1
            if qa_records:
                rec = qa_records[0]
                results["partial_merge_preserved"] = bool(
                    rec.get("student_full_name") == "QA Test Student"
                    and rec.get("dob") == "01-01-2011"
                    and rec.get("cts_number") == qa_cts
                    and rec.get("aadhaar_number") == qa_aadhaar
                    and rec.get("father_name") == "Test Father"
                    and rec.get("phone_number") == "9876543210"
                    and rec.get("academic_year") == "2026-27"
                    and rec.get("standard") == "10"
                    and rec.get("division") == "C"
                )
            else:
                results["partial_merge_preserved"] = False

            # 4) Search/filter behavior.
            good_search = client.get(
                "/api/search",
                query_string={
                    "q": qa_gr,
                    "academic_year": "2026-27",
                    "standard": "10",
                    "division": "C"
                }
            )
            good_items = good_search.get_json() or []
            results["class_filtered_search"] = bool(
                good_search.status_code == 200
                and len(good_items) == 1
                and good_items[0].get("gr_number") == qa_gr
            )

            wrong_search = client.get(
                "/api/search",
                query_string={
                    "q": qa_gr,
                    "academic_year": "2026-27",
                    "standard": "9",
                    "division": "A"
                }
            )
            wrong_items = wrong_search.get_json() or []
            results["wrong_class_excluded"] = bool(
                wrong_search.status_code == 200 and len(wrong_items) == 0
            )

            # 5) Full record endpoint should expose the merged record.
            if first_row:
                full = client.get(f"/api/student/{first_row}")
                full_json = full.get_json() or {}
                full_data = full_json.get("data") or {}
                results["full_record_load"] = bool(
                    full.status_code == 200
                    and full_data.get("gr_number") == qa_gr
                    and full_data.get("phone_number") == "9876543210"
                    and full_data.get("academic_year") == "2026-27"
                    and full_data.get("standard") == "10"
                    and full_data.get("division") == "C"
                )
            else:
                results["full_record_load"] = False

            # 6) Filtered Excel download.
            export = client.get(
                "/api/export.xlsx",
                query_string={
                    "academic_year": "2026-27",
                    "standard": "10",
                    "division": "C"
                }
            )
            results["class_filtered_export"] = bool(
                export.status_code == 200
                and "spreadsheetml" in str(export.content_type or "")
                and len(export.data or b"") > 500
            )

            # 7) Promotion must require confirmation.
            promo_payload = {
                "data": {
                    "gr_number": qa_gr,
                    "academic_year": "2027-28",
                    "standard": "11",
                    "division": "A",
                    "roll_number": "12"
                },
                "document_types": ["FORM"],
                "uncertain_fields": []
            }
            promo_block = client.post("/api/upsert", json=promo_payload)
            promo_block_json = promo_block.get_json() or {}
            results["promotion_confirmation_required"] = bool(
                promo_block.status_code == 409
                and promo_block_json.get("code") == "PROMOTION_CONFIRM_REQUIRED"
            )

            promo_payload["allow_enrollment_change"] = True
            promo = client.post("/api/upsert", json=promo_payload)
            promo_json = promo.get_json() or {}
            results["promotion_save_ok"] = bool(
                promo.status_code == 200
                and promo_json.get("ok")
                and promo_json.get("sheet_verified")
                and promo_json.get("enrollment_changed")
                and promo_json.get("history_saved")
            )

            # 8) Verify current enrollment and preserved history.
            ws, records = read_records()
            qa_records = [r for r in records if str(r.get("gr_number", "")).strip() == qa_gr]
            results["promoted_master_current"] = bool(
                len(qa_records) == 1
                and qa_records[0].get("academic_year") == "2027-28"
                and qa_records[0].get("standard") == "11"
                and qa_records[0].get("division") == "A"
                and qa_records[0].get("roll_number") == "12"
            )

            hws = get_history_ws()
            hvals = hws.get_all_values()
            actions = [
                row[9] for row in hvals[1:]
                if len(row) > 9 and str(row[1]).strip() == qa_gr
            ]
            results["history_initial"] = "INITIAL" in actions
            results["history_promoted_from"] = "PROMOTED_FROM" in actions
            results["history_promoted_to"] = "PROMOTED_TO" in actions

            # 9) Search should now move with current class.
            old_class = client.get(
                "/api/search",
                query_string={
                    "q": qa_gr,
                    "academic_year": "2026-27",
                    "standard": "10",
                    "division": "C"
                }
            ).get_json() or []
            new_class = client.get(
                "/api/search",
                query_string={
                    "q": qa_gr,
                    "academic_year": "2027-28",
                    "standard": "11",
                    "division": "A"
                }
            ).get_json() or []
            results["current_class_search_moves"] = bool(
                len(old_class) == 0
                and len(new_class) == 1
                and new_class[0].get("gr_number") == qa_gr
            )

    except Exception as e:
        results["fatal_error"] = str(e)
        app.logger.exception("extended qa failed")
    finally:
        cleanup()

    boolean_checks = [v for v in results.values() if isinstance(v, bool)]
    has_errors = any(
        key.endswith("_error") or key == "fatal_error"
        for key in results
    )
    results["all_boolean_checks_pass"] = bool(boolean_checks) and all(boolean_checks) and not has_errors
    print("[EXTENDED_QA] " + json.dumps(results, ensure_ascii=False), flush=True)

STARTUP_SMOKE = startup_smoke_test()
if os.getenv("RUN_EXTENDED_QA", "").strip() == "1":
    threading.Thread(target=background_extended_qa, daemon=True).start()
else:
    threading.Thread(target=background_external_selftest, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
