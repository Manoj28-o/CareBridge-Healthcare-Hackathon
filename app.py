"""
app.py - the Flask REST API for the Healthcare Appointment demo.

Start it with:   python app.py        (then open http://127.0.0.1:5000/api/health)

Every response is JSON in one of two shapes:
    success:  {"success": true,  "data": ...}
    failure:  {"success": false, "error": {"code": "SLOT_UNAVAILABLE", "message": "..."}}

This is a DEMO with fictional data. It does not connect to real hospitals,
send real SMS or email, or give medical advice.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta

from flask import (
    Flask, current_app, g, jsonify, render_template, request,
    session
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_cors import CORS

import database
import recommender
import appointment_agent


# ---------------------------------------------------------------------------
# Small helpers for consistent JSON responses and errors
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """Raise this anywhere in a route to send a clean JSON error."""

    def __init__(self, status, code, message, field=None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field


def ok(data, status=200):
    return jsonify({"success": True, "data": data}), status


def error_response(status, code, message, field=None):
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return jsonify({"success": False, "error": body}), status


# ---------------------------------------------------------------------------
# Input validation (server-side, never trust the frontend)
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{7,15}$")


def get_json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "INVALID_JSON", "Request body must be a JSON object.")
    return body


def clean_text(value, field, min_len, max_len, required=True):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ApiError(400, "MISSING_FIELD", f"'{field}' is required.", field)
        return ""
    if not isinstance(value, str):
        raise ApiError(400, "INVALID_FIELD", f"'{field}' must be text.", field)
    value = value.strip()
    if not (min_len <= len(value) <= max_len):
        raise ApiError(400, "INVALID_FIELD",
                       f"'{field}' must be {min_len}-{max_len} characters long.", field)
    return value


def normalize_contact(value):
    """Accept an email or a phone number. Returns a cleaned version (lowercase email / digits)."""
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "MISSING_FIELD", "'contact' is required (phone or email).", "contact")
    value = value.strip()
    if EMAIL_RE.match(value):
        return value.lower()
    phone = re.sub(r"[\s\-().]", "", value)   # remove spaces, dashes, brackets
    if PHONE_RE.match(phone):
        return phone
    raise ApiError(400, "INVALID_FIELD",
                   "'contact' must be a valid email or a phone number (7-15 digits).", "contact")


def parse_int(value, field):
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise ApiError(400, "MISSING_FIELD", f"'{field}' is required.", field)
    try:
        return int(str(value).strip())
    except ValueError:
        raise ApiError(400, "INVALID_FIELD", f"'{field}' must be a whole number.", field)


def parse_date(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "MISSING_FIELD", f"'{field}' is required (YYYY-MM-DD).", field)
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ApiError(400, "INVALID_DATE", f"'{field}' must be a real date in YYYY-MM-DD format.", field)


def parse_time(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "MISSING_FIELD", f"'{field}' is required (HH:MM, 24 hour).", field)
    try:
        return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ApiError(400, "INVALID_TIME", f"'{field}' must be a valid time in HH:MM format.", field)


def parse_status(value):
    status = value.strip().upper()
    if status not in database.VALID_STATUSES:
        raise ApiError(400, "INVALID_FIELD",
                       "'status' must be one of: " + ", ".join(database.VALID_STATUSES), "status")
    return status


def parse_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Appointment queries
# ---------------------------------------------------------------------------
APPOINTMENT_SELECT = """
    SELECT a.appointment_id, a.status, a.patient_name, a.contact, a.reason,
           a.created_at, a.updated_at, a.cancelled_at, a.completed_at,
           d.id AS doctor_id, d.name AS doctor_name, d.specialty, d.hospital, d.location,
           s.slot_date AS appointment_date, s.slot_time AS appointment_time
      FROM appointments a
      JOIN doctors d           ON d.id = a.doctor_id
      JOIN appointment_slots s ON s.id = a.slot_id
