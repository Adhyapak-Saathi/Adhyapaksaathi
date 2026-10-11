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
            # Smart ID maker:
            exam_name = q.get('examName', 'Unknown')
            test_num = str(q.get('testNumber', '1'))
            q_id = str(q.get('id', '0'))
            doc_id = str(q['id'])
            db.collection('exam_questions').document(doc_id).set(q)
            
        exam_tag = questions_list[0].get('examNmae', '') if questions_list else ''
        return jsonify({"status":"success", "message":f"{len(questions_list)} questions saved successfully!"})
                       
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500
    
    
@mocktest_api.route('/api/get-mock-questions', methods=['GET'])
def get_questions():
    try:
        test_id = request.args.get('testId')
        exam_name = request.args.get('examName')
        
        docs = db.collection('exam_questions').stream()
        valid_questions = []
        
        for doc in docs:
            q = doc.to_dict()
            # Yahan hum check kar rahe hain
            if str(q.get('testNumber')) == str(test_id) and str(q.get('examName')) == str(exam_name):
                valid_questions.append(q)
                
        return jsonify(valid_questions)
    
    except Exception as e:
        return jsonify({"error":str(e)}), 500
    
@mocktest_api.route('/api/get-leaderboard', methods=['GET'])
def get_leaderboard():
    try:
        # Get testId from website
        test_id = request.args.get('testId')
        
        docs = db.collection('student_scores').where('test_id', '==', str(test_id)).stream()
        
        leaderboard_data = []
        
        for doc in docs:
            score_data = doc.to_dict()
            leaderboard_data.append({
                "name": score_data.get('student_name', 'Unknown'),
                "score" : score_data.get('score', 0)
            })
        
        leaderboard_data = sorted(leaderboard_data, key=lambda x: x['score'], reverse=True)
        
        return jsonify({"leaderboard": leaderboard_data})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500