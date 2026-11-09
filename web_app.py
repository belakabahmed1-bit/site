"""موقع الأستاذ بلعقاب أسامة: يشارك قاعدة بيانات البوت نفسها (Turso)."""
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import (Flask, Response, abort, flash, get_flashed_messages, jsonify,
                   redirect, render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5279737033"))
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=60)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

with db.db_lock:
    db.cursor.executescript("""
    CREATE TABLE IF NOT EXISTS web_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, first_name TEXT, last_name TEXT,
        phone TEXT UNIQUE, branch TEXT, school TEXT, pw_hash TEXT, code TEXT,
        active INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS web_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, from_teacher INTEGER,
        text TEXT, created_at TEXT, is_read INTEGER DEFAULT 0);
    """)
    db.conn.commit()


def q(sql, args=(), one=False, commit=False):
    with db.db_lock:
        db.cursor.execute(sql, args)
        if commit:
            db.conn.commit()
            return db.cursor.lastrowid
        return db.cursor.fetchone() if one else db.cursor.fetchall()


def parse_media(extra_json, fallback=None):
    """يحوّل حقل extra_media إلى قائمة (النوع، file_id) بدون تكرار (نفس منطق البوت)."""
    try:
        data = json.loads(extra_json) if extra_json else []
    except Exception:
        data = []
    items, seen = [], set()
    for m in data if isinstance(data, list) else []:
        if isinstance(m, dict) and m.get("file_id") and m["file_id"] not in seen:
            seen.add(m["file_id"])
            items.append((m.get("type") or "unknown", m["file_id"]))
    return items or ([("unknown", fallback)] if fallback else [])


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def notify(text):
    """إشعار الأستاذ على تيليغرام."""
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": ADMIN_ID, "text": text}, timeout=5)
    except Exception as e:
        print("notify error:", e)


_fails = {}


def too_many(key, limit=8, window=600):
    t = time.time()
    _fails[key] = [x for x in _fails.get(key, []) if t - x < window]
    return len(_fails[key]) >= limit


def fail(key):
    _fails.setdefault(key, []).append(time.time())


def me():
    uid = session.get("uid")
    return q("SELECT id, first_name, last_name, active FROM web_users WHERE id=?", (uid,), one=True) if uid else None


def student_required(f):
    @wraps(f)
    def w(*a, **k):
        u = me()
        if not u:
            return redirect(url_for("login"))
        if not u[3]:
            return redirect(url_for("activate"))
        return f(u, *a, **k)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if session.get("admin") else redirect(url_for("admin"))
    return w


def new_password():
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))


# ============ التسجيل والدخول ============
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")
    d = request.get_json(force=True, silent=True) or {}
    first, last = (d.get("first") or "").strip(), (d.get("last") or "").strip()
    phone = re.sub(r"\D", "", d.get("phone") or "")
    arabic = lambda s: re.fullmatch(r"[\u0621-\u064A\s]{2,}", s)
    if not (arabic(first) and arabic(last)):
        return jsonify(error="اكتب الاسم واللقب بالحروف العربية فقط"), 400
    if not 9 <= len(phone) <= 12:
        return jsonify(error="رقم الهاتف غير صحيح"), 400
    if q("SELECT 1 FROM web_users WHERE phone=?", (phone,), one=True):
        return jsonify(error="هذا الرقم مسجل من قبل، سجّل الدخول"), 409
    pw = new_password()
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    q("INSERT INTO web_users (first_name, last_name, phone, branch, school, pw_hash, code, created_at) "
      "VALUES (?,?,?,?,?,?,?,?)",
      (first, last, phone, d.get("branch", ""), d.get("school", ""), generate_password_hash(pw), code, now()),
      commit=True)
    notify(f"🆕 تسجيل جديد في الموقع\n👤 {first} {last}\n📞 {phone}\n🏫 {d.get('school', '')}\n"
           f"🎓 {d.get('branch', '')}\n🔑 كود التفعيل: {code}")
    return jsonify(password=pw)


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        phone = re.sub(r"\D", "", request.form.get("phone", ""))
        u = q("SELECT id, pw_hash FROM web_users WHERE phone=?", (phone,), one=True)
        if too_many(("login", phone)):
            err = "محاولات كثيرة، أعد المحاولة بعد قليل"
        elif u and check_password_hash(u[1], request.form.get("password", "").strip()):
            session.clear()
            session["uid"], session.permanent = u[0], True
            return redirect("/")
        else:
            fail(("login", phone))
            err = "رقم الهاتف أو كلمة المرور غير صحيحة"
    return render_template("login.html", err=err)


