from flask import Blueprint, redirect, url_for, render_template, request

# Create Blueprint using pages name
page_routes = Blueprint('pages', __name__)

@page_routes.route("/")
@page_routes.route("/home")
def home():
    return render_template("index.html")

@page_routes.route("/test")
def test():
    return render_template("test.html")

@page_routes.route("/mocktest")
def mocktest():
    return render_template("mocktest.html")

@page_routes.route("/news")
def news():
    return render_template("news.html")

@page_routes.route("/materials")
def materials():
    return render_template("materials.html")

@page_routes.route("/about")
def about():
    return render_template("about.html")

@page_routes.route("/contact", methods=["GET","POST"])
def contact():
    if request.method == "POST":
        user_name = request.form.get('name')
        user_email = request.form.get('email')
        user_message = request.form.get('message')
        
        # Print data for testing 
        print("--- New form submission ---")
        print(f"Name: {user_name}")
        print(f"Email: {user_email}")
        print(f"Message: {user_message}")
        
        return "Form Submited Successfully"
    return render_template("contact.html")