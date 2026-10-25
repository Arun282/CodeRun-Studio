import io
import os
import re
import sqlite3
import secrets
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import requests
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory, abort, session

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("RENDER", "") == "true"

ROOT = Path(os.getenv("AKHOSTING_STORAGE", "/tmp/akhosting-sites"))
ROOT.mkdir(parents=True, exist_ok=True)
PISTON_BASE = os.getenv("PISTON_API_URL", "https://emkc.org/api/v2/piston").rstrip("/")
PISTON_API_KEY = os.getenv("PISTON_API_KEY", "")
MAX_FILES = 500
MAX_UNPACKED = 30 * 1024 * 1024
DB_PATH = os.getenv("DATABASE_PATH", "/tmp/akhosting.db")
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

with db() as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, site_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, files_count INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id))")

def current_user():
    uid = session.get("user_id")
    if not uid: return None
    with db() as conn:
        row = conn.execute("SELECT id,email FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

def slugify(value):
    value = re.sub(r"[^a-z0-9-]", "-", value.lower().strip())
    value = re.sub(r"-+", "-", value).strip("-")[:40]
    return value
ALLOWED_EXTS = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".txt", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".pdf"}

@app.get("/")
def home():
    return render_template("index.html", user=current_user())

@app.get("/api/me")
def api_me():
    user = current_user()
    if not user: return jsonify({"user": None, "projects": []})
    with db() as conn:
        rows = conn.execute("SELECT id,name,slug,site_id,files_count,created_at FROM projects WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    base = request.url_root.rstrip("/")
    return jsonify({"user": user, "projects": [dict(r, url=f"{base}/p/{r['slug']}") for r in rows]})

@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 200:
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    try:
        with db() as conn:
            cur = conn.execute("INSERT INTO users(email,password_hash) VALUES(?,?)", (email, generate_password_hash(password)))
            session.clear()
            session["user_id"] = cur.lastrowid
        return jsonify({"ok": True, "email": email})
    except sqlite3.IntegrityError:
        return jsonify({"error": "An account with this email already exists. Please sign in."}), 409

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    with db() as conn:
        row = conn.execute("SELECT id,password_hash FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Email or password is incorrect."}), 401
    session.clear()
    session["user_id"] = row["id"]
    return jsonify({"ok": True, "email": email})

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/health")
def health():
    return jsonify({"ok": True, "app": "AK Hosting"})

@app.get("/api/runtimes")
def runtimes():
    headers = {"Authorization": PISTON_API_KEY} if PISTON_API_KEY else {}
    try:
        response = requests.get(f"{PISTON_BASE}/runtimes", headers=headers, timeout=12)
        return (response.text, response.status_code, {"Content-Type": "application/json"})
    except requests.RequestException:
        return jsonify({"error": "Execution service is unreachable. Configure PISTON_API_URL and optional PISTON_API_KEY."}), 503

@app.post("/api/execute")
def execute():
    data = request.get_json(silent=True) or {}
    language = str(data.get("language", "")).lower().strip()
    code, stdin = data.get("code", ""), data.get("stdin", "")
    allowed = {"python", "python3", "c", "c++", "cpp", "java", "php", "ruby", "go", "rust", "bash", "typescript", "kotlin", "swift", "csharp"}
    if language not in allowed:
        return jsonify({"error": "Unsupported server language."}), 400
    if not isinstance(code, str) or not code.strip() or len(code) > 100_000:
        return jsonify({"error": "Enter code (maximum 100 KB)."}), 400
    if not isinstance(stdin, str) or len(stdin) > 20_000:
        return jsonify({"error": "Input is too long."}), 400
    language = {"python3": "python", "cpp": "c++"}.get(language, language)
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
        return jsonify({"error": "Execution timed out."}), 504
    except requests.RequestException:
        return jsonify({"error": "Execution service unavailable. Configure a working Piston-compatible API."}), 503

