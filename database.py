"""
database.py - everything about the SQLite database lives here.

What this file does:
  1. Opens database connections (with foreign keys switched ON).
  2. Creates the tables (schema) if they do not exist yet.
  3. Seeds fictional demo data ONCE (never duplicates on restart).
  4. Holds a few shared queries so app.py and recommender.py read doctors
     and slots in exactly the same way (one database, one source of truth).

Run `python database.py` to create/seed the database without starting Flask.
"""
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# You can override the database file with an environment variable.
DEFAULT_DB_PATH = os.environ.get("HEALTHCARE_DB", os.path.join(BASE_DIR, "healthcare.db"))

VALID_STATUSES = ("BOOKED", "CANCELLED", "COMPLETED")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    specialty         TEXT    NOT NULL,
    hospital          TEXT    NOT NULL,
    location          TEXT    NOT NULL,
    experience_years  INTEGER NOT NULL CHECK (experience_years >= 0),
    consultation_fee  INTEGER NOT NULL CHECK (consultation_fee >= 0),
    description       TEXT    NOT NULL DEFAULT ''
);

-- One row per doctor per date per time. is_available = 1 means "can be booked".
CREATE TABLE IF NOT EXISTS appointment_slots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id     INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    slot_date     TEXT    NOT NULL,            -- 'YYYY-MM-DD'
    slot_time     TEXT    NOT NULL,            -- 'HH:MM' (24 hour)
    is_available  INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1)),
    UNIQUE (doctor_id, slot_date, slot_time)   -- no duplicate slots
);

CREATE TABLE IF NOT EXISTS appointments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id  TEXT    NOT NULL UNIQUE,   -- public ID like 'APT-3F9A12BC'
    doctor_id       INTEGER NOT NULL REFERENCES doctors(id),
    slot_id         INTEGER NOT NULL REFERENCES appointment_slots(id),
    patient_name    TEXT    NOT NULL,
    contact         TEXT    NOT NULL,          -- phone or email (fictional in demo)
    reason          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'BOOKED'
                    CHECK (status IN ('BOOKED', 'CANCELLED', 'COMPLETED')),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    cancelled_at    TEXT,
    completed_at    TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'patient',   -- 'patient' or 'admin'
    created_at    TEXT NOT NULL
);

-- One-time password reset codes. Only a HASH of the code is stored.
CREATE TABLE IF NOT EXISTS password_resets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash   TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

-- THE double-booking guard: a slot may belong to at most ONE appointment that
-- is not cancelled. Even if two requests sneak through at the same moment,
-- SQLite itself rejects the second one. Cancelled rows are ignored, so a
-- cancelled slot can be booked again.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_slot
    ON appointments(slot_id) WHERE status != 'CANCELLED';

CREATE INDEX IF NOT EXISTS idx_slots_doctor_date ON appointment_slots(doctor_id, slot_date);
CREATE INDEX IF NOT EXISTS idx_appt_doctor       ON appointments(doctor_id, status);
CREATE INDEX IF NOT EXISTS idx_appt_contact      ON appointments(contact);
CREATE INDEX IF NOT EXISTS idx_doctors_specialty ON doctors(specialty, location);
"""


# ---------------------------------------------------------------------------
# Connections and transactions
# ---------------------------------------------------------------------------
def get_connection(db_path=None):
    """Open a connection. Remember to call .close() when you are done."""
    # isolation_level=None = "autocommit": we start transactions ourselves
    # with BEGIN IMMEDIATE (see transaction() below).
    conn = sqlite3.connect(db_path or DEFAULT_DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row            # rows behave like dictionaries
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite ignores foreign keys unless asked
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn):
    """
    Run a block of SQL as ONE all-or-nothing unit.

    BEGIN IMMEDIATE takes the database write lock right away. If two people
    try to book at the same time, the second request waits until the first
    finishes, then sees the slot is already taken. That is what makes booking
    safe under simultaneous requests.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def now_parts():
    """Current local date and time as strings: ('2025-01-31', '14:05')."""
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def is_past(slot_date, slot_time):
    """True if the given slot has already started."""
    today, now = now_parts()
    return (slot_date, slot_time) <= (today, now)


def new_appointment_id():
    return "APT-" + uuid.uuid4().hex[:8].upper()


# ---------------------------------------------------------------------------
# Shared read queries (used by app.py AND recommender.py)
# ---------------------------------------------------------------------------
# A slot counts as "upcoming" only if it is later than right now.
FUTURE_SLOT = "(s.slot_date > :today OR (s.slot_date = :today AND s.slot_time > :now))"


