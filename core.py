# -*- coding: utf-8 -*-
"""Бизнес-логика и хранилище: система обслуживания заявок на поступление на военную кафедру.

Версия 1.0.0. Хранилище — SQLite. Интерфейс (Streamlit) находится в app.py.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

APP_VERSION = "1.0.0"

MSK = timezone(timedelta(hours=3))
DB_PATH = os.environ.get(
    "VK_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db"),
)

# --- статусы заявки -------------------------------------------------------
S_NEW = "принята к рассмотрению"
S_OK = "принята"
S_FIX = "на доработке"
S_NO = "отклонена"
OPERATOR_STATUSES = (S_OK, S_FIX, S_NO)       # статусы, которые ставит оператор
ACTIVE_STATUSES = (S_NEW, S_OK, S_FIX)        # по ним студент не может подать вторую заявку

# --- роли ----------------------------------------------------------------
R_STUDENT = "student"
R_OPERATOR = "operator"
R_EDU_HEAD = "edu_head"
R_KAF_HEAD = "kafedra_head"
R_IT = "it_service"
ROLE_TITLES = {
    R_STUDENT: "Студент",
    R_OPERATOR: "Оператор",
    R_EDU_HEAD: "Начальник учебной части",
    R_KAF_HEAD: "Начальник военной кафедры",
    R_IT: "ИТ-служба",
}

REQUIRED_DOCS = [
    "Паспорт",
    "Приписное свидетельство или военный билет",
    "Заявление",
]
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_EXT = (".pdf", ".png", ".jpg", ".jpeg")
MAIL_SLA_SECONDS = 5 * 60   # NFR-01: письмо не позднее 5 минут после смены статуса

DEMO_PASSWORD = "test2026"

DUMMY_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class AppError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


def now_dt():
    return datetime.now(MSK)


def now_str():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    group_name TEXT NOT NULL,
    ticket_no TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    display_name TEXT NOT NULL,
    student_id INTEGER REFERENCES students(id)
);
CREATE TABLE IF NOT EXISTS specialties(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    seats INTEGER NOT NULL CHECK(seats >= 0)
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications(
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id),
    specialty_id INTEGER NOT NULL REFERENCES specialties(id),
    status TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    missing_docs TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    in_list INTEGER NOT NULL DEFAULT 0,
    list_decision TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_app
    ON applications(student_id) WHERE status IN ('принята к рассмотрению','принята','на доработке');
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    doc_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    content BLOB NOT NULL,
    uploaded_at TEXT NOT NULL,
    UNIQUE(application_id, doc_type)
);
CREATE TABLE IF NOT EXISTS status_log(
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    old_status TEXT,
    new_status TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    state TEXT NOT NULL
);
"""

STUDENTS = [
    ("Иванов Иван Сергеевич", "Б23-901", "23Б0101", "student01@mephi.test"),
    ("Петрова Мария Алексеевна", "Б23-901", "23Б0102", "student02@mephi.test"),
    ("Сидоров Артем Дмитриевич", "Б23-901", "23Б0103", "student03@mephi.test"),
    ("Козлова Анна Игоревна", "Б23-902", "23Б0201", "student04@mephi.test"),
    ("Новиков Павел Андреевич", "Б23-902", "23Б0202", "student05@mephi.test"),
    ("Морозова Елена Викторовна", "Б23-902", "23Б0203", "student06@mephi.test"),
    ("Волков Дмитрий Олегович", "Б23-903", "23Б0301", "student07@mephi.test"),
    ("Соколова Ольга Николаевна", "Б23-903", "23Б0302", "student08@mephi.test"),
    ("Лебедев Кирилл Романович", "Б23-903", "23Б0303", "student09@mephi.test"),
    ("Кузнецова Дарья Павловна", "Б23-904", "23Б0401", "student10@mephi.test"),
]
SPECIALTIES = [
    ("Автоматизированные системы управления", 2),
    ("Связь и радиотехническое обеспечение", 2),
    ("Эксплуатация радиоэлектронных систем", 3),
]
STAFF = [
    ("operator1", R_OPERATOR, "Оператор (учебная часть)"),
    ("edu_head", R_EDU_HEAD, "Начальник учебной части"),
    ("kafedra_head", R_KAF_HEAD, "Начальник военной кафедры"),
    ("it_service", R_IT, "ИТ-служба"),
]