def safe_extract_site(upload):
    site_id = uuid.uuid4().hex[:10]
    target = ROOT / site_id
    total = 0
    count = 0
    try:
        with zipfile.ZipFile(upload) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_FILES:
                raise ValueError("ZIP has too many files (maximum 500).")
            for info in infos:
                if info.is_dir():
                    continue
                raw = info.filename.replace("\\", "/")
                path = PurePosixPath(raw)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError("ZIP contains an unsafe file path.")
                if any(part.startswith(".") for part in path.parts):
                    continue
                if Path(path.name).suffix.lower() not in ALLOWED_EXTS:
                    continue
                total += info.file_size
                count += 1
                if total > MAX_UNPACKED:
                    raise ValueError("Unpacked website files exceed 30 MB.")
                dest = target.joinpath(*path.parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    dst.write(src.read(MAX_UNPACKED + 1))
        if not (target / "index.html").is_file():
            candidates = list(target.rglob("index.html"))
            if len(candidates) == 1:
                nested = candidates[0].parent
                temp = target / "_site_tmp"
                temp.mkdir()
                for item in nested.iterdir():
                    dest = temp / item.name
                    if item.is_dir():
                        import shutil
                        shutil.copytree(item, dest)
                    else:
                        import shutil
                        shutil.copy2(item, dest)
                import shutil
                for item in list(target.iterdir()):
                    if item != temp:
                        shutil.rmtree(item) if item.is_dir() else item.unlink()
                for item in list(temp.iterdir()):
                    item.rename(target / item.name)
                temp.rmdir()
        if not (target / "index.html").is_file():
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            raise ValueError("ZIP must contain index.html at its root (or one folder deep).")
        return site_id, count
    except zipfile.BadZipFile:
        raise ValueError("Please upload a valid .zip file.")
    except Exception:
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        raise

@app.post("/api/sites")
def upload_site():
    user = current_user()
    if not user: return jsonify({"error": "Please sign in or create an account before hosting a website."}), 401
    file = request.files.get("site_zip")
    if not file or not file.filename.lower().endswith(".zip"):
        return jsonify({"error": "Choose a ZIP file containing your static website."}), 400
    try:
        site_id, count = safe_extract_site(file.stream)
        base = request.url_root.rstrip("/")
        requested_name = str(request.form.get("site_name", "my-website")).strip()[:60]
        base_slug = slugify(request.form.get("slug", requested_name)) or "my-website"
        slug = base_slug
        with db() as conn:
            suffix = 2
            while conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                slug = f"{base_slug[:35]}-{suffix}"
                suffix += 1
            conn.execute("INSERT INTO projects(user_id,site_id,name,slug,files_count) VALUES(?,?,?,?,?)", (user["id"], site_id, requested_name or slug, slug, count))
        return jsonify({"ok": True, "site_id": site_id, "slug": slug, "files": count, "url": f"{base}/p/{slug}"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

@app.post("/api/import-github")
def import_github():
    user = current_user()
    if not user:
        return jsonify({"error": "Please sign in before importing a repository."}), 401
    data = request.get_json(silent=True) or {}
    repo_url = str(data.get("repo_url", "")).strip()
    branch = str(data.get("branch", "main")).strip()
    match = re.fullmatch(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", repo_url)
    if not match:
        return jsonify({"error": "Enter a public GitHub repository URL, for example https://github.com/owner/repo"}), 400
    owner, repo = match.group(1), match.group(2)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", branch):
        return jsonify({"error": "Invalid branch name."}), 400
    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
    try:
        response = requests.get(zip_url, timeout=20, stream=True, headers={"User-Agent": "AKHosting-Repo-Importer"})
        if response.status_code != 200:
            return jsonify({"error": "Could not download this branch. Check the repository URL, branch name, and public access."}), 400
        buf = io.BytesIO()
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 12 * 1024 * 1024:
                return jsonify({"error": "Repository ZIP is larger than 12 MB. Use a smaller static website ZIP."}), 413
            buf.write(chunk)
        buf.seek(0)
        site_id, count = safe_extract_site(buf)
        requested_name = str(data.get("site_name", repo)).strip()[:60] or repo
        base_slug = slugify(data.get("slug", requested_name)) or "github-site"
        slug = base_slug
        with db() as conn:
            suffix = 2
            while conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                slug = f"{base_slug[:35]}-{suffix}"
                suffix += 1
            conn.execute("INSERT INTO projects(user_id,site_id,name,slug,files_count) VALUES(?,?,?,?,?)",
                         (user["id"], site_id, requested_name, slug, count))
        base = request.url_root.rstrip("/")
        return jsonify({"ok": True, "site_id": site_id, "slug": slug, "files": count,
                        "url": f"{base}/p/{slug}", "source": "GitHub"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException:
        return jsonify({"error": "GitHub download failed. Please try again."}), 502

@app.get("/p/<slug>")
def hosted_site_home(slug):
    with db() as conn:
        row = conn.execute("SELECT site_id FROM projects WHERE slug=?", (slug,)).fetchone()
    if not row: abort(404)
    return hosted_site(row["site_id"], "index.html")

@app.route("/api/projects/<int:project_id>", methods=["DELETE"])
def delete_project(project_id):
    user = current_user()
    if not user: return jsonify({"error": "Please sign in."}), 401
    with db() as conn:
        row = conn.execute("SELECT site_id FROM projects WHERE id=? AND user_id=?", (project_id, user["id"])).fetchone()
        if not row: return jsonify({"error": "Project not found."}), 404
        conn.execute("DELETE FROM projects WHERE id=? AND user_id=?", (project_id, user["id"]))
    import shutil
    shutil.rmtree(ROOT / row["site_id"], ignore_errors=True)
    return jsonify({"ok": True})

@app.get("/sites/<site_id>/<path:filename>")
def hosted_site(site_id, filename):
    if not re.fullmatch(r"[a-f0-9]{10}", site_id):
        abort(404)
    folder = ROOT / site_id
    candidate = (folder / filename).resolve()
    if not candidate.is_relative_to(folder.resolve()) or not candidate.is_file():
        abort(404)
    return send_from_directory(folder, filename)

def make_android_project(site_id, app_name):
    site_dir = ROOT / site_id
    safe_name = re.sub(r"[^A-Za-z0-9 ]", "", app_name).strip()[:40] or "AK Hosting App"
    package = "com.akhosting.app" + site_id
    java_package = package.replace(".", "/")
    files = {}
    for file in site_dir.rglob("*"):
        if file.is_file():
            rel = file.relative_to(site_dir).as_posix()
            files["app/src/main/assets/site/" + rel] = file.read_bytes()
    files["settings.gradle"] = "pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\ndependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }\nrootProject.name = \"" + safe_name + "\"\ninclude ':app'\n"
    files["build.gradle"] = "plugins { id 'com.android.application' version '8.7.3' apply false }\n"
    files["gradle.properties"] = "org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\nandroid.useAndroidX=true\n"
    files["app/build.gradle"] = "plugins { id 'com.android.application' }\nandroid { namespace '" + package + "'; compileSdk 35; defaultConfig { applicationId '" + package + "'; minSdk 23; targetSdk 35; versionCode 1; versionName '1.0' } }\ndependencies { implementation 'androidx.appcompat:appcompat:1.7.0'; implementation 'androidx.webkit:webkit:1.12.1' }\n"
    files["app/src/main/AndroidManifest.xml"] = "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<manifest xmlns:android=\"http://schemas.android.com/apk/res/android\"><uses-permission android:name=\"android.permission.INTERNET\"/><application android:theme=\"@android:style/Theme.Material.Light.NoActionBar\" android:label=\"" + safe_name.replace("&", "&amp;").replace("<", "") + "\" android:usesCleartextTraffic=\"false\"><activity android:name=\".MainActivity\" android:exported=\"true\"><intent-filter><action android:name=\"android.intent.action.MAIN\"/><category android:name=\"android.intent.category.LAUNCHER\"/></intent-filter></activity></application></manifest>\n"
    files["app/src/main/java/" + java_package + "/MainActivity.java"] = """package """ + package + """;
import android.os.Bundle;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import androidx.appcompat.app.AppCompatActivity;
import androidx.webkit.WebSettingsCompat;
import androidx.webkit.WebViewFeature;
public class MainActivity extends AppCompatActivity {
 @Override protected void onCreate(Bundle savedInstanceState) {
  super.onCreate(savedInstanceState);
  WebView view = new WebView(this);
  view.setWebViewClient(new WebViewClient());
  view.getSettings().setJavaScriptEnabled(true);
  view.getSettings().setDomStorageEnabled(true);
  if (WebViewFeature.isFeatureSupported(WebViewFeature.FORCE_DARK)) WebSettingsCompat.setForceDark(view.getSettings(), WebSettingsCompat.FORCE_DARK_OFF);
  setContentView(view);
  view.loadUrl("file:///android_asset/site/index.html");
 }
}
"""
    files[".github/workflows/build-apk.yml"] = """name: Build Android APK
on:
  workflow_dispatch:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: '17'
      - uses: android-actions/setup-android@v3
      - uses: gradle/actions/setup-gradle@v4
      - name: Build debug APK
        run: gradle assembleDebug
      - uses: actions/upload-artifact@v4
        with:
          name: AKHosting-debug-apk
          path: app/build/outputs/apk/debug/app-debug.apk
          retention-days: 14
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr("AKHostingAndroid/" + name, content)
    buf.seek(0)
    return buf

@app.post("/api/apk-project")
def apk_project():
    user = current_user()
    if not user: return jsonify({"error": "Please sign in first."}), 401
    data = request.get_json(silent=True) or {}
    site_id = str(data.get("site_id", ""))
    app_name = str(data.get("app_name", "AK Hosting App"))
    with db() as conn:
        owned = conn.execute("SELECT 1 FROM projects WHERE user_id=? AND site_id=?", (user["id"], site_id)).fetchone()
    if not owned or not re.fullmatch(r"[a-f0-9]{10}", site_id) or not (ROOT / site_id / "index.html").is_file():
        return jsonify({"error": "Choose one of your hosted websites first."}), 400
    if len(app_name) > 40:
        return jsonify({"error": "App name must be 40 characters or less."}), 400
    package = make_android_project(site_id, app_name)
    return send_file(package, mimetype="application/zip", as_attachment=True, download_name="AKHostingAndroidProject.zip")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
