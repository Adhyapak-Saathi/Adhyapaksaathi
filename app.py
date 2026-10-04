from flask import Flask
from routes.page_routes import page_routes

app = Flask(__name__)

app.register_blueprint(page_routes)

if __name__=="__main__":
    app.run(debug=True)