# ---------------------------------------------------------------------------
# пароли и вход
# ---------------------------------------------------------------------------
def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000).hex()


def _make_user(conn, username, role, display_name, student_id=None, password=DEMO_PASSWORD):
    salt = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO users(username, salt, pw_hash, role, display_name, student_id) VALUES (?,?,?,?,?,?)",
        (username, salt, _hash(password, salt), role, display_name, student_id),
    )


def authenticate(username, password):
    """Вход по учётной записи. Возвращает словарь пользователя или None."""
    username = (username or "").strip()
    if not username or not password:
        return None
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(_hash(password, row["salt"]), row["pw_hash"]):
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "display_name": row["display_name"],
        "student_id": row["student_id"],
    }


# ---------------------------------------------------------------------------
# инициализация и тестовые данные
# ---------------------------------------------------------------------------
def init_db(force_reset=False):
    if force_reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    with connect() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for i, (name, grp, ticket, email) in enumerate(STUDENTS, start=1):
                c.execute(
                    "INSERT INTO students(id, full_name, group_name, ticket_no, email) VALUES (?,?,?,?,?)",
                    (i, name, grp, ticket, email),
                )
                _make_user(c, f"student{i:02d}", R_STUDENT, name, student_id=i)
            for name, seats in SPECIALTIES:
                c.execute("INSERT INTO specialties(name, seats) VALUES (?,?)", (name, seats))
            for username, role, title in STAFF:
                _make_user(c, username, role, title)
            c.execute("INSERT INTO settings(key, value) VALUES ('reception_open','1')")
            c.execute("INSERT INTO settings(key, value) VALUES ('mail_available','1')")
            c.execute("INSERT INTO settings(key, value) VALUES ('list_formed','0')")


def reset_db():
    """Полный сброс к исходным тестовым данным."""
    init_db(force_reset=True)


def seed_demo_applications():
    """Создаёт заявки студентов 05–10 (по два на специальность) для быстрой проверки итогового списка."""
    created = 0
    with connect() as c:
        existing = {r[0] for r in c.execute("SELECT student_id FROM applications")}
    plan = [(5, 1), (6, 1), (7, 1), (8, 2), (9, 2), (10, 3)]
    for sid, spec in plan:
        if sid in existing:
            continue
        files = {d: (f"{d}.pdf", DUMMY_PDF) for d in REQUIRED_DOCS}
        submit_application(sid, spec, files)
        created += 1
    return created


# ---------------------------------------------------------------------------
# настройки
# ---------------------------------------------------------------------------
def get_setting(key):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def reception_open():
    return get_setting("reception_open") == "1"


def mail_available():
    return get_setting("mail_available") == "1"


def list_formed():
    return get_setting("list_formed") == "1"


def set_reception_open(is_open):
    with connect() as c:
        if is_open and c.execute("SELECT value FROM settings WHERE key='list_formed'").fetchone()["value"] == "1":
            raise AppError("Итоговый список уже сформирован: приём заявок открыть нельзя.")
        _set_setting(c, "reception_open", "1" if is_open else "0")


def set_mail_available(is_available):
    with connect() as c:
        _set_setting(c, "mail_available", "1" if is_available else "0")


# ---------------------------------------------------------------------------
# справочники
# ---------------------------------------------------------------------------
def list_specialties():
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM specialties ORDER BY id")]


def get_student(student_id):
    with connect() as c:
        row = c.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# проверка файлов
