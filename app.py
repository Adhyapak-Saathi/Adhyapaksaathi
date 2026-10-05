from flask import Flask
from routes.page_routes import page_routes
from routes.test_api import test_api

app = Flask(__name__)

app.register_blueprint(page_routes)
app.register_blueprint(test_api)

if __name__=="__main__":
    app.run(debug=True)