import os, json, re, sqlite3, base64
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

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, 'students.db')
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024

FIELDS = [
('roll_number','Roll Number'),('gr_number','GR Number'),('cts_number','CTS Number'),
('abha_number','ABHA Number'),('pen_number','PEN Number'),('apaar_number','APAAR Number'),
('aadhaar_number','Aadhaar Number'),('aadhaar_name','Aadhaar Name'),
('aadhaar_according_name','Aadhaar મુજબ નામ'),('student_full_name','વિદ્યાર્થીનું પૂરું નામ'),
('mother_name','માતાનું નામ'),('father_name','પિતાનું નામ'),('phone_number','Phone Number'),
('address','સરનામું'),('admission_date','Admission Date'),('dob','DOB'),('sub_caste','પેટા જાતિ'),
('bank_account_name','Bank Account Name'),('bank_account_number','Bank Account Number'),
('bank_branch','Bank Branch'),('ifsc_code','IFSC Code'),('bank_name','Bank Name')]

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    cols = ','.join([f"{k} TEXT DEFAULT ''" for k,_ in FIELDS])
    with db() as con:
        con.execute(f"CREATE TABLE IF NOT EXISTS students (id INTEGER PRIMARY KEY AUTOINCREMENT,{cols},verification_status TEXT DEFAULT '',remarks TEXT DEFAULT '',last_updated TEXT DEFAULT '')")
init_db()

def norm(v): return re.sub(r'\s+',' ',str(v or '').strip()).lower()
def only_digits(v): return re.sub(r'\D','',str(v or ''))

def sanitize(data):
    out={k:str(data.get(k,'') or '').strip() for k,_ in FIELDS}
    a=only_digits(out['aadhaar_number'])
    out['aadhaar_number']=a if (not a or len(a)==12) else ''
    p=only_digits(out['phone_number'])
    out['phone_number']=p if (not p or 10 <= len(p) <= 13) else ''
    ifsc=out['ifsc_code'].replace(' ','').upper()
    out['ifsc_code']=ifsc if (not ifsc or re.fullmatch(r'[A-Z]{4}0[A-Z0-9]{6}',ifsc)) else ''
    return out

def ai_extract(image_bytes,mime_type,doc_type):
    key=(os.getenv('GEMINI_API_KEY','').strip() or
         os.getenv('GEMINI_KEY','').strip() or
         os.getenv('GOOGLE_API_KEY','').strip() or
         os.getenv('GOOGLE_GEMINI_API_KEY','').strip())
    model=os.getenv('GEMINI_MODEL','gemini-2.5-flash').strip()
    if not key: raise RuntimeError('GEMINI_API_KEY સેટ નથી.')
    prompt=f'''Read ONE photographed school/student document. It may be handwritten or printed in Gujarati/English/Hindi.
User-selected document type: {doc_type}
STRICT RULES:
- Never guess. If unclear/absent, return empty string.
- CTS Number = SSA/CTS AadhaarUID in this workflow.
- CTS Number is NOT the 12-digit Aadhaar Card Number.
- Aadhaar Number means only the separate 12-digit Aadhaar card number.
- ABHA, PEN and APAAR are separate identifiers.
- Never infer caste/sub-caste from surname. Never infer bank details.
- Dates: DD-MM-YYYY only if complete date is clearly visible.
- Doubtful fields: blank + add key to uncertain_fields.
Return JSON ONLY exactly in this shape:
{{"document_type":"","data":{{"roll_number":"","gr_number":"","cts_number":"","abha_number":"","pen_number":"","apaar_number":"","aadhaar_number":"","aadhaar_name":"","aadhaar_according_name":"","student_full_name":"","mother_name":"","father_name":"","phone_number":"","address":"","admission_date":"","dob":"","sub_caste":"","bank_account_name":"","bank_account_number":"","bank_branch":"","ifsc_code":"","bank_name":""}},"uncertain_fields":[]}}'''
    url=f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    payload={'contents':[{'parts':[{'text':prompt},{'inline_data':{'mime_type':mime_type,'data':base64.b64encode(image_bytes).decode('ascii')}}]}],'generationConfig':{'temperature':0}}
    r=requests.post(url,headers={'x-goog-api-key':key,'Content-Type':'application/json'},json=payload,timeout=90)
    if r.status_code>=300: raise RuntimeError(f'AI error {r.status_code}: {r.text[:300]}')
    obj=r.json(); parts=obj.get('candidates',[{}])[0].get('content',{}).get('parts',[])
    txt=''.join(p.get('text','') for p in parts).strip()
    txt=re.sub(r'^\`\`\`(?:json)?\s*','',txt,flags=re.I); txt=re.sub(r'\s*\`\`\`$','',txt)
    result=json.loads(txt)
    result['data']=sanitize(result.get('data',{})); result['uncertain_fields']=result.get('uncertain_fields') or []
    return result