# ---------------------------------------------------------------------------
def validate_file(doc_type, filename, data):
    if not filename or not data:
        raise AppError(f"«{doc_type}»: файл пустой.")
    if not filename.lower().endswith(ALLOWED_EXT):
        raise AppError(f"«{doc_type}»: допустимы только файлы PDF, PNG и JPG.")
    if len(data) > MAX_FILE_BYTES:
        raise AppError(f"«{doc_type}»: размер файла больше {MAX_FILE_BYTES // (1024 * 1024)} МБ.")
    low = filename.lower()
    ok = (
        (low.endswith(".pdf") and data.startswith(b"%PDF-"))
        or (low.endswith(".png") and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (low.endswith((".jpg", ".jpeg")) and data.startswith(b"\xff\xd8\xff"))
    )
    if not ok:
        raise AppError(f"«{doc_type}»: содержимое не соответствует формату файла.")


# ---------------------------------------------------------------------------
# заявки
# ---------------------------------------------------------------------------
def _log(conn, app_id, old, new, comment, by):
    conn.execute(
        "INSERT INTO status_log(application_id, old_status, new_status, comment, changed_by, changed_at)"
        " VALUES (?,?,?,?,?,?)",
        (app_id, old, new, comment, by, now_str()),
    )


def submit_application(student_id, specialty_id, files):
    """Подача заявки. files: {тип документа: (имя файла, байты)}."""
    if not reception_open():
        raise AppError("Приём заявок закрыт.")
    student = get_student(student_id)
    if student is None:
        raise AppError("Студент не найден в справочнике.")
    with connect() as c:
        if c.execute("SELECT 1 FROM specialties WHERE id = ?", (specialty_id,)).fetchone() is None:
            raise AppError("Выберите специальность из списка.")
        prev = c.execute(
            "SELECT id, status FROM applications WHERE student_id = ? ORDER BY id DESC LIMIT 1", (student_id,)
        ).fetchone()
    if prev is not None and prev["status"] in ACTIVE_STATUSES:
        raise AppError("Заявка уже подана. Повторная отправка не создаёт вторую заявку.")
    if prev is not None and prev["status"] == S_NO:
        raise AppError(
            "Ваша заявка отклонена. Порядок повторной подачи уточняется — обратитесь в учебную часть."
        )
    missing = [d for d in REQUIRED_DOCS if d not in files or not files[d] or not files[d][1]]
    if missing:
        raise AppError("Не приложены обязательные документы: " + "; ".join(missing) + ".")
    for doc_type in REQUIRED_DOCS:
        name, data = files[doc_type]
        validate_file(doc_type, name, data)
    ts = now_str()
    try:
        with connect() as c:
            cur = c.execute(
                "INSERT INTO applications(student_id, specialty_id, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?)",
                (student_id, specialty_id, S_NEW, ts, ts),
            )
            app_id = cur.lastrowid
            for doc_type in REQUIRED_DOCS:
                name, data = files[doc_type]
                c.execute(
                    "INSERT INTO documents(application_id, doc_type, filename, content, uploaded_at)"
                    " VALUES (?,?,?,?,?)",
                    (app_id, doc_type, name, data, ts),
                )
            _log(c, app_id, None, S_NEW, "Заявка подана студентом", student["full_name"])
    except sqlite3.IntegrityError:
        raise AppError("Заявка уже подана. Повторная отправка не создаёт вторую заявку.")
    return app_id


def get_student_application(student_id):
    """Последняя заявка студента или None."""
    with connect() as c:
        row = c.execute(
            "SELECT a.*, s.name AS specialty FROM applications a JOIN specialties s ON s.id = a.specialty_id"
            " WHERE a.student_id = ? ORDER BY a.id DESC LIMIT 1",
            (student_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["missing_docs"] = json.loads(d["missing_docs"])
    return d


def get_application(app_id):
    with connect() as c:
        row = c.execute(
            "SELECT a.*, s.name AS specialty, st.full_name, st.group_name, st.ticket_no, st.email"
            " FROM applications a JOIN specialties s ON s.id = a.specialty_id"
            " JOIN students st ON st.id = a.student_id WHERE a.id = ?",
            (app_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["missing_docs"] = json.loads(d["missing_docs"])
    return d


def get_documents(app_id):
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM documents WHERE application_id = ? ORDER BY id", (app_id,))]


def resubmit_documents(student_id, files):
    """Студент догружает документы по заявке в статусе «на доработке»."""
    app = get_student_application(student_id)
    if app is None or app["status"] != S_FIX:
        raise AppError("Догрузка документов возможна только для заявки в статусе «на доработке».")
    needed = app["missing_docs"]
    absent = [d for d in needed if d not in files or not files[d] or not files[d][1]]
    if absent:
        raise AppError("Не приложены документы: " + "; ".join(absent) + ".")
    for d in needed:
        validate_file(d, files[d][0], files[d][1])
    ts = now_str()
    student = get_student(student_id)
    with connect() as c:
        for d in needed:
            name, data = files[d]
            c.execute(
                "INSERT INTO documents(application_id, doc_type, filename, content, uploaded_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(application_id, doc_type) DO UPDATE SET filename = excluded.filename,"
                " content = excluded.content, uploaded_at = excluded.uploaded_at",
                (app["id"], d, name, data, ts),
            )
        c.execute(
            "UPDATE applications SET status = ?, missing_docs = '[]', updated_at = ? WHERE id = ?",
            (S_NEW, ts, app["id"]),
        )
        _log(c, app["id"], S_FIX, S_NEW, "Студент догрузил документы: " + "; ".join(needed), student["full_name"])


def list_applications(status=None):
    """Очередь заявок по порядку поступления; можно фильтровать по статусу."""
    q = (
        "SELECT a.id, a.status, a.created_at, a.updated_at, a.in_list, st.full_name, st.group_name,"
        " s.name AS specialty FROM applications a JOIN students st ON st.id = a.student_id"
        " JOIN specialties s ON s.id = a.specialty_id"
    )
    args = ()
    if status:
        q += " WHERE a.status = ?"
        args = (status,)
    q += " ORDER BY a.created_at ASC, a.id ASC"
    with connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def get_status_log(app_id):
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM status_log WHERE application_id = ? ORDER BY id", (app_id,))]


def change_status(app_id, new_status, comment, missing_docs, operator_name):
    """Оператор ставит статус. Возвращает True, если состояние изменилось."""
    comment = (comment or "").strip()
    missing_docs = list(missing_docs or [])
    if new_status not in OPERATOR_STATUSES:
        raise AppError("Недопустимый статус.")
    app = get_application(app_id)
    if app is None:
        raise AppError("Заявка не найдена.")
    if app["in_list"]:
        raise AppError(
            "Заявка включена в итоговый список. Изменить статус можно только по решению начальника военной кафедры."
        )
    if new_status == S_FIX:
        if not missing_docs:
            raise AppError("Для статуса «на доработке» укажите недостающие документы.")
        if any(d not in REQUIRED_DOCS for d in missing_docs):
            raise AppError("Недопустимый документ в списке недостающих.")
    else:
        missing_docs = []
    if new_status == S_NO and not comment:
        raise AppError("Для статуса «отклонена» нужен комментарий с причиной.")
    if app["status"] == new_status and app["comment"] == comment and app["missing_docs"] == missing_docs:
        return False   # повторное выполнение того же действия ничего не меняет
    ts = now_str()
    with connect() as c:
        c.execute(
            "UPDATE applications SET status = ?, comment = ?, missing_docs = ?, updated_at = ? WHERE id = ?",
            (new_status, comment, json.dumps(missing_docs, ensure_ascii=False), ts, app_id),
        )
        _log(c, app_id, app["status"], new_status, comment, operator_name)
        _queue_notification(c, app, new_status, comment, missing_docs, ts)
    return True


# ---------------------------------------------------------------------------
# письма
# ---------------------------------------------------------------------------
def _queue_notification(conn, app, new_status, comment, missing_docs, ts):
    subject = f"Заявка на военную кафедру: {new_status}"
    lines = [f"Здравствуйте, {app['full_name']}!", "", f"Статус вашей заявки: «{new_status}»."]
    if comment:
        lines.append(f"Комментарий оператора: {comment}")
    if missing_docs:
        lines.append("Нужно донести документы: " + "; ".join(missing_docs) + ".")
    body = "\n".join(lines)
    available = conn.execute("SELECT value FROM settings WHERE key='mail_available'").fetchone()["value"] == "1"
    conn.execute(
        "INSERT INTO notifications(application_id, to_email, subject, body, created_at, sent_at, state)"
        " VALUES (?,?,?,?,?,?,?)",
        (app["id"], app["email"], subject, body, ts, ts if available else None,
         "отправлено" if available else "письмо не ушло"),
    )


def list_notifications(only_failed=False):
    q = ("SELECT n.*, st.full_name FROM notifications n JOIN applications a ON a.id = n.application_id"
         " JOIN students st ON st.id = a.student_id")
    if only_failed:
        q += " WHERE n.state = 'письмо не ушло'"
    q += " ORDER BY n.id DESC"
    with connect() as c:
        rows = [dict(r) for r in c.execute(q)]
    for r in rows:
        if r["sent_at"]:
            r["delay_sec"] = int((parse_dt(r["sent_at"]) - parse_dt(r["created_at"])).total_seconds())
        else:
            r["delay_sec"] = int((now_dt().replace(tzinfo=None) - parse_dt(r["created_at"])).total_seconds())
    return rows


def student_notifications(student_id):
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT n.* FROM notifications n JOIN applications a ON a.id = n.application_id"
            " WHERE a.student_id = ? AND n.state = 'отправлено' ORDER BY n.id DESC", (student_id,))]


def retry_notification(notification_id):
    if not mail_available():
        raise AppError("Почтовый сервис недоступен: повторная отправка невозможна.")
    with connect() as c:
        row = c.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
        if row is None:
            raise AppError("Письмо не найдено.")
        if row["state"] == "отправлено":
            return False
        c.execute("UPDATE notifications SET sent_at = ?, state = 'отправлено' WHERE id = ?",
                  (now_str(), notification_id))
    return True


# ---------------------------------------------------------------------------
# итоговый список
# ---------------------------------------------------------------------------
def list_blockers():
    """Что мешает сформировать список: приём открыт или есть заявки без финального статуса."""
    blockers = []
    if reception_open():
        blockers.append("Приём заявок ещё открыт.")
    with connect() as c:
        n_new = c.execute("SELECT COUNT(*) FROM applications WHERE status = ?", (S_NEW,)).fetchone()[0]
        n_fix = c.execute("SELECT COUNT(*) FROM applications WHERE status = ?", (S_FIX,)).fetchone()[0]
    if n_new:
        blockers.append(f"Не обработано заявок: {n_new}.")
    if n_fix:
        blockers.append(f"Заявок «на доработке»: {n_fix}.")
    return blockers


def form_final_list():
    blockers = list_blockers()
    if blockers:
        raise AppError("Список сформировать нельзя. " + " ".join(blockers))
    with connect() as c:
        for spec in c.execute("SELECT * FROM specialties").fetchall():
            rows = c.execute(
                "SELECT id, in_list FROM applications WHERE specialty_id = ? AND status = ?"
                " ORDER BY created_at ASC, id ASC", (spec["id"], S_OK)).fetchall()
            for pos, r in enumerate(rows):
                if pos < spec["seats"] and not r["in_list"]:
                    c.execute("UPDATE applications SET in_list = 1 WHERE id = ?", (r["id"],))
                    _log(c, r["id"], S_OK, S_OK, "Включена в итоговый список", "система")
        _set_setting(c, "list_formed", "1")


def final_list_rows():
    with connect() as c:
        rows = c.execute(
            "SELECT s.name AS specialty, st.full_name, st.group_name, st.ticket_no, a.id AS app_id,"
            " a.list_decision FROM applications a JOIN students st ON st.id = a.student_id"
            " JOIN specialties s ON s.id = a.specialty_id WHERE a.in_list = 1"
            " ORDER BY s.id, a.created_at, a.id").fetchall()
    return [dict(r) for r in rows]


def surplus_rows():
    """Принятые заявки, не вошедшие в список из-за нехватки мест."""
    if not list_formed():
        return []
    with connect() as c:
        rows = c.execute(
            "SELECT s.name AS specialty, st.full_name, st.group_name, a.id AS app_id"
            " FROM applications a JOIN students st ON st.id = a.student_id"
            " JOIN specialties s ON s.id = a.specialty_id"
            " WHERE a.status = ? AND a.in_list = 0 ORDER BY s.id, a.created_at, a.id", (S_OK,)).fetchall()
    return [dict(r) for r in rows]


def seats_summary():
    with connect() as c:
        rows = c.execute(
            "SELECT s.id, s.name, s.seats,"
            " (SELECT COUNT(*) FROM applications a WHERE a.specialty_id = s.id AND a.status = ?) AS accepted,"
            " (SELECT COUNT(*) FROM applications a WHERE a.specialty_id = s.id AND a.in_list = 1) AS in_list"
            " FROM specialties s ORDER BY s.id", (S_OK,)).fetchall()
    return [dict(r) for r in rows]


def include_over_limit(app_id, reason, by):
    reason = (reason or "").strip()
    if not reason:
        raise AppError("Укажите основание решения.")
    app = get_application(app_id)
    if app is None or app["status"] != S_OK or app["in_list"] or not list_formed():
        raise AppError("Заявку нельзя включить сверх мест.")
    with connect() as c:
        c.execute("UPDATE applications SET in_list = 1, list_decision = ? WHERE id = ?", (reason, app_id))
        _log(c, app_id, S_OK, S_OK, "Включена в список сверх мест: " + reason, by)


def unlock_application(app_id, reason, by):
    """Снимает блокировку статуса заявки, уже вошедшей в список (решение начальника кафедры)."""
    reason = (reason or "").strip()
    if not reason:
        raise AppError("Укажите основание решения.")
    app = get_application(app_id)
    if app is None or not app["in_list"]:
        raise AppError("Заявка не входит в итоговый список.")
    with connect() as c:
        c.execute("UPDATE applications SET in_list = 0, list_decision = NULL, updated_at = ? WHERE id = ?",
                  (now_str(), app_id))
        _log(c, app_id, app["status"], app["status"], "Исключена из списка по решению: " + reason, by)


# ---------------------------------------------------------------------------
# сводка для начальника учебной части
# ---------------------------------------------------------------------------
def summary(date_from, date_to):
    """Число заявок по статусам за период (даты включительно, формат YYYY-MM-DD)."""
    if date_from > date_to:
        raise AppError("Дата начала периода позже даты окончания.")
    with connect() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM applications WHERE substr(created_at,1,10) BETWEEN ? AND ?"
            " GROUP BY status", (date_from, date_to)).fetchall()
    counts = {s: 0 for s in (S_NEW, S_OK, S_FIX, S_NO)}
    for r in rows:
        counts[r["status"]] = r["n"]
    return counts


def stale_new_applications(hours):
    limit = (now_dt() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT a.id, st.full_name, a.created_at FROM applications a JOIN students st ON st.id = a.student_id"
            " WHERE a.status = ? AND a.created_at <= ? ORDER BY a.created_at", (S_NEW, limit))]