"""


def appointment_to_dict(row):
    """Shape one database row into the JSON the frontend receives."""
    return {
        "appointment_id": row["appointment_id"],
        "status": row["status"],
        "patient_name": row["patient_name"],
        "contact": row["contact"],
        "reason": row["reason"],
        "doctor": {
            "id": row["doctor_id"], "name": row["doctor_name"], "specialty": row["specialty"],
            "hospital": row["hospital"], "location": row["location"],
        },
        "appointment_date": row["appointment_date"],
        "appointment_time": row["appointment_time"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "cancelled_at": row["cancelled_at"],
        "completed_at": row["completed_at"],
    }


def load_appointment(conn, appointment_id):
    """Fetch one appointment by its public ID or raise 404."""
    row = conn.execute(APPOINTMENT_SELECT + " WHERE a.appointment_id = ?",
                       (appointment_id.strip().upper(),)).fetchone()
    if row is None:
        raise ApiError(404, "APPOINTMENT_NOT_FOUND", f"No appointment with ID '{appointment_id}'.")
    return row


def require_doctor(conn, doctor_id):
    doctor = database.fetch_doctor(conn, doctor_id)
    if doctor is None:
        raise ApiError(404, "DOCTOR_NOT_FOUND", f"No doctor with ID {doctor_id}.")
    return doctor


def find_bookable_slot(conn, doctor_id, slot_date, slot_time):
    """
    Look up a slot and check it can be booked. MUST be called inside a
    transaction. Returns the slot row, or raises a helpful ApiError.
    """
    slot = conn.execute(
        "SELECT * FROM appointment_slots WHERE doctor_id=? AND slot_date=? AND slot_time=?",
        (doctor_id, slot_date, slot_time)).fetchone()
    if slot is None:
        raise ApiError(404, "SLOT_NOT_FOUND",
                       f"Doctor {doctor_id} has no slot on {slot_date} at {slot_time}.")
    if database.is_past(slot_date, slot_time):
        raise ApiError(400, "PAST_SLOT", "That time has already passed. Please pick a future slot.")
    if not slot["is_available"]:
        raise ApiError(409, "SLOT_UNAVAILABLE", "That slot is already booked. Please choose another time.")
    return slot


def claim_slot(conn, slot_id):
    """
    Mark a slot as taken. The `AND is_available = 1` part means this UPDATE only
    succeeds for the first caller; rowcount tells us if we won.
    """
    cursor = conn.execute(
        "UPDATE appointment_slots SET is_available = 0 WHERE id = ? AND is_available = 1", (slot_id,))
    if cursor.rowcount != 1:
        raise ApiError(409, "SLOT_UNAVAILABLE", "That slot is already booked. Please choose another time.")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(db_path=None):
    """
    Build the Flask app. Tests pass their own db_path so the real demo
    database is never touched.
    """
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get(
        "SECRET_KEY", "dev-only-change-this-before-deployment"
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    # DEMO ONLY: no real email is sent, so /api/forgot-password returns the reset code
    # in its response. Set DEMO_SHOW_RESET_CODE=0 to hide it (it is then only printed in the server console).
    app.config["DEMO_SHOW_RESET_CODE"] = os.environ.get("DEMO_SHOW_RESET_CODE", "1") != "0"
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["DB_PATH"] = db_path or database.DEFAULT_DB_PATH
    app.json.sort_keys = False

    # CORS configuration
    extra = [
        o.strip()
        for o in os.environ.get("CORS_ORIGINS", "").split(",")
        if o.strip()
    ]
    CORS(app, resources={
        r"/api/*": {
            "origins": [
                r"http://localhost:\d+",
                r"http://127\.0\.0\.1:\d+",
                "null",
                *extra,
            ]
        }
    }, supports_credentials=True)   # lets the login cookie travel from another localhost port

    database.init_db(app.config["DB_PATH"])

    # One database connection per request; closed automatically afterwards.
    def get_conn():
        if "conn" not in g:
            g.conn = database.get_connection(current_app.config["DB_PATH"])
        return g.conn

    @app.teardown_appcontext
    def close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    # ----- who is logged in? (session cookie set by /api/login) ------------
    def session_user():
        user_id = session.get("user_id")
        if user_id is None:
            return None
        return get_conn().execute("SELECT id, name, email, role FROM users WHERE id = ?", (user_id,)).fetchone()

    def require_login(role=None):
        """Raise 401 if nobody is logged in, 403 if the role is wrong."""
        user = session_user()
        if user is None:
            raise ApiError(401, "NOT_LOGGED_IN", "Please log in first.")
        if role and user["role"] != role:
            raise ApiError(403, "FORBIDDEN", "Admin access is required.")
        return user

    def public_user(row):
        return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]}

    # ----- error handlers: always JSON, never a stack trace ----------------
    @app.errorhandler(ApiError)
    def handle_api_error(err):
        return error_response(err.status, err.code, err.message, err.field)

    @app.errorhandler(sqlite3.IntegrityError)
    def handle_integrity_error(err):
        # The database constraints are our last line of defence against double booking.
        app.logger.warning("Integrity error: %s", err)
        return error_response(409, "CONFLICT", "That slot was just taken by someone else. Please choose another.")

    @app.errorhandler(sqlite3.Error)
    def handle_db_error(err):
        app.logger.exception("Database error")   # details go to the server log only
        return error_response(500, "DATABASE_ERROR", "A database problem occurred. Please try again.")

    @app.errorhandler(404)
    def handle_404(_err):
        return error_response(404, "NOT_FOUND", "That URL does not exist.")

    @app.errorhandler(405)
    def handle_405(_err):
        return error_response(405, "METHOD_NOT_ALLOWED", "That HTTP method is not allowed here.")

    @app.errorhandler(Exception)
    def handle_unexpected(err):
        app.logger.exception("Unexpected error")
        return error_response(500, "SERVER_ERROR", "Something went wrong on the server.")

    # ======================================================================
    # General
    # ======================================================================
    @app.get("/")
    def home():
        # Serves the frontend page (templates/index.html).
        return render_template("index.html")

    @app.get("/api")
    def api_info():
        return ok({"service": "Healthcare Appointment Demo API", "health": "/api/health",
                   "note": "Demo only: fictional data, no real hospitals, no medical advice."})

    @app.get("/api/health")
    def health():
        get_conn().execute("SELECT 1")   # proves the database answers too
        return ok({"status": "ok", "time": database.now_iso()})

    # ======================================================================
    # A. Doctors
    # ======================================================================
    @app.get("/api/doctors")
    def list_doctors():
        """Filters: q (name/specialty text), specialty, location, available=true, date=YYYY-MM-DD"""
        on_date = request.args.get("date", "").strip()
        if on_date:
            on_date = parse_date(on_date, "date")
        doctors = database.fetch_doctors(
            get_conn(),
            q=request.args.get("q", "").strip() or None,
            specialty=request.args.get("specialty", "").strip() or None,
            location=request.args.get("location", "").strip() or None,
            available_only=parse_bool(request.args.get("available", "")),
            on_date=on_date or None)
        return ok({"count": len(doctors), "doctors": doctors})

    @app.get("/api/doctors/<int:doctor_id>")
    def get_doctor(doctor_id):
        conn = get_conn()
        doctor = require_doctor(conn, doctor_id)
        doctor["available_slots"] = database.fetch_open_slots(conn, doctor_id)
        return ok(doctor)

    @app.get("/api/doctors/<int:doctor_id>/slots")
    def get_doctor_slots(doctor_id):
        conn = get_conn()
        require_doctor(conn, doctor_id)
        on_date = request.args.get("date", "").strip()
        if on_date:
            on_date = parse_date(on_date, "date")
        slots = database.fetch_open_slots(conn, doctor_id, on_date=on_date or None)
        return ok({"doctor_id": doctor_id, "count": len(slots), "slots": slots})

    # ======================================================================
    # B + C. Appointments
    # ======================================================================
    @app.post("/api/appointments")
    def book_appointment():
        require_login()   # only logged-in users may book
        body = get_json_body()
        patient_name = clean_text(body.get("patient_name"), "patient_name", 2, 100)
        contact = normalize_contact(body.get("contact"))
        doctor_id = parse_int(body.get("doctor_id"), "doctor_id")
        slot_date = parse_date(body.get("appointment_date"), "appointment_date")
        slot_time = parse_time(body.get("appointment_time"), "appointment_time")
        reason = clean_text(body.get("reason"), "reason", 0, 300, required=False)

        conn = get_conn()
        require_doctor(conn, doctor_id)

        # Everything below succeeds together or not at all.
        with database.transaction(conn):
            slot = find_bookable_slot(conn, doctor_id, slot_date, slot_time)
            claim_slot(conn, slot["id"])
            appointment_id = database.new_appointment_id()
            stamp = database.now_iso()
            conn.execute(
                """INSERT INTO appointments (appointment_id, doctor_id, slot_id, patient_name,
                                             contact, reason, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'BOOKED', ?, ?)""",
                (appointment_id, doctor_id, slot["id"], patient_name, contact, reason, stamp, stamp))

        appointment = appointment_to_dict(load_appointment(conn, appointment_id))
        return ok({"message": "Appointment booked successfully.", "appointment": appointment}, 201)

    @app.get("/api/appointments/<appointment_id>")
    def get_appointment(appointment_id):
        return ok(appointment_to_dict(load_appointment(get_conn(), appointment_id)))

    @app.get("/api/appointments")
    def list_appointments():
        """
        Filters: contact, appointment_id, doctor_id, date, status.
        At least one of contact / appointment_id / doctor_id is required, so
        the API never dumps every patient's record in one call.
        """
        args = request.args
        where, params = [], []
        if args.get("contact", "").strip():
            where.append("a.contact = ?")
            params.append(normalize_contact(args["contact"]))
        if args.get("appointment_id", "").strip():
            where.append("a.appointment_id = ?")
            params.append(args["appointment_id"].strip().upper())
        if args.get("doctor_id", "").strip():
            where.append("a.doctor_id = ?")
            params.append(parse_int(args["doctor_id"], "doctor_id"))
        if not where:
            raise ApiError(400, "MISSING_FILTER",
                           "Provide at least one of: contact, appointment_id, doctor_id.")
        if args.get("date", "").strip():
            where.append("s.slot_date = ?")
            params.append(parse_date(args["date"], "date"))
        if args.get("status", "").strip():
            where.append("a.status = ?")
            params.append(parse_status(args["status"]))
        sql = (APPOINTMENT_SELECT + " WHERE " + " AND ".join(where)   # only fixed text is joined
               + " ORDER BY s.slot_date, s.slot_time")
        rows = get_conn().execute(sql, params).fetchall()
        return ok({"count": len(rows), "appointments": [appointment_to_dict(r) for r in rows]})

    @app.patch("/api/appointments/<appointment_id>/cancel")
    def cancel_appointment(appointment_id):
        conn = get_conn()
        with database.transaction(conn):
            current = load_appointment(conn, appointment_id)
            if current["status"] != "BOOKED":
                raise ApiError(409, "INVALID_STATUS",
                               f"Only BOOKED appointments can be cancelled (this one is {current['status']}).")
            stamp = database.now_iso()
            appt = conn.execute("SELECT id, slot_id FROM appointments WHERE appointment_id = ?",
                                (current["appointment_id"],)).fetchone()
            conn.execute(
                "UPDATE appointments SET status='CANCELLED', cancelled_at=?, updated_at=? WHERE id=?",
                (stamp, stamp, appt["id"]))
            # Free the slot so someone else can book it.
            conn.execute("UPDATE appointment_slots SET is_available = 1 WHERE id = ?", (appt["slot_id"],))
        return ok({"message": "Appointment cancelled.",
                   "appointment": appointment_to_dict(load_appointment(conn, appointment_id))})

    @app.patch("/api/appointments/<appointment_id>/reschedule")
    def reschedule_appointment(appointment_id):
        body = get_json_body()
        new_date = parse_date(body.get("appointment_date"), "appointment_date")
        new_time = parse_time(body.get("appointment_time"), "appointment_time")
        conn = get_conn()
        with database.transaction(conn):
            current = load_appointment(conn, appointment_id)
            if current["status"] != "BOOKED":
                raise ApiError(409, "INVALID_STATUS",
                               f"Only BOOKED appointments can be rescheduled (this one is {current['status']}).")
            appt = conn.execute("SELECT id, slot_id, doctor_id FROM appointments WHERE appointment_id = ?",
                                (current["appointment_id"],)).fetchone()
            # Check "same slot" FIRST: the current slot is (correctly) marked unavailable,
            # so it would otherwise be reported as 'already booked'.
            if (new_date, new_time) == (current["appointment_date"], current["appointment_time"]):
                raise ApiError(400, "SAME_SLOT", "The appointment is already at that date and time.")
            new_slot = find_bookable_slot(conn, appt["doctor_id"], new_date, new_time)
            claim_slot(conn, new_slot["id"])
            conn.execute("UPDATE appointments SET slot_id=?, updated_at=? WHERE id=?",
                         (new_slot["id"], database.now_iso(), appt["id"]))
            conn.execute("UPDATE appointment_slots SET is_available = 1 WHERE id = ?", (appt["slot_id"],))
        return ok({"message": "Appointment rescheduled.",
                   "appointment": appointment_to_dict(load_appointment(conn, appointment_id))})

    @app.patch("/api/appointments/<appointment_id>/complete")
    def complete_appointment(appointment_id):
        # Demo simplification: allowed at any time so you can show it live.
        conn = get_conn()
        with database.transaction(conn):
            current = load_appointment(conn, appointment_id)
            if current["status"] != "BOOKED":
                raise ApiError(409, "INVALID_STATUS",
                               f"Only BOOKED appointments can be completed (this one is {current['status']}).")
            stamp = database.now_iso()
            conn.execute(
                "UPDATE appointments SET status='COMPLETED', completed_at=?, updated_at=? WHERE appointment_id=?",
                (stamp, stamp, current["appointment_id"]))
            # The slot stays unavailable: that visit really happened.
        return ok({"message": "Appointment marked as completed.",
                   "appointment": appointment_to_dict(load_appointment(conn, appointment_id))})

    # ======================================================================
    # D. Provider dashboard support
    # ======================================================================
    @app.get("/api/providers/<int:doctor_id>/appointments")
    def provider_appointments(doctor_id):
        """Filters: date=YYYY-MM-DD, status=BOOKED|CANCELLED|COMPLETED"""
        conn = get_conn()
        doctor = require_doctor(conn, doctor_id)
        where, params = ["a.doctor_id = ?"], [doctor_id]
        if request.args.get("date", "").strip():
            where.append("s.slot_date = ?")
            params.append(parse_date(request.args["date"], "date"))
        if request.args.get("status", "").strip():
            where.append("a.status = ?")
            params.append(parse_status(request.args["status"]))
        rows = conn.execute(
            APPOINTMENT_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY s.slot_date, s.slot_time",
            params).fetchall()
        return ok({"doctor_id": doctor_id, "doctor_name": doctor["name"], "count": len(rows),
                   "appointments": [appointment_to_dict(r) for r in rows]})

    @app.get("/api/providers/<int:doctor_id>/stats")
    def provider_stats(doctor_id):
        """Optional ?date=YYYY-MM-DD (defaults to today) for the 'daily count'."""
        conn = get_conn()
        doctor = require_doctor(conn, doctor_id)
        today, now = database.now_parts()
        day = parse_date(request.args["date"], "date") if request.args.get("date", "").strip() else today

        by_status = {status: 0 for status in database.VALID_STATUSES}
        for row in conn.execute(
                "SELECT status, COUNT(*) AS c FROM appointments WHERE doctor_id = ? GROUP BY status",
                (doctor_id,)):
            by_status[row["status"]] = row["c"]
        total = sum(by_status.values())

        on_day = conn.execute(
            """SELECT COUNT(*) AS c FROM appointments a JOIN appointment_slots s ON s.id = a.slot_id
                WHERE a.doctor_id = ? AND s.slot_date = ? AND a.status != 'CANCELLED'""",
            (doctor_id, day)).fetchone()["c"]

        upcoming = conn.execute(
            """SELECT COUNT(*) AS c FROM appointments a JOIN appointment_slots s ON s.id = a.slot_id
                WHERE a.doctor_id = ? AND a.status = 'BOOKED'
                  AND (s.slot_date > ? OR (s.slot_date = ? AND s.slot_time > ?))""",
            (doctor_id, today, today, now)).fetchone()["c"]

        # Next 7 days: appointments per day and how full the calendar is.
        start = datetime.strptime(today, "%Y-%m-%d").date()
        days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
        counts = {d: 0 for d in days}
        for row in conn.execute(
                """SELECT s.slot_date AS d, COUNT(*) AS c FROM appointments a
                     JOIN appointment_slots s ON s.id = a.slot_id
                    WHERE a.doctor_id = ? AND a.status != 'CANCELLED' AND s.slot_date BETWEEN ? AND ?
                    GROUP BY s.slot_date""", (doctor_id, days[0], days[-1])):
            counts[row["d"]] = row["c"]
        slot_row = conn.execute(
            """SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN is_available = 0 THEN 1 ELSE 0 END), 0) AS taken
                 FROM appointment_slots WHERE doctor_id = ? AND slot_date BETWEEN ? AND ?""",
            (doctor_id, days[0], days[-1])).fetchone()
        utilization = round(100 * slot_row["taken"] / slot_row["total"], 1) if slot_row["total"] else 0.0

        return ok({
            "doctor_id": doctor_id,
            "doctor_name": doctor["name"],
            "date": day,
            "appointments_on_date": on_day,
            "total_appointments": total,
            "by_status": by_status,
            "upcoming_booked": upcoming,
            "cancellation_rate_percent": round(100 * by_status["CANCELLED"] / total, 1) if total else 0.0,
            "daily_counts": [{"date": d, "count": counts[d]} for d in days],
            "next_7_days": {"total_slots": slot_row["total"], "booked_slots": slot_row["taken"],
                            "utilization_percent": utilization},
        })

    # ======================================================================
    # E. Integration for the recommendation teammate (read-only)
    # ======================================================================
    @app.get("/api/specialties")
    def list_specialties():
        return ok({"specialties": recommender.get_specialties(get_conn())})

    @app.get("/api/locations")
    def list_locations():
        return ok({"locations": recommender.get_locations(get_conn())})

    @app.get("/api/integration/doctors")
    def integration_doctors():
        """Doctors + their next free slots in ONE call. Filters: specialty, location, q, slots_per_doctor."""
        per_doctor = parse_int(request.args.get("slots_per_doctor", "5"), "slots_per_doctor")
        per_doctor = max(1, min(per_doctor, 50))
        doctors = recommender.get_doctors_with_slots(
            get_conn(),
            specialty=request.args.get("specialty", "").strip() or None,
            location=request.args.get("location", "").strip() or None,
            q=request.args.get("q", "").strip() or None,
            available_only=not request.args.get("available", "true").lower() in ("0", "false", "no"),
            slots_per_doctor=per_doctor)
        return ok({"count": len(doctors), "doctors": doctors})
    
    # ----- Appointment Agent (read-only search) -----
    
    # ----- Authentication -----
    @app.post("/api/register")
    def register():
        body = get_json_body()
        name = clean_text(body.get("name"), "name", 2, 100)
        email = clean_text(body.get("email"), "email", 5, 254).lower()
        password = body.get("password")

        if not EMAIL_RE.match(email):
            raise ApiError(400, "INVALID_EMAIL", "Enter a valid email address.", "email")

        if not isinstance(password, str) or len(password) < 8 or len(password) > 128:
            raise ApiError(
                400, "INVALID_PASSWORD",
                "Password must be between 8 and 128 characters.", "password"
            )

        conn = get_conn()
        password_hash = generate_password_hash(password)

        try:
            cursor = conn.execute(
                """INSERT INTO users (name, email, password_hash, created_at)
                   VALUES (?, ?, ?, ?)""",
                (name, email, password_hash, database.now_iso())
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "EMAIL_EXISTS", "An account with this email already exists.")

        user_id = cursor.lastrowid
        session.clear()
        session["user_id"] = user_id

        return ok({
            "message": "Registration successful.",
            "user": {"id": user_id, "name": name, "email": email, "role": "patient"}
        }, 201)

    def log_in(expected_role):
        body = get_json_body()
        email = clean_text(body.get("email"), "email", 5, 254).lower()
        password = body.get("password")
        if not isinstance(password, str) or not password or len(password) > 128:
            raise ApiError(400, "INVALID_CREDENTIALS", "Enter your email and password.")
        row = get_conn().execute(
            "SELECT id, name, email, role, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            raise ApiError(401, "INVALID_CREDENTIALS", "Incorrect email or password.")
        if row["role"] != expected_role:
            where = "the Admin login tab" if row["role"] == "admin" else "the Patient login tab"
            raise ApiError(403, "WRONG_LOGIN_TYPE", f"This account must sign in from {where}.")
        session.clear()
        session["user_id"] = row["id"]
        return ok({"message": "Login successful.", "user": public_user(row)})

    @app.post("/api/login")
    def login():
        return log_in("patient")

    @app.post("/api/admin/login")
    def admin_login():
        return log_in("admin")

    @app.get("/api/me")
    def current_user():
        user = session_user()
        if user is None:
            session.clear()
            raise ApiError(401, "NOT_LOGGED_IN", "Please log in first.")
        return ok({"user": public_user(user)})

    @app.post("/api/logout")
    def logout():
        session.clear()
        return ok({"message": "Logged out successfully."})
    @app.post("/api/agent")
    def appointment_agent_endpoint():
        body = get_json_body()
        message = clean_text(
            body.get("message"), "message", 1, 500
        )

        result = appointment_agent.run_agent(message, get_conn())

        return ok(result)
    # ======================================================================
    # Profile, change password, forgot / reset password
    # ======================================================================
    RESET_MINUTES, RESET_MAX_TRIES = 15, 5

    def valid_new_password(value):
        if not isinstance(value, str) or not 8 <= len(value) <= 128:
            raise ApiError(400, "INVALID_PASSWORD", "Password must be between 8 and 128 characters.", "new_password")
        return value

    def verify_current_password(user_id, password):
        row = get_conn().execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if not isinstance(password, str) or row is None or not check_password_hash(row["password_hash"], password):
            raise ApiError(403, "WRONG_PASSWORD", "Your current password is incorrect.", "current_password")

    def hash_code(code):
        return hashlib.sha256(code.encode()).hexdigest()

    @app.put("/api/profile")
    def update_profile():
        user = require_login()
        body = get_json_body()
        name = clean_text(body.get("name"), "name", 2, 100)
        email = clean_text(body.get("email"), "email", 5, 254).lower()
        if not EMAIL_RE.match(email):
            raise ApiError(400, "INVALID_EMAIL", "Enter a valid email address.", "email")
        if email != user["email"]:   # the email is the sign-in name, so changing it needs the password
            verify_current_password(user["id"], body.get("current_password"))
        conn = get_conn()
        try:
            with database.transaction(conn):
                conn.execute("UPDATE users SET name = ?, email = ? WHERE id = ?", (name, email, user["id"]))
        except sqlite3.IntegrityError:
            raise ApiError(409, "EMAIL_EXISTS", "Another account already uses this email.", "email")
        return ok({"message": "Profile updated.", "user": public_user(session_user())})

    @app.post("/api/profile/password")
    def change_password():
        user = require_login()
        body = get_json_body()
        verify_current_password(user["id"], body.get("current_password"))
        new_password = valid_new_password(body.get("new_password"))
        if new_password == body.get("current_password"):
            raise ApiError(400, "SAME_PASSWORD", "Choose a password different from the current one.", "new_password")
        conn = get_conn()
        with database.transaction(conn):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (generate_password_hash(new_password), user["id"]))
            conn.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (user["id"],))
        return ok({"message": "Password changed."})

    @app.post("/api/forgot-password")
    def forgot_password():
        """Creates a 6-digit one-time code (valid 15 minutes). Patient accounts only: admins cannot use this."""
        email = clean_text(get_json_body().get("email"), "email", 5, 254).lower()
        conn = get_conn()
        row = conn.execute("SELECT id FROM users WHERE email = ? AND role = 'patient'", (email,)).fetchone()
        # Same message whether or not the account exists, so the form does not reveal who is registered.
        data = {"message": "If a patient account exists for that email, a reset code has been created. "
                           "This demo sends no real email."}
        if row:
            code = f"{secrets.randbelow(1000000):06d}"
            expires = (datetime.now() + timedelta(minutes=RESET_MINUTES)).isoformat(timespec="seconds")
            with database.transaction(conn):
                conn.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (row["id"],))  # old codes die
                conn.execute("INSERT INTO password_resets (user_id, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                             (row["id"], hash_code(code), expires, database.now_iso()))
            print(f"[DEMO] Password reset code for user #{row['id']}: {code}")   # stands in for an email
            if app.config["DEMO_SHOW_RESET_CODE"]:
                data["demo_reset_code"] = code
        return ok(data)

    @app.post("/api/reset-password")
    def reset_password():
        body = get_json_body()
        email = clean_text(body.get("email"), "email", 5, 254).lower()
        code = clean_text(body.get("code"), "code", 6, 6)
        new_password = valid_new_password(body.get("new_password"))
        invalid = ApiError(400, "INVALID_RESET", "That code is invalid or has expired. Request a new one.")
        conn = get_conn()
        wrong_code = False
        with database.transaction(conn):
            row = conn.execute(
                """SELECT r.id, r.user_id, r.code_hash, r.expires_at, r.attempts FROM password_resets r
                     JOIN users u ON u.id = r.user_id
                    WHERE u.email = ? AND u.role = 'patient' AND r.used = 0 ORDER BY r.id DESC LIMIT 1""",
                (email,)).fetchone()
            if row is None or row["expires_at"] < database.now_iso() or row["attempts"] >= RESET_MAX_TRIES:
                raise invalid
            if hmac.compare_digest(row["code_hash"], hash_code(code)):
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(new_password), row["user_id"]))
                conn.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (row["user_id"],))
            else:
                conn.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
                wrong_code = True   # raise AFTER the transaction so the attempt count is saved
        if wrong_code:
            raise invalid
        session.clear()
        return ok({"message": "Password reset. You can now log in with the new password."})

    # ======================================================================
    # Admin: doctors and slots (every route needs an admin login)
    # ======================================================================
    def doctor_fields(body):
        years = parse_int(body.get("experience_years"), "experience_years")
        fee = parse_int(body.get("consultation_fee"), "consultation_fee")
        if not 0 <= years <= 70:
            raise ApiError(400, "INVALID_FIELD", "'experience_years' must be between 0 and 70.", "experience_years")
        if not 0 <= fee <= 100000:
            raise ApiError(400, "INVALID_FIELD", "'consultation_fee' must be between 0 and 100000.", "consultation_fee")
        return (clean_text(body.get("name"), "name", 2, 100), clean_text(body.get("specialty"), "specialty", 2, 60),
                clean_text(body.get("hospital"), "hospital", 2, 120), clean_text(body.get("location"), "location", 2, 60),
                years, fee, clean_text(body.get("description"), "description", 0, 500, required=False))

    @app.post("/api/admin/doctors")
    def admin_add_doctor():
        require_login("admin")
        fields = doctor_fields(get_json_body())
        conn = get_conn()
        with database.transaction(conn):
            cursor = conn.execute(
                """INSERT INTO doctors (name, specialty, hospital, location, experience_years,
                                        consultation_fee, description) VALUES (?, ?, ?, ?, ?, ?, ?)""", fields)
        return ok({"message": "Doctor added. Now add slots for them.",
                   "doctor": database.fetch_doctor(conn, cursor.lastrowid)}, 201)

    @app.put("/api/admin/doctors/<int:doctor_id>")
    def admin_edit_doctor(doctor_id):
        require_login("admin")
        fields = doctor_fields(get_json_body())
        conn = get_conn()
        require_doctor(conn, doctor_id)
        with database.transaction(conn):
            conn.execute(
                """UPDATE doctors SET name=?, specialty=?, hospital=?, location=?, experience_years=?,
                          consultation_fee=?, description=? WHERE id=?""", (*fields, doctor_id))
        return ok({"message": "Doctor updated.", "doctor": database.fetch_doctor(conn, doctor_id)})

    @app.delete("/api/admin/doctors/<int:doctor_id>")
    def admin_delete_doctor(doctor_id):
        require_login("admin")
        conn = get_conn()
        require_doctor(conn, doctor_id)
        with database.transaction(conn):
            if conn.execute("SELECT 1 FROM appointments WHERE doctor_id = ?", (doctor_id,)).fetchone():
                raise ApiError(409, "DOCTOR_HAS_APPOINTMENTS",
                               "This doctor has appointment records (current or past) and cannot be deleted.")
            conn.execute("DELETE FROM doctors WHERE id = ?", (doctor_id,))   # their empty slots go too
        return ok({"message": "Doctor deleted."})

    @app.get("/api/admin/doctors/<int:doctor_id>/slots")
    def admin_list_slots(doctor_id):
        """All upcoming slots including booked ones (the public endpoint hides those)."""
        require_login("admin")
        conn = get_conn()
        require_doctor(conn, doctor_id)
        rows = conn.execute(
            """SELECT id, slot_date, slot_time, is_available FROM appointment_slots
                WHERE doctor_id = ? AND slot_date >= ? ORDER BY slot_date, slot_time""",
            (doctor_id, database.now_parts()[0])).fetchall()
        slots = [{"slot_id": r["id"], "date": r["slot_date"], "time": r["slot_time"],
                  "booked": not r["is_available"]} for r in rows]
        return ok({"doctor_id": doctor_id, "count": len(slots), "slots": slots})

    @app.post("/api/admin/doctors/<int:doctor_id>/slots")
    def admin_add_slots(doctor_id):
        require_login("admin")
        body = get_json_body()
        conn = get_conn()
        require_doctor(conn, doctor_id)
        day = parse_date(body.get("date"), "date")
        if day < database.now_parts()[0]:
            raise ApiError(400, "PAST_DATE", "Choose today or a future date.", "date")
        times = body.get("times")
        if not isinstance(times, list) or not 1 <= len(times) <= 30:
            raise ApiError(400, "INVALID_FIELD", "'times' must be a list of 1-30 times in HH:MM format.", "times")
        times = sorted({parse_time(t, "times") for t in times})
        added = 0
        with database.transaction(conn):
            for slot_time in times:
                added += conn.execute(
                    "INSERT OR IGNORE INTO appointment_slots (doctor_id, slot_date, slot_time) VALUES (?, ?, ?)",
                    (doctor_id, day, slot_time)).rowcount
        return ok({"message": f"{added} slot(s) added.", "added": added, "skipped": len(times) - added}, 201)

    @app.delete("/api/admin/slots/<int:slot_id>")
    def admin_delete_slot(slot_id):
        require_login("admin")
        conn = get_conn()
        with database.transaction(conn):
            if conn.execute("SELECT 1 FROM appointment_slots WHERE id = ?", (slot_id,)).fetchone() is None:
                raise ApiError(404, "SLOT_NOT_FOUND", "No such slot.")
            if conn.execute("SELECT 1 FROM appointments WHERE slot_id = ?", (slot_id,)).fetchone():
                raise ApiError(409, "SLOT_IN_USE", "This slot has booking history, so it cannot be deleted. "
                                                   "Cancel the appointment first if it is still booked.")
            conn.execute("DELETE FROM appointment_slots WHERE id = ?", (slot_id,))
        return ok({"message": "Slot deleted."})

    return app


if __name__ == "__main__":
    # debug=True auto-reloads on code changes. Turn it off for any real deployment.
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=True)
