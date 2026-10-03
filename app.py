from flask import Flask, render_template, request

app = Flask(__name__)

@app.route("/")
@app.route("/home")
def home():
    return render_template("index.html")

@app.route("/test")
def test():
    return render_template("test.html")

@app.route("/mocktest")
def mocktest():
    return render_template("mocktest.html")

@app.route("/news")
def news():
    return render_template("news.html")

@app.route("/materials")
def materials():
    return render_template("materials.html")

@app.route("/about")
def about():
    return render_template("about.html")

# @app.route("/premium")
# def premium():
#     return render_template("premimum.html")

@app.route("/contact", methods=["GET","POST"])
def contact():
    if request.method == "POST":
    # Get data from form
        user_name = request.form.get('name')
        user_email = request.form.get('email')
        user_message = request.form.get('message')
        
        # Print data for testing
        print("--- New form submission ---")
        print(f"Name: {user_name}")
        print(f"Email: {user_email}")
        print(f"Message: {user_message}")

        return "Form Submited Successfully!" 
    return render_template("contact.html")

if __name__=="__main__":
    app.run(debug=True)