@app.route("/activate", methods=["GET", "POST"])
def activate():
    u = me()
    if not u:
        return redirect(url_for("login"))
    if u[3]:
        return redirect("/")
    err = None
    if request.method == "POST":
        key = ("act", u[0])
        code = request.form.get("code", "").strip().upper()
        row = q("SELECT code FROM web_users WHERE id=?", (u[0],), one=True)
        if too_many(key, 5, 3600):
            err = "تجاوزت 5 محاولات خاطئة. تواصل مع الأستاذ أسامة مباشرة."
        elif row and code and code == (row[0] or "").upper():
            q("UPDATE web_users SET active=1 WHERE id=?", (u[0],), commit=True)
            return redirect("/")
        else:
            fail(key)
            err = "الكود غير صحيح، تأكد منه أو اطلبه من الأستاذ."
    return render_template("activate.html", err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============ المحتوى ============
def nat_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", str(s))]


@app.route("/")
@student_required
def home(u):
    latest = q("SELECT e.id, e.name, un.name FROM exercises e JOIN units un ON un.id = e.unit_id "
               "WHERE e.is_active=1 AND un.is_active=1 AND e.item_type='lesson' ORDER BY e.id DESC LIMIT 5")
    unread = q("SELECT COUNT(*) FROM web_messages WHERE user_id=? AND from_teacher=1 AND is_read=0",
               (u[0],), one=True)[0]
    return render_template("index.html", latest=latest, unread=unread)


KINDS = {"lesson": "الدروس", "exercise": "التمارين"}


@app.route("/units/<kind>")
@student_required
def units(u, kind):
    if kind not in KINDS:
        abort(404)
    rows = q("SELECT un.id, un.name, COUNT(e.id) FROM units un LEFT JOIN exercises e "
             "ON e.unit_id = un.id AND e.is_active=1 AND e.item_type=? WHERE un.is_active=1 "
             "GROUP BY un.id ORDER BY un.id", (kind,))
    return render_template("list.html", title=KINDS[kind], empty="لا توجد وحدات مفتوحة حالياً.",
                           rows=[(f"/unit/{i}/{kind}", n, f"{c} منشور", None) for i, n, c in rows])


@app.route("/unit/<int:uid>/<kind>")
@student_required
def unit(u, uid, kind):
    un = q("SELECT name FROM units WHERE id=? AND is_active=1", (uid,), one=True)
    if not un or kind not in KINDS:
        abort(404)
    rows = q("SELECT id, name FROM exercises WHERE unit_id=? AND is_active=1 AND item_type=?", (uid, kind))
    rows.sort(key=lambda r: (nat_key(r[1]), r[0]))
    return render_template("list.html", title=f"{KINDS[kind]} {un[0]}", empty="لا يوجد محتوى منشور بعد.",
                           rows=[(f"/item/{i}", n, None, None) for i, n in rows])


@app.route("/item/<int:iid>")
@student_required
def item(u, iid):
    r = q("SELECT e.name, e.pdf_file_id, e.extra_media, un.name FROM exercises e JOIN units un ON un.id = e.unit_id "
          "WHERE e.id=? AND e.is_active=1 AND un.is_active=1 AND e.item_type IN ('lesson','exercise')",
          (iid,), one=True)
    if not r:
        abort(404)
    return render_template("item.html", r=r, media=parse_media(r[2], r[1]))


@app.route("/bac")
@student_required
def bac(u):
    rows = q("SELECT pdf_file_id, title, year, stream FROM bac_topics ORDER BY stream, year DESC, id DESC")
    return render_template("list.html", title="مواضيع BAC", empty="لا توجد مواضيع بعد.", ext=True,
                           rows=[(f"/file/{f}", t, str(y), s) for f, t, y, s in rows if f])


_paths = {}


@app.route("/file/<fid>")
@student_required
def file(u, fid):
    """ملفات البوت مخزنة كـ file_id في تيليغرام؛ نمرّرها من الخادم دون كشف التوكن."""
    for attempt in range(2):
        p = _paths.get(fid)
        if not p:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getFile", params={"file_id": fid}, timeout=10).json()
            if not r.get("ok"):
                abort(404)
            p = _paths[fid] = r["result"]["file_path"]
        up = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{p}", stream=True, timeout=20)
        if up.status_code == 200:
            return Response(up.iter_content(65536), content_type=mimetypes.guess_type(p)[0] or "application/octet-stream",
                            headers={"Cache-Control": "private, max-age=3600"})
        _paths.pop(fid, None)
    abort(404)


# ============ التواصل مع الأستاذ ============
def thread(uid):
    return q("SELECT text, from_teacher, created_at FROM web_messages WHERE user_id=? ORDER BY id DESC LIMIT 100",
             (uid,))[::-1]


@app.route("/chat")
@student_required
def chat(u):
    q("UPDATE web_messages SET is_read=1 WHERE user_id=? AND from_teacher=1", (u[0],), commit=True)
    return render_template("chat.html", msgs=thread(u[0]))


@app.route("/chat/list")
@student_required
def chat_list(u):
    q("UPDATE web_messages SET is_read=1 WHERE user_id=? AND from_teacher=1", (u[0],), commit=True)
    return render_template("_msgs.html", msgs=thread(u[0]))


@app.route("/chat/send", methods=["POST"])
@student_required
def chat_send(u):
    text = request.form.get("text", "").strip()[:1000]
    if text and not too_many(("chat", u[0]), 20, 60):
        fail(("chat", u[0]))
        q("INSERT INTO web_messages (user_id, from_teacher, text, created_at) VALUES (?,0,?,?)",
          (u[0], text, now()), commit=True)
        notify(f"💬 رسالة من {u[1]} {u[2]} (الموقع):\n{text[:300]}")
    return "", 204


# ============ لوحة الأستاذ ============
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and not session.get("admin"):
        if not too_many(("admin", "x"), 5, 600) and hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["admin"] = True
        else:
            fail(("admin", "x"))
            flash("كلمة المرور غير صحيحة")
        return redirect(url_for("admin"))
    if not session.get("admin"):
        return render_template("admin.html", authed=False, msgs=get_flashed_messages())
    pending = q("SELECT id, first_name, last_name, phone, school, branch, code, created_at FROM web_users WHERE active=0 ORDER BY id DESC")
    active = q("SELECT id, first_name, last_name, phone, school, branch FROM web_users WHERE active=1 ORDER BY id DESC")
    convs = q("SELECT u.id, u.first_name || ' ' || u.last_name, "
              "SUM(CASE WHEN m.from_teacher=0 AND m.is_read=0 THEN 1 ELSE 0 END) FROM web_users u "
              "JOIN web_messages m ON m.user_id = u.id GROUP BY u.id ORDER BY MAX(m.id) DESC")
    return render_template("admin.html", authed=True, pending=pending, active=active, convs=convs,
                           msgs=get_flashed_messages())


@app.route("/admin/<action>/<int:uid>", methods=["POST"])
@admin_required
def admin_action(action, uid):
    if action == "activate":
        q("UPDATE web_users SET active=1 WHERE id=?", (uid,), commit=True)
    elif action == "delete":
        q("DELETE FROM web_messages WHERE user_id=?", (uid,), commit=True)
        q("DELETE FROM web_users WHERE id=?", (uid,), commit=True)
    elif action == "reset":
        pw = new_password()
        q("UPDATE web_users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), uid), commit=True)
        flash(f"كلمة المرور الجديدة: {pw}")
    elif action == "reply":
        text = request.form.get("text", "").strip()[:1000]
        if text:
            q("INSERT INTO web_messages (user_id, from_teacher, text, created_at) VALUES (?,1,?,?)",
              (uid, text, now()), commit=True)
        return redirect(url_for("admin_chat", uid=uid))
    else:
        abort(404)
    return redirect(url_for("admin"))


@app.route("/admin/chat/<int:uid>")
@admin_required
def admin_chat(uid):
    u = q("SELECT first_name, last_name FROM web_users WHERE id=?", (uid,), one=True)
    if not u:
        abort(404)
    q("UPDATE web_messages SET is_read=1 WHERE user_id=? AND from_teacher=0", (uid,), commit=True)
    return render_template("admin_chat.html", uid=uid, name=f"{u[0]} {u[1]}", msgs=thread(uid))


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
