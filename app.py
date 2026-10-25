import os
import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200_000

PISTON_BASE = os.getenv("PISTON_API_URL", "https://emkc.org/api/v2/piston").rstrip("/")
PISTON_API_KEY = os.getenv("PISTON_API_KEY", "")

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/health")
def health():
    return jsonify({"ok": True, "app": "CodeRun Studio"})

@app.get("/api/runtimes")
def runtimes():
    headers = {}
    if PISTON_API_KEY:
        headers["Authorization"] = PISTON_API_KEY
    try:
        response = requests.get(f"{PISTON_BASE}/runtimes", headers=headers, timeout=12)
        return (response.text, response.status_code, {"Content-Type": "application/json"})
    except requests.RequestException:
        return jsonify({"error": "Code runtime service is not reachable. Configure PISTON_API_URL and, if required, PISTON_API_KEY in Render environment variables."}), 503

@app.post("/api/execute")
def execute():
    data = request.get_json(silent=True) or {}
    language = str(data.get("language", "")).lower().strip()
    code = data.get("code", "")
    stdin = data.get("stdin", "")
    allowed = {"python", "python3", "c", "c++", "cpp", "java", "php", "ruby", "go", "rust", "bash", "typescript", "kotlin", "swift", "csharp"}
    if language not in allowed:
        return jsonify({"error": "Unsupported server language."}), 400
    if not isinstance(code, str) or not code.strip() or len(code) > 100_000:
        return jsonify({"error": "Enter code (maximum 100 KB)."}), 400
    if not isinstance(stdin, str) or len(stdin) > 20_000:
        return jsonify({"error": "Input is too long."}), 400
    aliases = {"python3": "python", "cpp": "c++", "csharp": "csharp"}
    language = aliases.get(language, language)
    payload = {"language": language, "version": "*", "files": [{"name": "main.txt", "content": code}], "stdin": stdin, "run_timeout": 3000, "compile_timeout": 10000}
    headers = {"Content-Type": "application/json"}
    if PISTON_API_KEY:
        headers["Authorization"] = PISTON_API_KEY
    try:
        response = requests.post(f"{PISTON_BASE}/execute", json=payload, headers=headers, timeout=20)
        try:
            result = response.json()
        except ValueError:
            result = {"error": "Execution service returned an unexpected response."}
        return jsonify(result), response.status_code
    except requests.Timeout:
        return jsonify({"error": "Execution timed out. Try a shorter program."}), 504
    except requests.RequestException:
        return jsonify({"error": "Execution service is unavailable. Configure a working Piston-compatible API in Render."}), 503

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