def sheet_client():
    if gspread is None: return None
    raw=os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON','').strip(); sid=os.getenv('GOOGLE_SHEET_ID','').strip()
    if not raw or not sid: return None
    try: info=json.loads(raw)
    except Exception: info=json.loads(base64.b64decode(raw).decode('utf-8'))
    creds=Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return gspread.authorize(creds).open_by_key(sid)

def sync_sheet(student):
    ss=sheet_client()
    if ss is None: return False
    tab=os.getenv('GOOGLE_SHEET_TAB','Student_Master')
    try: ws=ss.worksheet(tab)
    except Exception: ws=ss.add_worksheet(title=tab,rows=1000,cols=40)
    headers=[h for _,h in FIELDS]+['Verification Status','Remarks','Last Updated']
    if ws.row_values(1)!=headers:
        ws.clear(); ws.append_row(headers)
    rows=ws.get_all_values(); hmap={h:i for i,h in enumerate(headers)}
    target=None
    for idx,row in enumerate(rows[1:],start=2):
        def cell(h):
            j=hmap[h]; return row[j] if j<len(row) else ''
        if student.get('pen_number') and cell('PEN Number')==student.get('pen_number'): target=idx; break
        if student.get('cts_number') and cell('CTS Number')==student.get('cts_number'): target=idx; break
        if student.get('gr_number') and cell('GR Number')==student.get('gr_number'): target=idx; break
        if student.get('aadhaar_number') and cell('Aadhaar Number')==student.get('aadhaar_number'): target=idx; break
    vals=[student.get(k,'') for k,_ in FIELDS]+[student.get('verification_status',''),student.get('remarks',''),student.get('last_updated','')]
    if target: ws.update(f'A{target}',[vals])
    else: ws.append_row(vals,value_input_option='USER_ENTERED')
    return True

@app.route('/')
def index(): return render_template('index.html',fields=FIELDS)

@app.route('/api/search')
def search():
    q=norm(request.args.get('q',''))
    if not q: return jsonify([])
    with db() as con: rows=con.execute('SELECT * FROM students ORDER BY id DESC').fetchall()
    out=[]
    for rr in rows:
        d=dict(rr); hay=' | '.join(norm(d.get(k)) for k in ['gr_number','pen_number','cts_number','aadhaar_number','student_full_name','aadhaar_name','father_name','mother_name'])
        if q in hay:
            out.append({k:d.get(k,'') for k in ['id','roll_number','gr_number','pen_number','cts_number','student_full_name','aadhaar_name']})
        if len(out)>=30: break
    return jsonify(out)

@app.route('/api/student/<int:sid>')
def student(sid):
    with db() as con: r=con.execute('SELECT * FROM students WHERE id=?',(sid,)).fetchone()
    return jsonify(dict(r)) if r else (jsonify({'error':'Not found'}),404)

@app.route('/api/extract',methods=['POST'])
def extract():
    f=request.files.get('image'); doc=request.form.get('doc_type','AUTO')
    if not f: return jsonify({'error':'Image missing'}),400
    try: return jsonify(ai_extract(f.read(),f.mimetype or 'image/jpeg',doc))
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/save/<int:sid>',methods=['POST'])
def save(sid):
    payload=request.get_json(force=True); incoming=sanitize(payload.get('data',{})); uncertain=payload.get('uncertain_fields') or []
    with db() as con:
        r=con.execute('SELECT * FROM students WHERE id=?',(sid,)).fetchone()
        if not r: return jsonify({'error':'Student not found'}),404
        current=dict(r); conflicts=[]; updates={}
        for k,_ in FIELDS:
            new=incoming.get(k,''); old=str(current.get(k,'') or '').strip()
            if not new: continue
            if not old: updates[k]=new
            elif norm(old).replace(' ','')!=norm(new).replace(' ',''):
                conflicts.append({'field':k,'existing':old,'scanned':new})
        status='VERIFY' if conflicts or uncertain else 'OK'
        remarks=[]
        if current.get('remarks'): remarks.append(current['remarks'])
        if uncertain: remarks.append('Uncertain: '+', '.join(uncertain))
        if conflicts: remarks.append('Conflicts: '+' | '.join(f'{c["field"]}: existing="{c["existing"]}" scanned="{c["scanned"]}"' for c in conflicts))
        updates.update(verification_status=status,remarks=' || '.join(remarks),last_updated=datetime.now().isoformat(timespec='seconds'))
        sets=', '.join(f'{k}=?' for k in updates); con.execute(f'UPDATE students SET {sets} WHERE id=?',list(updates.values())+[sid]); con.commit()
        fresh=dict(con.execute('SELECT * FROM students WHERE id=?',(sid,)).fetchone())
    synced=False; serr=''
    try: synced=sync_sheet(fresh)
    except Exception as e: serr=str(e)
    return jsonify({'ok':True,'status':status,'conflicts':conflicts,'sheet_synced':synced,'sheet_error':serr})

