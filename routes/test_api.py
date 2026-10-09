import csv
import requests
import json  
import os    
from io import StringIO
from flask import Blueprint, jsonify, request
from firebase_admin import firestore
from routes.firebase_config import db

test_api = Blueprint('test_api', __name__)

SHEET_CSV_URL =  "https://docs.google.com/spreadsheets/d/e/2PACX-1vS1iMp8oZ-gM1PlKf2-na0-_9DHw1vEf7VykllRqpvsfabforbC3v97JOFwwlwLBLEIAjHRqiRiHhXM/pub?output=csv"
CACHE_FILE = "questions_cache.json"  # Is file mein hamara data lock/save hoga

# Server ki memory jahan SAARE questions (answers ke sath) chhup kar rahenge
SERVER_DATABASE = []

def fetch_data_from_google():
    """This function gets data from Google Sheet, saves it in memory, AND creates a JSON file backup."""
    global SERVER_DATABASE
    
    try:
        print("Fetching questions from Google Sheet...")
        response = requests.get(SHEET_CSV_URL)
        response.encoding = 'utf-8'
        
        # Read the CSV in dictionary format
        csv_reader = csv.DictReader(StringIO(response.text))
        
        temp_db = []
        for idx, row in enumerate(csv_reader):
            if row.get('Question') and row.get('Question').strip() != '':
                ans_str = str(row.get('Ans', '')).strip()
                ans_val = int(ans_str) if ans_str.isdigit() else 0
            
                temp_db.append({
                    "id": idx,
                    "q": row.get('Question'),
                    "opts": [row.get('OptA'), row.get('OptB'), row.get('OptC'), row.get('OptD')],
                    "ans": ans_val,
                    "topic": row.get('Topic', 'સામાન્ય જ્ઞાન'),
                    "exp": row.get('Exp', 'કોઈ સમજૂતી ઉપલબ્ધ નથી.')
                })
            
        SERVER_DATABASE = temp_db
        
        # 🚀 NYA LOGIC: Data ko fast JSON file mein save (cache) kar lo
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(SERVER_DATABASE, f, ensure_ascii=False, indent=4)
            
        print(f"✅ Total {len(SERVER_DATABASE)} questions safely saved in JSON Cache!")
        return True
    
    except Exception as e:
        print("❌ Error loading from Google Sheets:", e)
        return False

def load_database():
    """Server start hote hi ye function check karta hai ki fast Cache use karna hai ya Google Sheet"""
    global SERVER_DATABASE
    
    # Agar cache file maujood hai, to wahan se turant padh lo (No Internet required)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                SERVER_DATABASE = json.load(f)
            print(f"⚡ FAST BOOT: Loaded {len(SERVER_DATABASE)} questions instantly from JSON Cache!")
        except Exception as e:
            print("Cache error, falling back to Google Sheets...", e)
            fetch_data_from_google()
    else:
        # Pehli baar server start hone par Google se layega
        fetch_data_from_google()

# 🚀 Server start hote hi Cache se data load hoga
load_database()

# 🔄 NAYA ROUTE: Admin (Aap) jab Google Sheet mein naya sawal daalein, to ise run karein
@test_api.route("/api/refresh-cache", methods=['GET'])
def refresh_cache():
    success = fetch_data_from_google()
    if success:
        return jsonify({"message": "Success! System synced with Google Sheets and Cache Updated."})
    return jsonify({"error": "Failed to update Cache."}), 500

# API ROUTE : Frontend Fetch() request will come here
@test_api.route("/api/get-questions", methods=['GET'])
def get_safe_questions():
    if len(SERVER_DATABASE) == 0:
        load_database()
        
    safe_questions = []
    for q in SERVER_DATABASE:
        safe_questions.append({
            "id": q["id"],
            "q": q["q"],
            "opts": q["opts"],
            "topic": q["topic"]
        })
        
    return jsonify(safe_questions)

# This will be checked here when the test is submitted.
@test_api.route('/api/submit-test', methods=['POST'])
def submit_test():
    user_data = request.json
    submissions = user_data.get('submissions', [])
    total_active = user_data.get('total_active', 0)
    
    correct = 0
    wrong = 0
    review_data = []
    
    db_dict = {q["id"]: q for q in SERVER_DATABASE}
    
    for item in submissions:
        q_id = item.get("id")
        selected_opt = item.get("selected_opt")
        
        if selected_opt is not None and q_id in db_dict:
            real_q = db_dict[q_id]
            is_correct = (selected_opt == real_q["ans"])
            
            if is_correct:
                correct +=1
            else:
                wrong +=1 
            
            review_data.append({
                "question": real_q["q"],
                "user_ans_text": real_q["opts"][selected_opt],
                "correct_ans_text": real_q["opts"][real_q["ans"]],
                "is_correct": is_correct
            })
    
    skipped = total_active - (correct + wrong)
    accuracy = round((correct/total_active)*100) if total_active>0 else 0
    
    try:
        db.collection('student_scores').add({
            'total_attempted' : total_active,
            'correct_answers' : correct,
            'accuracy_percentage' : accuracy,
            'status' : 'Completed'
        })
        
        print("✅ Student score successfully saved to Firebase!")
    
    except Exception as e:
        print("❌ Error:", e)
    
    return jsonify({
        "correct": correct,
        "wrong": wrong,
        "skipped": skipped,
        "accuracy": accuracy,
        "review_data": review_data
    })
    
    
@test_api.route("/api/submit-score", methods=['POST'])
def submit_score():
    try:
        # Get JSON data from frontend
        data = request.get_json()
        student_name = data.get("name")
        score = data.get("score")
        test_id = data.get('testID')
        
        # Save it to firebase
        doc_ref = db.collection('student_scores').document()
        doc_ref.set({
            'student_name': student_name,
            'score' : score,
            'test_id' : test_id,
            'timestamp' : firestore.SERVER_TIMESTAMP
        })
        
        # Success message
        return jsonify({"message":"Score saved successfully!", "status":"success"}), 200
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500