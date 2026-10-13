from flask import Blueprint, redirect, render_template, jsonify, request
from firebase_admin import firestore
import razorpay
import os
from dotenv import load_dotenv
load_dotenv()

# Create a Blueprint for mocktest API routes
mocktest_api = Blueprint('mocktest_api', __name__)

# Connect to the Firebase database
db = firestore.client()

# Route 1: Simple test to check if the backend is running
@mocktest_api.route('/api/test-sync', methods=['GET'])
def test_syns():
    return jsonify({"status":"success", "message":"Mocktest backend is ready!"})

# Route 2: Save new questions from Google Sheets to Firebase
@mocktest_api.route('/api/sync-questions', methods=['POST'])
def sync_questions():
    try:
        # Get data sent from the frontend
        data = request.get_json()
        questions_list = data.get('questions', [])
        
        # Loop through each question and save it to Firebase
        for q in questions_list:
            
            # SMART ID MAKER: Combine Exam Name, Test Number, and Question ID
            exam_name = str(q.get('examName', 'Unknown')).strip().replace(" ", "_")
            test_num = str(q.get('testNumber', '1')).strip()
            q_id = str(q.get('id', '0')).strip()
            
            # Naya Unique ID banega (Example: 'GSET_2_1')
            unique_doc_id = f"{exam_name}_{test_num}_{q_id}"
            
            # Save to Firebase with the unique ID
            db.collection('exam_questions').document(unique_doc_id).set(q)
            
        return jsonify({"status":"success", "message":f"{len(questions_list)} questions saved successfully!"})
                       
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500
    
# Route 3: Fetch questions for the mocktest based on Exam Name and Test ID
@mocktest_api.route('/api/get-mock-questions', methods=['GET'])
def get_questions():
    try:
        # Remove extra spaces and convert to lowercase for the requested data
        test_id = str(request.args.get('testId', '')).strip()
        exam_name = str(request.args.get('examName', '')).strip().lower()
        
        # Fetch all questions from Firebase
        docs = db.collection('exam_questions').stream()
        valid_questions = []
        
        # Filter questions (Smart Matching)
        for doc in docs:
            q = doc.to_dict()
            
            # Clean the database data to ensure an exact match
            db_test_num = str(q.get('testNumber', '')).strip()
            db_exam_name = str(q.get('examName', '')).strip().lower()
            
            # Add to the list if both test ID and exam name match perfectly
            if exam_name == '' or exam_name == 'none':
                if db_test_num == test_id:
                    valid_questions.append(q)
            
            else:
                if db_test_num == test_id and db_exam_name == exam_name:
                    valid_questions.append(q)
                
        # Send the filtered questions back to the website
        return jsonify(valid_questions)
    
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# Route 4: Fetch and sort the top scores for the Live Leaderboard
@mocktest_api.route('/api/get-leaderboard', methods=['GET'])
def get_leaderboard():
    try:
        test_id = request.args.get('testId')
        print(f"====== LEADERBOARD MANGWA RAHA HAI FOR TEST: {test_id} ======")
        
        # SMART QUERY: Firebase mein Number (1) aur String ('1') dono ko ek sath dhoondhega
        test_id_str = str(test_id)
        search_ids = [test_id_str]
        if test_id_str.isdigit():
            search_ids.append(int(test_id_str))
            
        docs = db.collection('student_scores').where('test_id', 'in', search_ids).stream()
        leaderboard_data = []
        
        for doc in docs:
            score_data = doc.to_dict()
            leaderboard_data.append({
                "name": score_data.get('student_name', 'Unknown'),
                "score" : score_data.get('score', 0)
            })
        
        print(f"====== FIREBASE SE {len(leaderboard_data)} SCORES MILE ======")
        leaderboard_data = sorted(leaderboard_data, key=lambda x: x['score'], reverse=True)
        return jsonify({"leaderboard": leaderboard_data})
    
    except Exception as e:
        print(f"!!! LEADERBOARD ERROR !!!: {str(e)}")
        return jsonify({"error": str(e)}), 500
    
# RAZAORPAY

razorpay_client = razorpay.Client(auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")))

# Route 5: Create a new Razorpay payment order
@mocktest_api.route('/api/create-order', methods=['POST'])
def create_order():
    try:
        # Amount must be in paise 
        amount = 39*100
        
        # Order details required by Razorpay
        data = {
            "amount" : amount,
            "currency": "INR",
            "receipt":"order_rcptid_11"
        }
        
        # Tell Razorpay to generate a new order
        payment_order = razorpay_client.order.create(data=data)
        
        # Send the order details to the website so the payment popup can open
        return jsonify(payment_order)
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
# Route 6: Save student score to Firebase Leaderboard
@mocktest_api.route('/api/submit-score', methods=['POST'])
def submit_score():
    try:
        data = request.get_json(force=True)
        print("====== NAYA SCORE AAYA ======", data)
        
        name = str(data.get('name', 'Unknown')).strip()
        score = float(data.get('score', 0))
        
        # BULLETPROOF LOGIC: test_id kabhi null nahi jayega
        raw_test_id = data.get('testId') or data.get('test_id')
        if raw_test_id is None or str(raw_test_id).strip() == '' or str(raw_test_id).strip() == 'null':
            test_id = "1"  # Default test 1 maan lega
        else:
            test_id = str(raw_test_id).strip()
        
        db.collection('student_scores').add({
            'student_name': name,
            'score': score,
            'test_id': test_id
        })
        
        print(f"====== SCORE SAVE HO GAYA! Test ID: {test_id} ======")
        return jsonify({"status": "success", "message": "Score saved!"})
    
    except Exception as e:
        print(f"!!! SCORE SAVE ERROR !!!: {str(e)}")
        return jsonify({"error": str(e)}), 500