import csv
import requests
from io import StringIO
from flask import Blueprint, jsonify, request

test_api = Blueprint('test_api', __name__)

SHEET_CSV_URL =  "https://docs.google.com/spreadsheets/d/e/2PACX-1vS1iMp8oZ-gM1PlKf2-na0-_9DHw1vEf7VykllRqpvsfabforbC3v97JOFwwlwLBLEIAjHRqiRiHhXM/pub?output=csv"

# # Server ki memory jahan SAARE questions (answers ke sath) chhup kar rahenge
SERVER_DATABASE = []

def fetch_data_from_google():
    """This function gets data from the Google Sheet and saves it on the server."""
    global SERVER_DATABASE
    
    try:
        print("Fetching questions from Google Sheet...")
        response = requests.get(SHEET_CSV_URL)
        response.encoding = 'utf-8'
        
        # Read the CSV in dictionary format (like JSON)
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
        print(f"✅ Total {len(SERVER_DATABASE)} questions loaded successfully!")
    
    except Exception as e:
        print("❌ Error loading from Google Sheets:", e)
        
# # The database will load the first time someone runs this file.
fetch_data_from_google()

# API ROUTE : Frontend Fetch() request will come here
@test_api.route("/api/get-questions", methods=['GET'])
def get_safe_questions():
    # If the database is empty for any reason, load the data again from the internet.
    if len(SERVER_DATABASE) == 0:
        fetch_data_from_google()
        
    safe_questions = []
    for q in SERVER_DATABASE:
        # NOTE: We are NOT adding 'ans' and 'exp' here! 🛑
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
    
    # Two things will come from the frontend:
        # 1. submissions: [{"id": 12, "selected_opt": 2}, ...]
        # 2. total_active: The total number of questions the student attempted (for example, 15)

    submissions = user_data.get('submissions', [])
    total_active = user_data.get('total_active', 0)
    
    correct = 0
    wrong = 0
    review_data = []
    
    # We create a dictionary to find the Question ID in the database.
    db_dict = {q["id"]: q for q in SERVER_DATABASE}
    
    for item in submissions:
        q_id = item.get("id")
        selected_opt = item.get("selected_opt")
        
        # If student select option
        if selected_opt is not None and q_id in db_dict:
            real_q = db_dict[q_id]
            is_correct = (selected_opt == real_q["ans"])
            
            if is_correct:
                correct +=1
            else:
                wrong +=1 
            
            # Prepare for review screen
            review_data.append({
                "question": real_q["q"],
                "user_ans_text": real_q["opts"][selected_opt],
                "correct_ans_text": real_q["opts"][real_q["ans"]],
                "is_correct": is_correct
            })
    
    skipped = total_active - (correct + wrong)
    accuracy = round((correct/total_active)*100) if total_active>0 else 0
    
    # Send the complete scorecard back to the frontend
    return jsonify({
        "correct": correct,
        "wrong": wrong,
        "skipped": skipped,
        "accuracy": accuracy,
        "review_data": review_data
    })