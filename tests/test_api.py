"""
tests/test_api.py - automated tests for the Healthcare Appointment API.

Run from the project folder with:   python -m unittest discover -s tests -v

Every test gets its OWN temporary database (created fresh in setUp and deleted
in tearDown), so your real demo database (healthcare.db) is never touched.
"""
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

# Let the tests import app.py / database.py from the project folder.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from app import create_app  # noqa: E402


def future_date(days=1):
    """A date a few days from now, so the slot is never 'in the past'."""
    return (datetime.now().date() + timedelta(days=days)).isoformat()


ADMIN_EMAIL, ADMIN_PASSWORD = "test.admin@example.com", "Admin-Test-123"


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"] = ADMIN_EMAIL, ADMIN_PASSWORD
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(self.db_path)
        self.app.testing = True
        self.client = self.app.test_client()
        self.login_patient(self.client)   # booking needs a logged-in user

    def login_patient(self, client, email="patient@example.com"):
        body = {"name": "Test Patient", "email": email, "password": "password123"}
        if client.post("/api/register", json=body).status_code != 201:
            client.post("/api/login", json=body)

    def login_admin(self, client):
        resp = client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        self.assertEqual(resp.status_code, 200)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass

    # ----- helpers --------------------------------------------------------
    def free_slots(self, doctor_id=1, date=None):
        url = f"/api/doctors/{doctor_id}/slots" + (f"?date={date}" if date else "")
        return self.client.get(url).get_json()["data"]["slots"]

    def book(self, doctor_id=1, date=None, time=None, name="Test Patient",
             contact="test.patient@example.com"):
        """Book a slot (the first free one on day+3 unless told otherwise)."""
        if date is None or time is None:
            slot = self.free_slots(doctor_id, future_date(3))[0]
            date, time = slot["date"], slot["time"]
        return self.client.post("/api/appointments", json={
            "patient_name": name, "contact": contact, "doctor_id": doctor_id,
            "appointment_date": date, "appointment_time": time, "reason": "Test visit"})


