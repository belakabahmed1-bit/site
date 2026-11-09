"""قاعدة بيانات موحّدة ونظام إدارة البيانات للبوت.
يجب استيراده أولاً في bot.py (import db) قبل أي ملف آخر.
"""
import os
import json
import threading
from datetime import datetime
import sqlite3
import libsql_experimental as libsql
from datetime import timedelta

# الاتصال بقاعدة بيانات Turso السحابية عبر متغيرات البيئة
URL = os.getenv("TURSO_DB_URL")
TOKEN = os.getenv("TURSO_AUTH_TOKEN")

if not URL or not TOKEN:
    raise RuntimeError("TURSO_DB_URL و TURSO_AUTH_TOKEN يجب إضافتهما في متغيرات البيئة")

db_lock = threading.RLock()
_activity_cache = set()

_RETRY_HINTS = ("stream", "expired", "closed", "connection", "hrana", "timed out",
                "timeout", "baton", "error sending request", "transport", "broken pipe")


def _connect():
    return libsql.connect(database=URL, auth_token=TOKEN)


class _Holder:
    """يحتفظ بالاتصال الحقيقي ويعيد فتحه تلقائياً إذا انقطع."""

    def __init__(self):
        self.raw = _connect()

    def reconnect(self):
        try:
            self.raw = _connect()
        except Exception as e:
            print("DB reconnect failed:", e)


_holder = _Holder()


def _should_retry(exc):
    msg = str(exc).lower()
    return any(h in msg for h in _RETRY_HINTS)


class _SafeCursor:
    """مؤشر آمن للخيوط المتعددة: كل استعلام يُنفَّذ ويُقرأ كاملاً داخل القفل،
    فلا يحدث خطأ Recursive use of cursors مهما تزامنت الطلبات."""

    def __init__(self, holder):
        self._h = holder
        self._local = threading.local()

    def _st(self):
        l = self._local
        if not hasattr(l, "rows"):
            l.rows, l.pos = [], 0
            l.rowcount, l.lastrowid, l.description = -1, None, None
        return l

    def _run(self, fn):
        try:
            return fn(self._h.raw)
        except Exception as e:
            if not _should_retry(e):
                raise
            self._h.reconnect()
            return fn(self._h.raw)

    def execute(self, sql, params=None):
        st = self._st()

        def go(raw):
            cur = raw.cursor()
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            try:
                rows = cur.fetchall()
            except Exception:
                rows = []
            return cur, rows

        with db_lock:
            cur, rows = self._run(go)
            st.rows, st.pos = (list(rows) if rows else []), 0
            st.rowcount = getattr(cur, "rowcount", -1)
            st.lastrowid = getattr(cur, "lastrowid", None)
            st.description = getattr(cur, "description", None)
        return self

    def executescript(self, script):
        with db_lock:
            self._run(lambda raw: raw.cursor().executescript(script))
        return self

    def fetchone(self):
        st = self._st()
        if st.pos < len(st.rows):
            row = st.rows[st.pos]
            st.pos += 1
            return row
        return None

    def fetchall(self):
        st = self._st()
        rows = st.rows[st.pos:]
        st.pos = len(st.rows)
        return rows

    def fetchmany(self, size=1):
        st = self._st()
        rows = st.rows[st.pos:st.pos + size]
        st.pos += len(rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._st().rowcount

    @property
    def lastrowid(self):
        return self._st().lastrowid

    @property
    def description(self):
        return self._st().description

    def close(self):
        pass


class _SafeConn:
    def __init__(self, holder):
        self._h = holder

    def cursor(self):
        return _SafeCursor(self._h)

    def commit(self):
        with db_lock:
            try:
                self._h.raw.commit()
            except Exception as e:
                if not _should_retry(e):
                    raise
                self._h.reconnect()

    def rollback(self):
        with db_lock:
            try:
                self._h.raw.rollback()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._h.raw, name)


conn = _SafeConn(_holder)
cursor = conn.cursor()

user_steps, temp_data, attempts = {}, {}, {}

# الأدمن في وضع معاينة واجهة التلميذ
admin_preview = set()


