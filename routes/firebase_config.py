import os
import firebase_admin
from firebase_admin import credentials, firestore


local_path = r"R:\Ashubhai's docs\Adhyapaksaathi\firebase_json_for_adhyapaksaathi.json"
render_path = "/etc/secrets/firebase_json_for_adhyapaksaathi.json"

if os.path.exists(render_path):
    cert_path = render_path
    print("Running on Render cloud!")
else:
    cert_path = local_path
    print("Runnin on local computer")
    
firebase_key = credentials.Certificate(cert_path)

if not firebase_admin._apps:
    firebase_admin.initialize_app(credential=firebase_key)

db = firestore.client()