class TestHealthAndDoctors(ApiTestCase):
    def test_health(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(resp.get_json()["data"]["status"], "ok")

    def test_seed_has_enough_doctors_and_is_not_duplicated(self):
        doctors = self.client.get("/api/doctors").get_json()["data"]["doctors"]
        self.assertGreaterEqual(len(doctors), 8)
        self.assertGreaterEqual(len({d["specialty"] for d in doctors}), 5)
        create_app(self.db_path)   # "restart the server" on the same database
        again = self.client.get("/api/doctors").get_json()["data"]["doctors"]
        self.assertEqual(len(doctors), len(again))

    def test_filter_by_specialty(self):
        data = self.client.get("/api/doctors?specialty=cardiology").get_json()["data"]
        self.assertGreaterEqual(data["count"], 1)
        self.assertTrue(all(d["specialty"] == "Cardiology" for d in data["doctors"]))

    def test_filter_by_location_and_search(self):
        by_loc = self.client.get("/api/doctors?location=Hyderabad").get_json()["data"]["doctors"]
        self.assertTrue(by_loc and all(d["location"] == "Hyderabad" for d in by_loc))
        by_name = self.client.get("/api/doctors?q=verma").get_json()["data"]["doctors"]
        self.assertEqual([d["name"] for d in by_name], ["Dr. Asha Verma"])
        by_spec = self.client.get("/api/doctors?q=derma").get_json()["data"]["doctors"]
        self.assertEqual(by_spec[0]["specialty"], "Dermatology")

    def test_filter_by_availability(self):
        all_count = self.client.get("/api/doctors").get_json()["data"]["count"]
        on_date = self.client.get(f"/api/doctors?date={future_date(2)}").get_json()["data"]["count"]
        self.assertEqual(all_count, on_date)   # every doctor has slots 2 days from now
        far = self.client.get("/api/doctors?date=2000-01-01").get_json()["data"]["count"]
        self.assertEqual(far, 0)
        avail = self.client.get("/api/doctors?available=true").get_json()["data"]["count"]
        self.assertEqual(avail, all_count)

    def test_doctor_detail_and_not_found(self):
        resp = self.client.get("/api/doctors/1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertEqual(data["id"], 1)
        self.assertIn("available_slots", data)
        self.assertEqual(self.client.get("/api/doctors/9999").status_code, 404)

    def test_slots_endpoint_invalid_date(self):
        resp = self.client.get("/api/doctors/1/slots?date=not-a-date")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "INVALID_DATE")


class TestBooking(ApiTestCase):
    def test_successful_booking(self):
        resp = self.book()
        self.assertEqual(resp.status_code, 201)
        appt = resp.get_json()["data"]["appointment"]
        self.assertTrue(appt["appointment_id"].startswith("APT-"))
        self.assertEqual(appt["status"], "BOOKED")
        self.assertEqual(appt["doctor"]["id"], 1)
        # The booked slot must disappear from the free list.
        free = [(s["date"], s["time"]) for s in self.free_slots(1)]
        self.assertNotIn((appt["appointment_date"], appt["appointment_time"]), free)

    def test_booking_unavailable_slot(self):
        first = self.book()
        appt = first.get_json()["data"]["appointment"]
        second = self.book(date=appt["appointment_date"], time=appt["appointment_time"],
                           name="Someone Else", contact="+919111111111")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"]["code"], "SLOT_UNAVAILABLE")

    def test_duplicate_booking_same_patient_same_slot(self):
        first = self.book(name="Same Person", contact="same@example.com")
        appt = first.get_json()["data"]["appointment"]
        again = self.book(date=appt["appointment_date"], time=appt["appointment_time"],
                          name="Same Person", contact="same@example.com")
        self.assertEqual(again.status_code, 409)
        count = self.client.get("/api/appointments?contact=same@example.com").get_json()["data"]["count"]
        self.assertEqual(count, 1)

    def test_simultaneous_booking_only_one_wins(self):
        slot = self.free_slots(1, future_date(4))[0]
        results = []

        def worker(i):
            client = self.app.test_client()
            self.login_patient(client)
            resp = client.post("/api/appointments", json={
                "patient_name": f"Racer {i}", "contact": f"+9190000010{i:02d}", "doctor_id": 1,
                "appointment_date": slot["date"], "appointment_time": slot["time"]})
            results.append(resp.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(201), 1, results)
        self.assertEqual(results.count(409), 7, results)

    def test_database_blocks_double_active_booking(self):
        """Even if app code were buggy, the unique index must refuse a second active booking."""
        import sqlite3
        slot = self.free_slots(1, future_date(5))[0]
        self.assertEqual(self.book(date=slot["date"], time=slot["time"]).status_code, 201)
        conn = database.get_connection(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO appointments (appointment_id, doctor_id, slot_id, patient_name, contact,
                       status, created_at, updated_at) VALUES ('APT-FAKE0001', 1, ?, 'X Y', 'x@example.com',
                       'BOOKED', 'now', 'now')""", (slot["slot_id"],))
        finally:
            conn.close()

    def test_invalid_input(self):
        slot = self.free_slots(1, future_date(3))[0]
        good = {"patient_name": "Test Patient", "contact": "test@example.com", "doctor_id": 1,
                "appointment_date": slot["date"], "appointment_time": slot["time"]}
        cases = [
            ({**good, "patient_name": ""}, 400, "MISSING_FIELD"),
            ({**good, "patient_name": "A"}, 400, "INVALID_FIELD"),
            ({**good, "contact": "not-a-contact"}, 400, "INVALID_FIELD"),
            ({**good, "appointment_date": "2025-13-45"}, 400, "INVALID_DATE"),
            ({**good, "appointment_time": "25:99"}, 400, "INVALID_TIME"),
            ({**good, "doctor_id": "abc"}, 400, "INVALID_FIELD"),
            ({**good, "doctor_id": 9999}, 404, "DOCTOR_NOT_FOUND"),
            ({**good, "appointment_time": "03:17"}, 404, "SLOT_NOT_FOUND"),
            ({**good, "appointment_date": "2000-01-01"}, 404, "SLOT_NOT_FOUND"),
        ]
        for body, status, code in cases:
            with self.subTest(body=body):
                resp = self.client.post("/api/appointments", json=body)
                self.assertEqual(resp.status_code, status)
                self.assertEqual(resp.get_json()["error"]["code"], code)
        missing = {k: v for k, v in good.items() if k != "doctor_id"}
        self.assertEqual(self.client.post("/api/appointments", json=missing).status_code, 400)
        resp = self.client.post("/api/appointments", data="not json", content_type="text/plain")
        self.assertEqual(resp.get_json()["error"]["code"], "INVALID_JSON")

    def test_cannot_book_a_slot_in_the_past(self):
        conn = database.get_connection(self.db_path)
        try:
            conn.execute("INSERT INTO appointment_slots (doctor_id, slot_date, slot_time) VALUES (1,'2000-01-01','09:00')")
        finally:
            conn.close()
        resp = self.book(date="2000-01-01", time="09:00")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "PAST_SLOT")


class TestManagement(ApiTestCase):
    def test_get_and_list_appointments(self):
        appt = self.book(contact="+91 90000-99999").get_json()["data"]["appointment"]
        got = self.client.get(f"/api/appointments/{appt['appointment_id']}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(self.client.get("/api/appointments/APT-NOPE0000").status_code, 404)
        by_contact = self.client.get("/api/appointments?contact=%2B919000099999").get_json()["data"]
        self.assertEqual(by_contact["count"], 1)
        self.assertEqual(self.client.get("/api/appointments").status_code, 400)  # needs a filter

    def test_cancel_and_rebook_same_slot(self):
        appt = self.book().get_json()["data"]["appointment"]
        date, time = appt["appointment_date"], appt["appointment_time"]
        cancel = self.client.patch(f"/api/appointments/{appt['appointment_id']}/cancel")
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.get_json()["data"]["appointment"]["status"], "CANCELLED")
        free = [(s["date"], s["time"]) for s in self.free_slots(1)]
        self.assertIn((date, time), free)
        rebook = self.book(date=date, time=time, name="New Patient", contact="+919222222222")
        self.assertEqual(rebook.status_code, 201)

    def test_cannot_cancel_twice_or_modify_completed(self):
        appt = self.book().get_json()["data"]["appointment"]
        aid = appt["appointment_id"]
        self.assertEqual(self.client.patch(f"/api/appointments/{aid}/cancel").status_code, 200)
        for action in ("cancel", "complete"):
            resp = self.client.patch(f"/api/appointments/{aid}/{action}")
            self.assertEqual(resp.status_code, 409)
        other = self.book(date=appt["appointment_date"], time=appt["appointment_time"]).get_json()["data"]["appointment"]
        oid = other["appointment_id"]
        self.assertEqual(self.client.patch(f"/api/appointments/{oid}/complete").status_code, 200)
        self.assertEqual(self.client.patch(f"/api/appointments/{oid}/cancel").status_code, 409)
        slot = self.free_slots(1, future_date(4))[0]
        resched = self.client.patch(f"/api/appointments/{oid}/reschedule",
                                    json={"appointment_date": slot["date"], "appointment_time": slot["time"]})
        self.assertEqual(resched.status_code, 409)
        # A completed visit still blocks its slot.
        blocked = self.book(date=appt["appointment_date"], time=appt["appointment_time"])
        self.assertEqual(blocked.status_code, 409)

    def test_successful_reschedule(self):
        appt = self.book().get_json()["data"]["appointment"]
        old = (appt["appointment_date"], appt["appointment_time"])
        new_slot = self.free_slots(1, future_date(4))[0]
        resp = self.client.patch(f"/api/appointments/{appt['appointment_id']}/reschedule",
                                 json={"appointment_date": new_slot["date"], "appointment_time": new_slot["time"]})
        self.assertEqual(resp.status_code, 200)
        moved = resp.get_json()["data"]["appointment"]
        self.assertEqual((moved["appointment_date"], moved["appointment_time"]),
                         (new_slot["date"], new_slot["time"]))
        self.assertEqual(moved["status"], "BOOKED")
        free = [(s["date"], s["time"]) for s in self.free_slots(1)]
        self.assertIn(old, free)                                      # old slot released
        self.assertNotIn((new_slot["date"], new_slot["time"]), free)  # new slot taken

    def test_reschedule_to_unavailable_slot(self):
        first = self.book().get_json()["data"]["appointment"]
        taken = self.free_slots(1, future_date(4))[0]
        self.assertEqual(self.book(date=taken["date"], time=taken["time"],
                                   name="Other Person", contact="+919333333333").status_code, 201)
        resp = self.client.patch(f"/api/appointments/{first['appointment_id']}/reschedule",
                                 json={"appointment_date": taken["date"], "appointment_time": taken["time"]})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"]["code"], "SLOT_UNAVAILABLE")
        still = self.client.get(f"/api/appointments/{first['appointment_id']}").get_json()["data"]
        self.assertEqual(still["appointment_date"], first["appointment_date"])  # unchanged
        self.assertEqual(still["appointment_time"], first["appointment_time"])
        same = self.client.patch(f"/api/appointments/{first['appointment_id']}/reschedule",
                                 json={"appointment_date": first["appointment_date"],
                                       "appointment_time": first["appointment_time"]})
        self.assertEqual(same.get_json()["error"]["code"], "SAME_SLOT")


class TestProvider(ApiTestCase):
    def test_provider_listing_filters_and_stats(self):
        listing = self.client.get("/api/providers/1/appointments").get_json()["data"]
        self.assertGreaterEqual(listing["count"], 4)   # seeded sample appointments
        statuses = {a["status"] for a in listing["appointments"]}
        self.assertEqual(statuses, {"BOOKED", "COMPLETED", "CANCELLED"})
        booked = self.client.get("/api/providers/1/appointments?status=booked").get_json()["data"]
        self.assertTrue(all(a["status"] == "BOOKED" for a in booked["appointments"]))
        day = future_date(1)
        on_day = self.client.get(f"/api/providers/1/appointments?date={day}").get_json()["data"]
        self.assertTrue(on_day["appointments"] and all(a["appointment_date"] == day for a in on_day["appointments"]))
        self.assertEqual(self.client.get("/api/providers/1/appointments?status=bogus").status_code, 400)
        self.assertEqual(self.client.get("/api/providers/9999/appointments").status_code, 404)

        before = self.client.get(f"/api/providers/1/stats?date={future_date(3)}").get_json()["data"]
        self.assertEqual(before["by_status"]["BOOKED"] + before["by_status"]["COMPLETED"]
                         + before["by_status"]["CANCELLED"], before["total_appointments"])
        self.assertEqual(before["appointments_on_date"], 0)
        self.assertEqual(len(before["daily_counts"]), 7)
        self.book(doctor_id=1)   # books on day+3
        after = self.client.get(f"/api/providers/1/stats?date={future_date(3)}").get_json()["data"]
        self.assertEqual(after["appointments_on_date"], 1)
        self.assertEqual(after["by_status"]["BOOKED"], before["by_status"]["BOOKED"] + 1)
        self.assertEqual(after["upcoming_booked"], before["upcoming_booked"] + 1)
        self.assertGreater(after["next_7_days"]["utilization_percent"],
                           before["next_7_days"]["utilization_percent"])


class TestIntegration(ApiTestCase):
    def test_recommender_endpoints_use_same_data(self):
        spec = self.client.get("/api/specialties").get_json()["data"]["specialties"]
        self.assertIn("Cardiology", spec)
        data = self.client.get("/api/integration/doctors?specialty=Cardiology&slots_per_doctor=2").get_json()["data"]
        self.assertGreaterEqual(data["count"], 1)
        for doc in data["doctors"]:
            self.assertEqual(len(doc["next_slots"]), 2)
        import recommender
        conn = database.get_connection(self.db_path)
        try:
            self.assertEqual(recommender.get_specialties(conn), spec)
            self.assertTrue(recommender.suggest_doctors(conn, specialty="Cardiology", limit=1))
        finally:
            conn.close()


class TestAuthAndAgent(ApiTestCase):
    def test_register_login_me_logout(self):
        body = {"name": "Demo User", "email": "Demo@Example.com", "password": "password123"}
        self.assertEqual(self.client.post("/api/register", json=body).status_code, 201)
        self.assertEqual(self.client.post("/api/register", json=body).status_code, 409)  # duplicate email
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        bad = self.client.post("/api/login", json={"email": "demo@example.com", "password": "wrong-pass"})
        self.assertEqual(bad.status_code, 401)
        ok = self.client.post("/api/login", json={"email": "demo@example.com", "password": "password123"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.get("/api/me").get_json()["data"]["user"]["email"], "demo@example.com")
        short = self.client.post("/api/register", json={**body, "email": "b@example.com", "password": "short"})
        self.assertEqual(short.status_code, 400)

    def _agent(self, text):
        return self.client.post("/api/agent", json={"message": text}).get_json()["data"]

    def test_agent_filters_and_whole_word_matching(self):
        r = self._agent("Find a cardiologist in Hyderabad")
        self.assertTrue(r["results"])
        self.assertTrue(all(d["specialty"] == "Cardiology" and d["location"] == "Hyderabad" for d in r["results"]))
        # "appointment" contains the letters "ent": it must NOT be read as the ENT specialty.
        r = self._agent("find available appointment slots")
        self.assertGreater(len({d["specialty"] for d in r["results"]}), 1)
        self.assertIn("experience_years", r["results"][0])
        self.assertEqual(self.client.post("/api/agent", json={"message": ""}).status_code, 400)


class TestRolesAndAdmin(ApiTestCase):
    def test_booking_requires_login(self):
        anon = self.app.test_client()
        slot = self.free_slots(1, future_date(3))[0]
        resp = anon.post("/api/appointments", json={
            "patient_name": "No Login", "contact": "a@example.com", "doctor_id": 1,
            "appointment_date": slot["date"], "appointment_time": slot["time"]})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error"]["code"], "NOT_LOGGED_IN")
        self.assertIn(slot["time"], [s["time"] for s in self.free_slots(1, slot["date"])])  # slot untouched

    def test_two_login_types_are_separate(self):
        admin_on_patient = self.app.test_client().post("/api/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        self.assertEqual(admin_on_patient.status_code, 403)
        patient_on_admin = self.app.test_client().post("/api/admin/login", json={"email": "patient@example.com", "password": "password123"})
        self.assertEqual(patient_on_admin.status_code, 403)
        admin = self.app.test_client()
        self.login_admin(admin)
        self.assertEqual(admin.get("/api/me").get_json()["data"]["user"]["role"], "admin")
        # public registration can never create an admin
        reg = self.app.test_client().post("/api/register", json={"name": "Sneaky", "email": "s@example.com", "password": "password123", "role": "admin"})
        self.assertEqual(reg.get_json()["data"]["user"]["role"], "patient")

    def test_admin_routes_are_protected(self):
        anon = self.app.test_client()
        self.assertEqual(anon.post("/api/admin/doctors", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/admin/doctors", json={}).status_code, 403)   # a patient
        self.assertEqual(self.client.delete("/api/admin/slots/1").status_code, 403)

    def test_admin_manages_doctors_and_slots(self):
        admin = self.app.test_client()
        self.login_admin(admin)
        doc = {"name": "Dr. Test Admin", "specialty": "Oncology", "hospital": "Test Hospital", "location": "Pune",
               "experience_years": 7, "consultation_fee": 650, "description": "Fictional."}
        created = admin.post("/api/admin/doctors", json=doc)
        self.assertEqual(created.status_code, 201)
        doc_id = created.get_json()["data"]["doctor"]["id"]
        self.assertEqual(admin.post("/api/admin/doctors", json={**doc, "consultation_fee": "abc"}).status_code, 400)
        self.assertEqual(admin.put(f"/api/admin/doctors/{doc_id}", json={**doc, "location": "Nagpur"}).get_json()["data"]["doctor"]["location"], "Nagpur")
        self.assertEqual(self.free_slots(doc_id), [])   # new doctor starts with no slots (and none are auto-created)
        day = future_date(2)
        added = admin.post(f"/api/admin/doctors/{doc_id}/slots", json={"date": day, "times": ["09:00", "09:30", "09:00"]})
        self.assertEqual(added.get_json()["data"]["added"], 2)
        self.assertEqual(admin.post(f"/api/admin/doctors/{doc_id}/slots", json={"date": "2000-01-01", "times": ["09:00"]}).status_code, 400)
        self.assertEqual(admin.post(f"/api/admin/doctors/{doc_id}/slots", json={"date": day, "times": ["99:99"]}).status_code, 400)
        self.assertEqual(len(self.free_slots(doc_id, day)), 2)   # patients see them
        # a booked slot cannot be deleted; a free one can
        self.assertEqual(self.book(doc_id, day, "09:00").status_code, 201)
        listing = admin.get(f"/api/admin/doctors/{doc_id}/slots").get_json()["data"]["slots"]
        self.assertEqual({s["time"]: s["booked"] for s in listing}, {"09:00": True, "09:30": False})
        by_time = {s["time"]: s["slot_id"] for s in listing}
        self.assertEqual(admin.delete(f"/api/admin/slots/{by_time['09:00']}").status_code, 409)
        self.assertEqual(admin.delete(f"/api/admin/slots/{by_time['09:30']}").status_code, 200)
        self.assertEqual(admin.delete(f"/api/admin/doctors/{doc_id}").status_code, 409)   # has an appointment
        empty = admin.post("/api/admin/doctors", json=doc).get_json()["data"]["doctor"]["id"]
        self.assertEqual(admin.delete(f"/api/admin/doctors/{empty}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/doctors/{empty}").status_code, 404)

    def test_restart_keeps_admin_deletions(self):
        admin = self.app.test_client()
        self.login_admin(admin)
        slot = admin.get("/api/admin/doctors/2/slots").get_json()["data"]["slots"][-1]
        self.assertEqual(admin.delete(f"/api/admin/slots/{slot['slot_id']}").status_code, 200)
        create_app(self.db_path)   # "restart the server"
        left = [(s["date"], s["time"]) for s in admin.get("/api/admin/doctors/2/slots").get_json()["data"]["slots"]]
        self.assertNotIn((slot["date"], slot["time"]), left)

    def test_old_database_without_role_column_is_upgraded(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.executescript("DROP TABLE users; CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
                           " email TEXT NOT NULL COLLATE NOCASE UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);")
        conn.close()
        app = create_app(self.db_path)   # startup should add the column and the admin
        c = app.test_client()
        self.assertEqual(c.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code, 200)


class TestProfileAndPasswords(ApiTestCase):
    """self.client is logged in as patient@example.com / password123."""

    def test_update_details_and_email_rules(self):
        ok = self.client.put("/api/profile", json={"name": "New Name", "email": "patient@example.com"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.get("/api/me").get_json()["data"]["user"]["name"], "New Name")
        # changing the email needs the current password
        no_pw = self.client.put("/api/profile", json={"name": "New Name", "email": "new@example.com"})
        self.assertEqual(no_pw.get_json()["error"]["code"], "WRONG_PASSWORD")
        good = self.client.put("/api/profile", json={"name": "New Name", "email": "NEW@example.com", "current_password": "password123"})
        self.assertEqual(good.get_json()["data"]["user"]["email"], "new@example.com")
        # an email that another account already uses is refused
        other = self.app.test_client()
        self.login_patient(other, "taken@example.com")
        clash = self.client.put("/api/profile", json={"name": "New Name", "email": "taken@example.com", "current_password": "password123"})
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(self.client.put("/api/profile", json={"name": "A", "email": "x"}).status_code, 400)
        self.assertEqual(self.app.test_client().put("/api/profile", json={"name": "Abc", "email": "a@b.co"}).status_code, 401)

    def test_change_password(self):
        bad = self.client.post("/api/profile/password", json={"current_password": "nope-nope", "new_password": "brand-new-pass"})
        self.assertEqual(bad.status_code, 403)
        self.assertEqual(self.client.post("/api/profile/password", json={"current_password": "password123", "new_password": "short"}).status_code, 400)
        self.assertEqual(self.client.post("/api/profile/password", json={"current_password": "password123", "new_password": "password123"}).status_code, 400)
        self.assertEqual(self.client.post("/api/profile/password", json={"current_password": "password123", "new_password": "brand-new-pass"}).status_code, 200)
        fresh = self.app.test_client()
        self.assertEqual(fresh.post("/api/login", json={"email": "patient@example.com", "password": "password123"}).status_code, 401)
        self.assertEqual(fresh.post("/api/login", json={"email": "patient@example.com", "password": "brand-new-pass"}).status_code, 200)

    def _forgot(self, email="patient@example.com"):
        return self.app.test_client().post("/api/forgot-password", json={"email": email}).get_json()["data"]

    def test_forgot_and_reset_password(self):
        anon = self.app.test_client()
        code = self._forgot()["demo_reset_code"]
        self.assertEqual(len(code), 6)
        weak = anon.post("/api/reset-password", json={"email": "patient@example.com", "code": code, "new_password": "short"})
        self.assertEqual(weak.status_code, 400)
        done = anon.post("/api/reset-password", json={"email": "patient@example.com", "code": code, "new_password": "reset-pass-99"})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(anon.post("/api/login", json={"email": "patient@example.com", "password": "reset-pass-99"}).status_code, 200)
        # a code works only once
        again = self.app.test_client().post("/api/reset-password", json={"email": "patient@example.com", "code": code, "new_password": "another-pass-1"})
        self.assertEqual(again.get_json()["error"]["code"], "INVALID_RESET")

    def test_reset_code_security(self):
        anon = self.app.test_client()
        # unknown email: same message, no code leaked
        unknown = self._forgot("nobody@example.com")
        self.assertNotIn("demo_reset_code", unknown)
        self.assertEqual(unknown["message"], self._forgot()["message"])
        # too many wrong guesses lock the code, even if the right one is then used
        code = self._forgot()["demo_reset_code"]
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            r = anon.post("/api/reset-password", json={"email": "patient@example.com", "code": wrong, "new_password": "reset-pass-99"})
            self.assertEqual(r.status_code, 400)
        locked = anon.post("/api/reset-password", json={"email": "patient@example.com", "code": code, "new_password": "reset-pass-99"})
        self.assertEqual(locked.get_json()["error"]["code"], "INVALID_RESET")
        # a newer code replaces older ones
        old = self._forgot()["demo_reset_code"]
        new = self._forgot()["demo_reset_code"]
        if old != new:
            r = anon.post("/api/reset-password", json={"email": "patient@example.com", "code": old, "new_password": "reset-pass-99"})
            self.assertEqual(r.status_code, 400)
        # expired code
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE password_resets SET expires_at = '2000-01-01T00:00:00'")
        conn.commit(); conn.close()
        expired = anon.post("/api/reset-password", json={"email": "patient@example.com", "code": new, "new_password": "reset-pass-99"})
        self.assertEqual(expired.get_json()["error"]["code"], "INVALID_RESET")

    def test_admin_cannot_use_forgot_password(self):
        data = self._forgot(ADMIN_EMAIL)
        self.assertNotIn("demo_reset_code", data)
        r = self.app.test_client().post("/api/reset-password", json={"email": ADMIN_EMAIL, "code": "123456", "new_password": "hacked-pass-1"})
        self.assertEqual(r.status_code, 400)
        admin = self.app.test_client()
        self.login_admin(admin)   # original admin password still works

    def test_demo_code_can_be_hidden(self):
        os.environ["DEMO_SHOW_RESET_CODE"] = "0"
        try:
            app = create_app(self.db_path)
            data = app.test_client().post("/api/forgot-password", json={"email": "patient@example.com"}).get_json()["data"]
            self.assertNotIn("demo_reset_code", data)
        finally:
            del os.environ["DEMO_SHOW_RESET_CODE"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
