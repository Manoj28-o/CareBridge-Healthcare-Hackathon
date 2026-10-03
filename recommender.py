"""
recommender.py - a SMALL integration interface for the recommendation teammate.

Rules of this file:
  * It reads the SAME SQLite database as the API (no second database, no copies).
  * It needs NO API key and makes NO network calls.
  * It does NOT give medical advice. The functions below only fetch data.
    Your teammate's recommendation logic can call them and decide which doctor
    to suggest (for example by turning symptoms into a specialty first).

Two ways to use it:

  A) Inside Python (same project):
         import database, recommender
         conn = database.get_connection()
         try:
             doctors = recommender.get_doctors_with_slots(conn, specialty="Cardiology")
         finally:
             conn.close()

  B) Over HTTP (separate program / JavaScript):
         GET /api/integration/doctors?specialty=Cardiology&slots_per_doctor=3
         GET /api/specialties
         GET /api/locations
"""
import database


def get_specialties(conn):
    """Sorted list of every specialty in the database."""
    rows = conn.execute("SELECT DISTINCT specialty FROM doctors ORDER BY specialty").fetchall()
    return [row["specialty"] for row in rows]


def get_locations(conn):
    """Sorted list of every location in the database."""
    rows = conn.execute("SELECT DISTINCT location FROM doctors ORDER BY location").fetchall()
    return [row["location"] for row in rows]


def get_doctors_with_slots(conn, specialty=None, location=None, q=None,
                           available_only=True, slots_per_doctor=5):
    """
    Doctors (same fields as GET /api/doctors) plus a `next_slots` list.
    Each slot has slot_id, date and time, the same shape as GET /api/doctors/<id>/slots.
    """
    doctors = database.fetch_doctors(conn, q=q, specialty=specialty, location=location,
                                     available_only=available_only)
    for doctor in doctors:
        doctor["next_slots"] = database.fetch_open_slots(conn, doctor["id"], limit=slots_per_doctor)
    return doctors


def suggest_doctors(conn, specialty=None, location=None, limit=5):
    """
    PLACEHOLDER ranking: doctors whose first free slot is soonest come first.
    Replace or extend this with real recommendation logic.
    """
    doctors = get_doctors_with_slots(conn, specialty=specialty, location=location,
                                     available_only=True, slots_per_doctor=1)
    doctors.sort(key=lambda d: (d["next_slots"][0]["date"], d["next_slots"][0]["time"]))
    return doctors[:limit]
