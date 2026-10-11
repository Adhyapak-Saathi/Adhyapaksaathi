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