from flask import Blueprint, redirect, render_template, jsonify, request
from firebase_admin import firestore

mocktest_api = Blueprint('mocktest_api', __name__)

db = firestore.client()

@mocktest_api.route('/api/test-sync', methods=['GET'])
def test_syns():
    return jsonify({"status":"success", "message":"Mocktest backend is ready!"})

@mocktest_api.route('/api/sync-questions', methods=['POST'])
def sync_questions():
    try:
        data = request.get_json()
        questions_list = data.get('questions', [])
        
        for q in questions_list:
            doc_id = str(q['id'])
            db.collection('exam_questions').document(doc_id).set(q)
            
        return jsonify({"status":"success", "message":f"{len(questions_list)} questions saved successfully!"})
                       
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500
    
    
@mocktest_api.route('/api/get-questions', methods=['GET'])
def get_questions():
    try:
        # The website will tell us which test it needs (for example, Test 1 or Test 2).
        test_id = request.args.get('testId')
        
        # Get all Questions from firebase
        docs = db.collection('exam_questions').stream()
        
        valid_questions = []
        for doc in docs:
            q = doc.to_dict()
            # Add only the questions whose testNumber matches our test_id to the list
            if str(q.get('testNumber')) == str(test_id):
                valid_questions.append(q)
                
        # Send the questions back to the website in JSON(list) format
        return jsonify(valid_questions)
    
    except Exception as e:
        return jsonify({"error":str(e)}), 500