def like_pattern(text):
    """Turn user text into a safe LIKE pattern (so % and _ are not wildcards)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def fetch_doctors(conn, q=None, specialty=None, location=None,
                  available_only=False, on_date=None):
    """List doctors. All filters are optional. Uses parameterized SQL only."""
    today, now = now_parts()
    params = {"today": today, "now": now}
    sql = f"""
        SELECT d.*,
               (SELECT COUNT(*) FROM appointment_slots s
                 WHERE s.doctor_id = d.id AND s.is_available = 1 AND {FUTURE_SLOT}
               ) AS available_slots_count
          FROM doctors d
         WHERE 1 = 1"""
    if q:
        sql += " AND (d.name LIKE :q ESCAPE '\\' OR d.specialty LIKE :q ESCAPE '\\')"
        params["q"] = like_pattern(q)
    if specialty:
        sql += " AND d.specialty = :specialty COLLATE NOCASE"
        params["specialty"] = specialty
    if location:
        sql += " AND d.location LIKE :location ESCAPE '\\'"
        params["location"] = like_pattern(location)
    if available_only:
        sql += f""" AND EXISTS (SELECT 1 FROM appointment_slots s
                     WHERE s.doctor_id = d.id AND s.is_available = 1 AND {FUTURE_SLOT})"""
    if on_date:
        sql += f""" AND EXISTS (SELECT 1 FROM appointment_slots s
                     WHERE s.doctor_id = d.id AND s.slot_date = :on_date
                       AND s.is_available = 1 AND {FUTURE_SLOT})"""
        params["on_date"] = on_date
    sql += " ORDER BY d.name"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def fetch_doctor(conn, doctor_id):
    """One doctor (or None) with available_slots_count."""
    today, now = now_parts()
    row = conn.execute(
        f"""SELECT d.*,
                   (SELECT COUNT(*) FROM appointment_slots s
                     WHERE s.doctor_id = d.id AND s.is_available = 1 AND {FUTURE_SLOT}
                   ) AS available_slots_count
              FROM doctors d WHERE d.id = :id""",
        {"id": doctor_id, "today": today, "now": now},
    ).fetchone()
    return dict(row) if row else None


def fetch_open_slots(conn, doctor_id, on_date=None, limit=None):
    """Upcoming, bookable slots for one doctor, earliest first."""
    today, now = now_parts()
    params = {"doctor_id": doctor_id, "today": today, "now": now}
    sql = f"""SELECT s.id AS slot_id, s.slot_date AS date, s.slot_time AS time
                FROM appointment_slots s
               WHERE s.doctor_id = :doctor_id AND s.is_available = 1 AND {FUTURE_SLOT}"""
    if on_date:
        sql += " AND s.slot_date = :on_date"
        params["on_date"] = on_date
    sql += " ORDER BY s.slot_date, s.slot_time"
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# Seed data (all fictional)
# ---------------------------------------------------------------------------
SEED_DOCTORS = [
    # name, specialty, hospital, location, years, fee, description
    ("Dr. Asha Verma", "Cardiology", "Riverside Heart Institute", "Hyderabad", 15, 800,
     "Fictional doctor. Focuses on heart health, blood pressure and preventive cardiology."),
    ("Dr. Rohan Mehta", "Cardiology", "Lakeview Cardiac Care", "Bengaluru", 11, 700,
     "Fictional doctor. Experienced in ECG interpretation and cardiac rehabilitation."),
    ("Dr. Neha Kapoor", "Dermatology", "Glow Skin Clinic", "Hyderabad", 8, 500,
     "Fictional doctor. Treats acne, eczema, allergies and common skin conditions."),
    ("Dr. Imran Qureshi", "Pediatrics", "Little Steps Children's Hospital", "Mumbai", 12, 600,
     "Fictional doctor. Child health, vaccinations and growth monitoring."),
    ("Dr. Priya Nair", "Orthopedics", "Greenfield Bone and Joint Centre", "Chennai", 14, 900,
     "Fictional doctor. Joint pain, sports injuries and fracture follow-up."),
    ("Dr. Karthik Rao", "Neurology", "Summit Neuro Centre", "Bengaluru", 17, 1000,
     "Fictional doctor. Headaches, migraines and nerve-related conditions."),
    ("Dr. Sana Sheikh", "General Medicine", "Sunrise Family Clinic", "Hyderabad", 9, 400,
     "Fictional doctor. First point of contact for fever, cough and general check-ups."),
    ("Dr. Vikram Joshi", "ENT", "ClearHear ENT Hospital", "Mumbai", 10, 550,
     "Fictional doctor. Ear, nose and throat problems, sinus and hearing checks."),
    ("Dr. Meera Iyer", "Gynecology", "Harmony Women's Health", "Chennai", 13, 750,
     "Fictional doctor. Women's health, prenatal care and routine screenings."),
    ("Dr. Arjun Das", "General Medicine", "Metro Care Clinic", "Mumbai", 6, 350,
     "Fictional doctor. Routine consultations and chronic-condition follow-ups."),
]

SLOT_TIMES = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
              "14:00", "14:30", "15:00", "16:00"]


def top_up_slots(conn, days=7, include_empty=False):
    """
    Keep every doctor's calendar filled up to today + `days - 1`.
    It only EXTENDS a calendar past the doctor's last existing slot, so slots an
    admin deleted are not recreated on restart, and a doctor with no slots stays
    empty until the admin adds some (include_empty=True is used only when seeding).
    """
    doctor_ids = [r["id"] for r in conn.execute("SELECT id FROM doctors ORDER BY id")]
    today = datetime.now().date()
    with transaction(conn):
        for index, doctor_id in enumerate(doctor_ids):
            last = conn.execute("SELECT MAX(slot_date) AS d FROM appointment_slots WHERE doctor_id = ?",
                                (doctor_id,)).fetchone()["d"]
            if last is None and not include_empty:
                continue
            first = today if last is None else max(today, datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1))
            times = SLOT_TIMES[index % 4: index % 4 + 6]   # a slightly different day per doctor
            for offset in range(days):
                day = today + timedelta(days=offset)
                if day < first:
                    continue
                for slot_time in times:
                    conn.execute(
                        "INSERT OR IGNORE INTO appointment_slots (doctor_id, slot_date, slot_time)"
                        " VALUES (?, ?, ?)", (doctor_id, day.isoformat(), slot_time))


def _insert_sample_appointment(conn, doctor_id, day, slot_time, name, contact, reason, status):
    """Create a sample slot (if needed) and a sample appointment on it."""
    conn.execute(
        "INSERT OR IGNORE INTO appointment_slots (doctor_id, slot_date, slot_time, is_available)"
        " VALUES (?, ?, ?, 1)", (doctor_id, day, slot_time))
    slot = conn.execute(
        "SELECT id FROM appointment_slots WHERE doctor_id=? AND slot_date=? AND slot_time=?",
        (doctor_id, day, slot_time)).fetchone()
    stamp = now_iso()
    conn.execute(
        """INSERT INTO appointments (appointment_id, doctor_id, slot_id, patient_name, contact,
                                     reason, status, created_at, updated_at, cancelled_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_appointment_id(), doctor_id, slot["id"], name, contact, reason, status, stamp, stamp,
         stamp if status == "CANCELLED" else None, stamp if status == "COMPLETED" else None))
    if status != "CANCELLED":
        conn.execute("UPDATE appointment_slots SET is_available = 0 WHERE id = ?", (slot["id"],))