def _cols(table):
    with db_lock:
        cursor.execute(f"PRAGMA table_info({table})")
        return [r[1] for r in cursor.fetchall()]


def _add_col(table, col, decl):
    with db_lock:
        if _cols(table) and col not in _cols(table):
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                conn.commit()
            except Exception:
                pass


# إنشاء جميع الجداول اللازمة
with db_lock:
    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        school TEXT,
        last_active TEXT,
        joined_at TEXT
    );

    CREATE TABLE IF NOT EXISTS wait (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        school TEXT,
        code TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS deleted_users (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        school TEXT,
        joined_at TEXT,
        last_active TEXT,
        deleted_at TEXT
    );

    CREATE TABLE IF NOT EXISTS user_activity (
        user_id INTEGER,
        activity_date TEXT,
        PRIMARY KEY (user_id, activity_date)
    );

    CREATE TABLE IF NOT EXISTS units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        is_active INTEGER DEFAULT 1,
        unit_type TEXT DEFAULT 'general'
    );

    CREATE TABLE IF NOT EXISTS exercises (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unit_id INTEGER,
        name TEXT,
        is_active INTEGER DEFAULT 1,
        pdf_file_id TEXT,
        solution_file_id TEXT,
        item_type TEXT DEFAULT 'exercise',
        extra_media TEXT DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        student_name TEXT,
        exercise_name TEXT,
        photo_id TEXT,
        status TEXT DEFAULT 'قيد الانتظار',
        is_read INTEGER DEFAULT 0,
        has_grade INTEGER DEFAULT 0,
        grade_text TEXT DEFAULT NULL,
        notes_history TEXT DEFAULT NULL
    );

    CREATE TABLE IF NOT EXISTS bac_topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year INTEGER,
        stream TEXT,
        title TEXT,
        pdf_file_id TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS file_folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        parent_id INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS custom_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        file_id TEXT,
        file_type TEXT,
        folder_id INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER,
        sender_name TEXT,
        receiver_id INTEGER,
        message_text TEXT,
        created_at TEXT,
        is_read INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

# إضافة الأعمدة للجداول القديمة إن لم تكن موجودة
_add_col("users", "joined_at", "TEXT")
_add_col("deleted_users", "joined_at", "TEXT")
_add_col("wait", "created_at", "TEXT")
_add_col("units", "unit_type", "TEXT DEFAULT 'general'")
_add_col("exercises", "item_type", "TEXT DEFAULT 'exercise'")
_add_col("exercises", "solution_file_id", "TEXT")
_add_col("exercises", "extra_media", "TEXT DEFAULT '[]'")
_add_col("submissions", "is_read", "INTEGER DEFAULT 0")
_add_col("submissions", "has_grade", "INTEGER DEFAULT 0")
_add_col("submissions", "grade_text", "TEXT")
_add_col("submissions", "notes_history", "TEXT")
_add_col("chat_messages", "file_id", "TEXT")
_add_col("chat_messages", "file_type", "TEXT")

# الوحدات الافتراضية الثابتة
default_units = [
    "الدوال العددية 📉",
    "الدلة الأسية 📈",
    "الدلة اللوغارتمية 📊",
    "المتتاليات 🔢",
    "الاحتمالات 🎲",
    "القسمة في Z ➗",
    "الأعداد المركبة 🧮"
]

with db_lock:
    # تحديث وتوحيد تسميات الوحدات
    try:
        cursor.execute("UPDATE units SET name = 'الدلة الأسية 📈' WHERE name LIKE '%الأسية%'")
        cursor.execute("UPDATE units SET name = 'الدلة اللوغارتمية 📊' WHERE name LIKE '%اللوغار%'")
    except Exception:
        pass

    for u_name in default_units:
        cursor.execute("INSERT OR IGNORE INTO units (name, is_active) VALUES (?, 1)", (u_name,))

    # الإعدادات الافتراضية
    default_settings = {
        "chat_enabled": "1",         # فتح المحادثة بين التلميذ والأستاذ (1=مفتوح, 0=مغلق)
        "notify_homework": "1",       # إشعار تلقائي عند إضافة واجب جديد
        "notify_lessons": "1",        # إشعار تلقائي عند إضافة درس/تمرين
        "notify_bac": "1"             # إشعار تلقائي عند إضافة موضوع بكالوريا
    }
    for k, v in default_settings.items():
        cursor.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()


