import os
import uuid
import base64
import io
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

import qrcode
from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session, g
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

DATABASE = os.path.join(os.path.dirname(__file__), "attendance.db")
QR_EXPIRY_MINUTES = int(os.environ.get("QR_EXPIRY_MINUTES", 10))
# 공인 IP/도메인을 명시할 때 설정. 예) http://203.0.113.5:5000 또는 https://example.com
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS professors (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            professor_id INTEGER NOT NULL,
            class_name  TEXT    NOT NULL,
            description TEXT,
            token       TEXT    NOT NULL UNIQUE,
            created_at  TEXT    NOT NULL,
            expires_at  TEXT    NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (professor_id) REFERENCES professors(id)
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            student_id  TEXT    NOT NULL,
            student_name TEXT   NOT NULL,
            submitted_at TEXT   NOT NULL,
            ip_address  TEXT,
            UNIQUE (session_id, student_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
    """)
    # Seed a default professor account (admin / admin123)
    try:
        db.execute(
            "INSERT INTO professors (name, username, password) VALUES (?, ?, ?)",
            ("관리자", "admin", "admin123"),
        )
    except sqlite3.IntegrityError:
        pass
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "professor_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes – Auth
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if "professor_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        prof = db.execute(
            "SELECT * FROM professors WHERE username=? AND password=?",
            (username, password),
        ).fetchone()
        if prof:
            session["professor_id"] = prof["id"]
            session["professor_name"] = prof["name"]
            return redirect(url_for("dashboard"))
        error = "아이디 또는 비밀번호가 올바르지 않습니다."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes – Professor dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    now = datetime.utcnow().isoformat()
    sessions_list = db.execute(
        """SELECT s.*, COUNT(a.id) AS attendance_count
           FROM sessions s
           LEFT JOIN attendance a ON a.session_id = s.id
           WHERE s.professor_id = ?
           GROUP BY s.id
           ORDER BY s.created_at DESC""",
        (session["professor_id"],),
    ).fetchall()
    return render_template("dashboard.html", sessions=sessions_list, now=now)


@app.route("/sessions/new", methods=["GET", "POST"])
@login_required
def new_session():
    if request.method == "POST":
        class_name = request.form.get("class_name", "").strip()
        description = request.form.get("description", "").strip()
        expiry_minutes = int(request.form.get("expiry_minutes", QR_EXPIRY_MINUTES))
        if not class_name:
            return render_template("new_session.html", error="강의명을 입력해 주세요.", default_expiry=QR_EXPIRY_MINUTES)

        token = str(uuid.uuid4())
        now = datetime.utcnow()
        expires_at = (now + timedelta(minutes=expiry_minutes)).isoformat()
        db = get_db()
        db.execute(
            "INSERT INTO sessions (professor_id, class_name, description, token, created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (session["professor_id"], class_name, description, token, now.isoformat(), expires_at),
        )
        db.commit()
        sess = db.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        return redirect(url_for("view_session", session_id=sess["id"]))
    return render_template("new_session.html", default_expiry=QR_EXPIRY_MINUTES)


@app.route("/sessions/<int:session_id>")
@login_required
def view_session(session_id):
    db = get_db()
    sess = db.execute(
        "SELECT * FROM sessions WHERE id=? AND professor_id=?",
        (session_id, session["professor_id"]),
    ).fetchone()
    if not sess:
        return redirect(url_for("dashboard"))

    attendance_list = db.execute(
        "SELECT * FROM attendance WHERE session_id=? ORDER BY submitted_at",
        (session_id,),
    ).fetchall()

    # BASE_URL 환경변수가 있으면 우선 사용 (공인 IP/도메인 지원)
    base = BASE_URL or request.host_url.rstrip("/")
    attend_url = base + url_for("attend", token=sess["token"])
    qr_img = _make_qr_base64(attend_url)

    now = datetime.utcnow().isoformat()
    return render_template(
        "session.html",
        sess=sess,
        attendance=attendance_list,
        qr_img=qr_img,
        attend_url=attend_url,
        now=now,
    )


@app.route("/sessions/<int:session_id>/toggle", methods=["POST"])
@login_required
def toggle_session(session_id):
    db = get_db()
    sess = db.execute(
        "SELECT * FROM sessions WHERE id=? AND professor_id=?",
        (session_id, session["professor_id"]),
    ).fetchone()
    if sess:
        db.execute("UPDATE sessions SET active=? WHERE id=?", (0 if sess["active"] else 1, session_id))
        db.commit()
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/delete", methods=["POST"])
@login_required
def delete_session(session_id):
    db = get_db()
    db.execute(
        "DELETE FROM sessions WHERE id=? AND professor_id=?",
        (session_id, session["professor_id"]),
    )
    db.execute("DELETE FROM attendance WHERE session_id=?", (session_id,))
    db.commit()
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Routes – Student attendance
# ---------------------------------------------------------------------------

@app.route("/attend/<token>", methods=["GET", "POST"])
def attend(token):
    db = get_db()
    sess = db.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    now = datetime.utcnow()

    if not sess:
        return render_template("attend.html", error="유효하지 않은 QR 코드입니다.", sess=None)

    expired = now.isoformat() > sess["expires_at"]
    inactive = not sess["active"]

    if request.method == "POST":
        if expired:
            return render_template("attend.html", sess=sess, error="출석 시간이 만료되었습니다.")
        if inactive:
            return render_template("attend.html", sess=sess, error="출석이 마감되었습니다.")

        student_id = request.form.get("student_id", "").strip()
        student_name = request.form.get("student_name", "").strip()

        if not student_id or not student_name:
            return render_template("attend.html", sess=sess, error="학번과 이름을 모두 입력해 주세요.")

        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        try:
            db.execute(
                "INSERT INTO attendance (session_id, student_id, student_name, submitted_at, ip_address) VALUES (?,?,?,?,?)",
                (sess["id"], student_id, student_name, now.isoformat(), ip),
            )
            db.commit()
            return render_template("attend.html", sess=sess, success=True)
        except sqlite3.IntegrityError:
            return render_template("attend.html", sess=sess, error="이미 출석 처리되었습니다.")

    return render_template("attend.html", sess=sess, expired=expired, inactive=inactive)


# ---------------------------------------------------------------------------
# API – live attendance count (for auto-refresh)
# ---------------------------------------------------------------------------

@app.route("/api/sessions/<int:session_id>/count")
@login_required
def attendance_count(session_id):
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM attendance WHERE session_id=?",
        (session_id,),
    ).fetchone()
    sess = db.execute("SELECT expires_at, active FROM sessions WHERE id=?", (session_id,)).fetchone()
    now = datetime.utcnow().isoformat()
    expired = now > sess["expires_at"] if sess else True
    return jsonify(count=row["cnt"], expired=expired, active=bool(sess["active"]) if sess else False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_qr_base64(data: str) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
