import firebase_admin
from firebase_admin import credentials, firestore

firebase_key = credentials.Certificate(r"R:\Ashubhai's docs\Adhyapaksaathi\firebase_json_for_adhyapaksaathi.json")

if not firebase_admin._apps:
    firebase_admin.initialize_app(credential=firebase_key)

db = firestore.client()