# ===================== إعدادات النظام =====================
def get_setting(key, default="1"):
    with db_lock:
        cursor.execute("SELECT value FROM app_settings WHERE key=?", (key,))
        row = cursor.fetchone()
        return row[0] if row else default


def set_setting(key, value):
    with db_lock:
        cursor.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()


# ===================== إدارة المستخدمين =====================
def get_user(user_id):
    with db_lock:
        cursor.execute("SELECT user_id, full_name, school, last_active, joined_at FROM users WHERE user_id=?", (user_id,))
        return cursor.fetchone()


def get_wait(user_id):
    with db_lock:
        cursor.execute("SELECT user_id, full_name, school, code, created_at FROM wait WHERE user_id=?", (user_id,))
        return cursor.fetchone()


def activate_from_wait(user_id):
    with db_lock:
        d = get_wait(user_id)  # (user_id, full_name, school, code, created_at)
        if not d:
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        joined = d[4] if (len(d) > 4 and d[4]) else now
        cursor.execute("INSERT OR REPLACE INTO users (user_id, full_name, school, last_active, joined_at) VALUES (?,?,?,?,?)",
                       (d[0], d[1], d[2], now, joined))
        cursor.execute("DELETE FROM wait WHERE user_id=?", (user_id,))
        conn.commit()
        user_steps.pop(user_id, None)
        attempts.pop(user_id, None)
        return True


def track_activity(user_id):
    """يسجّل نشاط يومي للمستخدم المفعّل مع حماية الذاكرة لتفادي خطأ Recursive use of cursors."""
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = (user_id, today)
    if cache_key in _activity_cache:
        return

    with db_lock:
        if cache_key in _activity_cache:
            return
        if not get_user(user_id):
            return
        now_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        cursor.execute("INSERT OR IGNORE INTO user_activity (user_id, activity_date) VALUES (?,?)", (user_id, today))
        cursor.execute("UPDATE users SET last_active=? WHERE user_id=?", (now_time, user_id))
        conn.commit()
        _activity_cache.add(cache_key)


# ===================== إحصائيات وشارات =====================
def get_active_homework_count():
    """عدد الواجبات المفعلة حالياً والمفتوحة لاستقبال الحلول."""
    with db_lock:
        cursor.execute("""
            SELECT COUNT(e.id) FROM exercises e
            JOIN units u ON e.unit_id = u.id
            WHERE u.is_active = 1 AND e.is_active = 1 AND e.item_type = 'homework'
        """)
        res = cursor.fetchone()
        return res[0] if res else 0


def get_active_homework_count_for_unit(unit_id):
    """عدد الواجبات المفعلة داخل وحدة معينة."""
    with db_lock:
        cursor.execute("SELECT COUNT(*) FROM exercises WHERE unit_id=? AND is_active=1 AND item_type='homework'", (unit_id,))
        res = cursor.fetchone()
        return res[0] if res else 0


def get_unread_chat_count(receiver_id):
    """عدد الرسائل غير المقروءة لمستخدم معين (مع حذف الرسائل التي تجاوزت 24 ساعة)."""
    cleanup_old_chat_messages()
    with db_lock:
        cursor.execute("SELECT COUNT(*) FROM chat_messages WHERE receiver_id=? AND is_read=0", (receiver_id,))
        res = cursor.fetchone()
        return res[0] if res else 0


def get_total_active_students():
    """إجمالي التلاميذ المفعلين."""
    with db_lock:
        cursor.execute("SELECT COUNT(*) FROM users")
        res = cursor.fetchone()
        return res[0] if res else 0


# ===================== حذف وإدارة رسائل المحادثة (24 ساعة والحذف اليدوي) =====================
def cleanup_old_chat_messages():
    """حذف جميع الرسائل التي مضى عليها أكثر من 24 ساعة تلقائياً."""
    with db_lock:
        try:
            cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("DELETE FROM chat_messages WHERE created_at < ?", (cutoff,))
            conn.commit()
        except Exception as e:
            print("Chat cleanup error:", e)