@app.route('/api/import',methods=['POST'])
def import_master():
    f=request.files.get('file')
    if not f: return jsonify({'error':'File missing'}),400
    raw=f.read(); name=(f.filename or '').lower()
    try: df=pd.read_csv(BytesIO(raw),dtype=str).fillna('') if name.endswith('.csv') else pd.read_excel(BytesIO(raw),dtype=str).fillna('')
    except Exception as e: return jsonify({'error':f'File read failed: {e}'}),400
    aliases={
      'roll_number':['roll number','roll no','rollno','roll'], 'gr_number':['gr number','grno','gr no'],
      'cts_number':['cts number','cts no','aadhaaruid','aadhaar uid'], 'abha_number':['abha number','abha no'],
      'pen_number':['pen number','student pen','pen no'], 'apaar_number':['apaar number','apaar id','apaar no'],
      'aadhaar_number':['aadhaar number','aadhaar no','aadhar number'], 'aadhaar_name':['name as per aadhaar','aadhaar name','aadhar name'],
      'aadhaar_according_name':['aadhaar મુજબ નામ','as per aadhaar name'], 'student_full_name':['student full name','studentname','student name','name'],
      'mother_name':['mothername','mother name'], 'father_name':['fathername','father name'], 'phone_number':['phone number','mobile','mobile number','phone'],
      'address':['address','સરનામું'], 'admission_date':['doa','admission date','date of admission'], 'dob':['dob','date of birth'],
      'sub_caste':['sub caste','subcaste','પેટા જાતિ'], 'bank_account_name':['bank account name','account holder name'],
      'bank_account_number':['bank account number','account number'], 'bank_branch':['bank branch','branch'], 'ifsc_code':['ifsc','ifsc code'], 'bank_name':['bank name']}
    cmap={norm(c):c for c in df.columns}; chosen={}
    for key,opts in aliases.items():
        for a in opts:
            if norm(a) in cmap: chosen[key]=cmap[norm(a)]; break
    inserted=0
    with db() as con:
        keys=[k for k,_ in FIELDS]
        for _,row in df.iterrows():
            d=sanitize({k:(str(row[chosen[k]]).strip() if k in chosen else '') for k in keys})
            if not any(d.values()): continue
            con.execute(f"INSERT INTO students ({','.join(keys)},verification_status,last_updated) VALUES ({','.join(['?']*(len(keys)+2))})",[d[k] for k in keys]+['IMPORTED',datetime.now().isoformat(timespec='seconds')]); inserted+=1
        con.commit()
    return jsonify({'ok':True,'inserted':inserted,'mapped_columns':chosen})

@app.route('/api/export.xlsx')
def export_xlsx():
    with db() as con: df=pd.read_sql_query('SELECT * FROM students ORDER BY id',con)
    out=BytesIO(); df.to_excel(out,index=False); out.seek(0)
    return send_file(out,as_attachment=True,download_name='student_master_export.xlsx',mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=True)


@app.route('/api/health')
def health():
    return jsonify({
        'ok': True,
        'gemini_key_set': bool(
            os.getenv('GEMINI_API_KEY','').strip() or
            os.getenv('GEMINI_KEY','').strip() or
            os.getenv('GOOGLE_API_KEY','').strip() or
            os.getenv('GOOGLE_GEMINI_API_KEY','').strip()
        ),
        'gemini_model': os.getenv('GEMINI_MODEL','gemini-2.5-flash'),
        'google_sheet_id_set': bool(os.getenv('GOOGLE_SHEET_ID','').strip()),
        'google_service_account_set': bool(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON','').strip())
    })


@app.route('/api/selftest')
def selftest():
    checks = {}
    try:
        with db() as con:
            con.execute("SELECT 1").fetchone()
        checks['database'] = True
    except Exception as e:
        checks['database'] = False
        checks['database_error'] = str(e)

    checks['gemini_key_set'] = bool(
        os.getenv('GEMINI_API_KEY','').strip() or
        os.getenv('GEMINI_KEY','').strip() or
        os.getenv('GOOGLE_API_KEY','').strip() or
        os.getenv('GOOGLE_GEMINI_API_KEY','').strip()
    )
    checks['google_sheet_configured'] = bool(
        os.getenv('GOOGLE_SHEET_ID','').strip() and
        os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON','').strip()
    )
    checks['gallery_supported'] = True
    checks['cts_mapping_rule'] = 'CTS Number = SSA AadhaarUID; Aadhaar Number is separate 12-digit card number'
    checks['ok'] = bool(checks['database'])
    return jsonify(checks)