def seed_demo_data(conn):
    """Insert doctors + sample appointments, but ONLY if the doctors table is empty."""
    if conn.execute("SELECT COUNT(*) AS c FROM doctors").fetchone()["c"] > 0:
        return False
    with transaction(conn):
        conn.executemany(
            """INSERT INTO doctors (name, specialty, hospital, location, experience_years,
                                    consultation_fee, description) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            SEED_DOCTORS)
    top_up_slots(conn, include_empty=True)

    today = datetime.now().date()
    day1 = (today + timedelta(days=1)).isoformat()
    day2 = (today + timedelta(days=2)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    # Fictional patients: names are made up and the contacts use example/fake formats.
    samples = [
        (1, day1, "10:00", "Demo Patient One", "demo.patient1@example.com", "Routine check-up", "BOOKED"),
        (1, day1, "10:30", "Demo Patient Two", "+919000000002", "Follow-up visit", "BOOKED"),
        (1, day2, "09:00", "Demo Patient Three", "+919000000003", "Blood pressure review", "BOOKED"),
        (1, yesterday, "09:00", "Demo Patient Four", "demo.patient4@example.com", "Chest pain review", "COMPLETED"),
        (1, yesterday, "09:30", "Demo Patient Five", "+919000000005", "Cancelled by patient", "CANCELLED"),
        (3, day1, "09:00", "Demo Patient Six", "demo.patient6@example.com", "Skin rash", "BOOKED"),
        (7, day1, "09:30", "Demo Patient Seven", "+919000000007", "Fever and cough", "BOOKED"),
    ]
    with transaction(conn):
        for doctor_id, day, slot_time, name, contact, reason, status in samples:
            _insert_sample_appointment(conn, doctor_id, day, slot_time, name, contact, reason, status)
    return True


def migrate(conn):
    """Add columns that older copies of healthcare.db do not have yet."""
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
    if "role" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'patient'")


def ensure_admin(conn):
    """
    Make sure one admin account exists. Set ADMIN_EMAIL and ADMIN_PASSWORD in the
    environment to choose your own. The built-in default is for the LOCAL DEMO only.
    """
    if conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
        return
    email = os.environ.get("ADMIN_EMAIL", "admin@medislot.demo").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "Admin@12345")
    conn.execute(
        "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)"
        " ON CONFLICT(email) DO UPDATE SET role = 'admin'",
        ("Demo Admin", email, generate_password_hash(password), now_iso()))


def init_db(db_path=None):
    """Create tables, seed once, and top up slots. Safe to call on every start."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        ensure_admin(conn)
        seed_demo_data(conn)
        top_up_slots(conn)   # keeps dates fresh on later restarts (no duplicates)
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database ready at: {DEFAULT_DB_PATH}")
    