def delete_chat_history(user_id):
    """حذف جميع رسائل المحادثة لمستخدم معين مع الأستاذ بناءً على طلبه."""
    with db_lock:
        try:
            cursor.execute("DELETE FROM chat_messages WHERE sender_id = ? OR receiver_id = ?", (user_id, user_id))
            conn.commit()
            return cursor.rowcount
        except Exception as e:
            print("Delete chat history error:", e)
            return 0


def delete_chat_message(message_id):
    """حذف رسالة محددة بالآيدي."""
    with db_lock:
        try:
            cursor.execute("DELETE FROM chat_messages WHERE id = ?", (message_id,))
            conn.commit()
            return cursor.rowcount
        except Exception as e:
            print("Delete chat message error:", e)
            return 0


# ===================== منطق الزر الأصفر (علامة 0/20 التلقائية وحذفها) =====================
def apply_auto_zero_to_missing_students(exercise_id):
    """عند تحويل الواجب إلى أصفر (منتهي)، يُمنح كل تلميذ لم يرسل حله علامة 00/20 تلقائياً."""
    with db_lock:
        cursor.execute("SELECT name FROM exercises WHERE id=?", (exercise_id,))
        ex_row = cursor.fetchone()
        if not ex_row:
            return 0
        ex_name = ex_row[0]
        clean_name = ex_name.replace("حل", "").strip()

        # جميع التلاميذ المفعلين
        cursor.execute("SELECT user_id, full_name FROM users")
        all_students = cursor.fetchall()

        # التلاميذ الذين قدموا حلاً لهذا الواجب بالفعل
        cursor.execute("SELECT DISTINCT student_id FROM submissions WHERE exercise_name = ?", (ex_name,))
        submitted_ids = {r[0] for r in cursor.fetchall()}

        now_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        day_names = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
        day_name = day_names[datetime.now().weekday()]
        full_date_str = f"{day_name} {now_time}"

        count_applied = 0
        for s_id, s_name in all_students:
            if s_id not in submitted_ids:
                cursor.execute("""
                    INSERT INTO submissions (student_id, student_name, exercise_name, photo_id, status, is_read, has_grade, grade_text, notes_history)
                    VALUES (?, ?, ?, 'NO_SUBMISSION', 'auto_zero', 1, 1, '00/20', ?)
                """, (
                    s_id, s_name, ex_name,
                    f"• [{full_date_str}] النظام التلقائي: انتهت مهلة تسليم الواجب ولم يتم استلام أي حل. العلامة المسجلة: 00/20."
                ))
                count_applied += 1
        conn.commit()
        return count_applied


def remove_auto_zero_on_reopen(exercise_id):
    """عند إعادة فتح الواجب ليكون أخضر مجدداً، تُحذف علامة 00/20 التلقائية التي لم يضعها الأستاذ يدوياً."""
    with db_lock:
        cursor.execute("SELECT name FROM exercises WHERE id=?", (exercise_id,))
        ex_row = cursor.fetchone()
        if not ex_row:
            return 0
        ex_name = ex_row[0]
        clean_name = ex_name.replace("حل", "").strip()

        cursor.execute(
            "DELETE FROM submissions WHERE exercise_name = ? AND status = 'auto_zero' AND grade_text = '00/20'",
            (ex_name,)
        )
        count_removed = cursor.rowcount
        conn.commit()
        return count_removed


# ===================== نسخة احتياطية =====================
def export_to_sqlite(path):
    """ينسخ كل الجداول من Turso إلى ملف SQLite محلي ويرجع عدد الصفوف المنسوخة."""
    total = 0
    with db_lock:
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")
        tables = cursor.fetchall()
        dest = sqlite3.connect(path)
        try:
            for name, create_sql in tables:
                dest.execute(create_sql)
                cursor.execute(f'SELECT * FROM "{name}"')
                rows = cursor.fetchall()
                if rows:
                    marks = ",".join("?" * len(rows[0]))
                    dest.executemany(f'INSERT INTO "{name}" VALUES ({marks})', [tuple(r) for r in rows])
                    total += len(rows)
            dest.commit()
        finally:
            dest.close()
    return total
