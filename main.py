from flask import Flask
import requests

app = Flask(__name__)

@app.route("/")
def home():
    r = requests.get("https://example.com", timeout=10)
    return f"Status: {r.status_code}"