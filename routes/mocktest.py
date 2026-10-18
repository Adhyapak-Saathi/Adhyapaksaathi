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
        test_id_str = str(request.args.get('testId', '1')).strip()
        leaderboard_data = []
        
        # 1. Pehle TEXT (String) wala ID dhoondhega (Jaise: "1")
        docs_str = db.collection('student_scores').where('test_id', '==', test_id_str).stream()
        for doc in docs_str:
            d = doc.to_dict()
            leaderboard_data.append({"name": d.get('student_name', 'Unknown'), "score": d.get('score', 0)})
            
        # 2. Phir NUMBER wala ID dhoondhega (Jaise: 1)
        if test_id_str.isdigit():
            test_id_num = int(test_id_str)
            docs_num = db.collection('student_scores').where('test_id', '==', test_id_num).stream()
            for doc in docs_num:
                d = doc.to_dict()
                # Duplicate entries se bachne ke liye check karega
                if not any(entry['name'] == d.get('student_name', 'Unknown') and entry['score'] == d.get('score', 0) for entry in leaderboard_data):
                    leaderboard_data.append({"name": d.get('student_name', 'Unknown'), "score": d.get('score', 0)})
        
        # Sabko Highest Score ke hisaab se sort kar dega
        leaderboard_data = sorted(leaderboard_data, key=lambda x: x['score'], reverse=True)
        
        return jsonify({"leaderboard": leaderboard_data})
    
    except Exception as e:
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
    
# Route 7: Securely verify Razorpay Payment and Unlock Test
@mocktest_api.route("/api/verify-payment", methods = ['POST'])
def verify_payment():
    try:
        # Parse JSON data from frontend
        data = request.get_json()
        
        # 1. Extract payment details and user ID
        payment_id = data.get("razorpay_payment_id")
        order_id = data.get("razorpay_order_id")
        signature = data.get("razorpay_signature")
        test_id = str(data.get("test_id", '')).strip()
        user_uid = str(data.get('user_uid', '')).strip()
        
        if not user_uid:
            return jsonify({"status":"error", "message":"User ID missing!"}), 400
        
        # 2. Verify Signature with Razorpay
        params_dict = {
            'razorpay_order_id' : order_id,
            'razorpay_payment_id' : payment_id,
            'razorpay_signature' : signature
        }
        
        # This will raise an error if the payment is fake
        razorpay_client.utility.verify_payment_signature(params_dict)
        
        # 3. Verification PASS! Unlock test in Firebase from the backend
        db.collection('users').document(user_uid).set({
            f'paid_test{test_id}_new_v2' : True
        }, merge=True)
        
        return jsonify({"status":"success", "message":"Payment verified and test unlocked!"})
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"status":"error", "message":"Payment verification failed!"})
    
    except Exception as e:
        return jsonify({"status":"error", "message": str(e)}), 500