# Healthcare Appointment Backend (Hackathon Demo)

Flask + SQLite REST API for finding doctors, booking, cancelling and rescheduling
appointments, plus provider dashboard statistics.

> **Demo only.** All doctors and patients are fictional. It does not connect to
> real hospitals, does not send real SMS/email, and gives no medical advice.

## Project structure

```
healthcare-hackathon/
├── app.py              Flask app + all API routes
├── database.py         SQLite schema, seed data, shared queries
├── recommender.py      Small read-only interface for the recommendation teammate
├── requirements.txt    Flask, Flask-CORS
├── README.md
├── templates/          (empty, reserved for later)
├── static/             (empty, reserved for later)
└── tests/
    ├── __init__.py
    └── test_api.py     21 automated tests (use a temporary database)
```

**Coexisting with your existing `main.py`:** none of these files is called
`main.py`, so nothing is overwritten. Start this API with `python app.py`.
Your `main.py` keeps working as before. If you later want `main.py` to start the
API, add: `from app import create_app` then `create_app().run(port=5000)`.

## Setup on Windows + VS Code

1. Open your project folder in VS Code (**File > Open Folder**). Put the files above in it.
2. Open a terminal (**Terminal > New Terminal**, PowerShell) and create a virtual environment:
   ```
   python -m venv venv
   venv\Scripts\Activate.ps1
   ```
   If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use Command Prompt and run `venv\Scripts\activate.bat`.
   You should see `(venv)` at the start of the prompt.
3. Install dependencies: `pip install -r requirements.txt`
4. Create and seed the database (optional, the server also does this automatically):
   `python database.py`
   This creates `healthcare.db`. Running it again never duplicates data.
   To start fresh, stop the server and delete `healthcare.db`.
5. Start the server: `python app.py`  (it listens on http://127.0.0.1:5000)
6. Test it: open http://127.0.0.1:5000/api/health in a browser, or in a second terminal:
   ```
   curl.exe http://127.0.0.1:5000/api/health
   curl.exe "http://127.0.0.1:5000/api/doctors?specialty=Cardiology"
   ```
7. Run the automated tests: `python -m unittest discover -s tests -v`

## Response format

```json
{ "success": true,  "data": { ... } }
{ "success": false, "error": { "code": "SLOT_UNAVAILABLE", "message": "...", "field": "optional" } }
```
Dates are `YYYY-MM-DD`, times are 24-hour `HH:MM`. Statuses: `BOOKED`, `CANCELLED`, `COMPLETED`.

## Endpoints

| Method | URL | Purpose |
|---|---|---|
| GET | `/api/health` | Server check |
| GET | `/api/doctors` | List/search/filter doctors |
| GET | `/api/doctors/<id>` | Doctor details + open slots |
| GET | `/api/doctors/<id>/slots` | Open slots (optional `?date=`) |
| POST | `/api/appointments` | Book |
| GET | `/api/appointments/<appointment_id>` | One appointment |
| GET | `/api/appointments` | List by `contact`, `appointment_id` or `doctor_id` |
| PATCH | `/api/appointments/<appointment_id>/cancel` | Cancel |
| PATCH | `/api/appointments/<appointment_id>/reschedule` | Move to another slot |
| PATCH | `/api/appointments/<appointment_id>/complete` | Mark completed |
| GET | `/api/providers/<id>/appointments` | Provider list (`date`, `status`) |
| GET | `/api/providers/<id>/stats` | Provider statistics |
| GET | `/api/specialties`, `/api/locations` | Lookup lists |
| GET | `/api/integration/doctors` | Doctors + next slots in one call |

### GET /api/doctors
Query params (all optional): `q` (name or specialty text), `specialty` (exact, case-insensitive),
`location` (partial), `available=true` (has any upcoming free slot), `date=YYYY-MM-DD` (free slot on that date).
```json
{"success": true, "data": {"count": 2, "doctors": [
  {"id": 1, "name": "Dr. Asha Verma", "specialty": "Cardiology", "hospital": "Riverside Heart Institute",
   "location": "Hyderabad", "experience_years": 15, "consultation_fee": 800,
   "description": "Fictional doctor. ...", "available_slots_count": 37}]}}
```

### GET /api/doctors/<id>
Same fields as above plus `available_slots: [{slot_id, date, time}]`. Unknown ID gives `404 DOCTOR_NOT_FOUND`.

### GET /api/doctors/<id>/slots?date=2026-10-06
```json
{"success": true, "data": {"doctor_id": 1, "count": 6, "slots": [
  {"slot_id": 19, "date": "2026-10-06", "time": "09:00"}]}}
```
Errors: `400 INVALID_DATE`, `404 DOCTOR_NOT_FOUND`.

### POST /api/appointments
Body (JSON):
```json
{"patient_name": "Demo Patient", "contact": "demo.patient@example.com", "doctor_id": 1,
 "appointment_date": "2026-10-06", "appointment_time": "09:00", "reason": "Routine check-up"}
```
`reason` is optional. `contact` is an email or a phone number (7-15 digits).
Success `201`:
```json
{"success": true, "data": {"message": "Appointment booked successfully.", "appointment": {
  "appointment_id": "APT-B7BEEE55", "status": "BOOKED", "patient_name": "Demo Patient",
  "contact": "demo.patient@example.com", "reason": "Routine check-up",
  "doctor": {"id": 1, "name": "Dr. Asha Verma", "specialty": "Cardiology",
             "hospital": "Riverside Heart Institute", "location": "Hyderabad"},
  "appointment_date": "2026-10-06", "appointment_time": "09:00",
  "created_at": "2026-10-03T09:38:26", "updated_at": "2026-10-03T09:38:26",
  "cancelled_at": null, "completed_at": null}}}
```
Errors:

| Status | code | When |
|---|---|---|
| 400 | `INVALID_JSON` | Body is not a JSON object |
| 400 | `MISSING_FIELD` / `INVALID_FIELD` | Bad or missing field (`error.field` names it) |
| 400 | `INVALID_DATE` / `INVALID_TIME` | Not a real date or time |
| 400 | `PAST_SLOT` | Slot already started |
| 404 | `DOCTOR_NOT_FOUND` | Unknown `doctor_id` |
| 404 | `SLOT_NOT_FOUND` | Doctor has no slot at that date and time |
| 409 | `SLOT_UNAVAILABLE` | Slot already booked (also returned to the loser of a simultaneous race) |

### GET /api/appointments/<appointment_id>
Returns the same `appointment` object. Unknown ID gives `404 APPOINTMENT_NOT_FOUND`.

### GET /api/appointments
Needs at least one of `contact`, `appointment_id`, `doctor_id` (otherwise `400 MISSING_FILTER`).
Optional: `date`, `status`. Returns `{"count": n, "appointments": [ ... ]}`.
Phone numbers are matched after removing spaces and dashes; in a URL write `+` as `%2B`.

### PATCH .../cancel
No body. Only `BOOKED` appointments can be cancelled, and the slot becomes free again.
Returns `{"message": "Appointment cancelled.", "appointment": {...status: "CANCELLED"}}`.
Error: `409 INVALID_STATUS` if already cancelled or completed.

### PATCH .../reschedule
Body: `{"appointment_date": "2026-10-06", "appointment_time": "09:30"}` (same doctor).
Only `BOOKED` appointments. The old slot is released, the new one is taken, all in one transaction.
Errors: `409 INVALID_STATUS`, `409 SLOT_UNAVAILABLE`, `404 SLOT_NOT_FOUND`, `400 SAME_SLOT`, `400 PAST_SLOT`.
On any error the appointment stays exactly where it was.

### PATCH .../complete
No body. Only `BOOKED` appointments (demo simplification: allowed at any time). The slot stays blocked.
Error: `409 INVALID_STATUS`.

### GET /api/providers/<id>/appointments
Optional `date` and `status`. Returns `{"doctor_id", "doctor_name", "count", "appointments": [...]}` sorted by date and time.

### GET /api/providers/<id>/stats
Optional `date` (defaults to today) for the daily count.
```json
{"success": true, "data": {"doctor_id": 1, "doctor_name": "Dr. Asha Verma", "date": "2026-10-03",
 "appointments_on_date": 0, "total_appointments": 6,
 "by_status": {"BOOKED": 3, "CANCELLED": 2, "COMPLETED": 1},
 "upcoming_booked": 3, "cancellation_rate_percent": 33.3,
 "daily_counts": [{"date": "2026-10-03", "count": 0}, {"date": "2026-10-04", "count": 2}],
 "next_7_days": {"total_slots": 42, "booked_slots": 3, "utilization_percent": 7.1}}}
```
`daily_counts` has 7 entries (cancelled appointments are not counted).

## For the frontend teammate

Open the HTML with VS Code **Live Server** (usually http://127.0.0.1:5500) or any
`localhost` port. CORS already allows every `localhost` / `127.0.0.1` port and
pages opened directly from a file. For a deployed site, set the environment
variable `CORS_ORIGINS=https://your-site.com` before starting the API.

```javascript
const API = "http://127.0.0.1:5000/api";

async function bookAppointment(form) {
  const response = await fetch(`${API}/appointments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },   // required
    body: JSON.stringify({
      patient_name: form.name,
      contact: form.contact,            // email or phone
      doctor_id: Number(form.doctorId),
      appointment_date: form.date,      // "YYYY-MM-DD"
      appointment_time: form.time,      // "HH:MM"
      reason: form.reason,              // optional
    }),
  });
  const result = await response.json();
  if (!result.success) {
    alert(result.error.message);        // show this text to the user
    return null;
  }
  return result.data.appointment;       // includes appointment_id
}

// Cancel:
fetch(`${API}/appointments/${id}/cancel`, { method: "PATCH" });
// Reschedule:
fetch(`${API}/appointments/${id}/reschedule`, {
  method: "PATCH", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ appointment_date: "2026-10-06", appointment_time: "09:30" }) });
```
Always check `result.success`. A booking attempt that loses a race returns `409 SLOT_UNAVAILABLE`:
show the message and reload `/api/doctors/<id>/slots`.

## For the recommendation teammate

Do not create another database. Read the same one, either way:

1. Over HTTP: `GET /api/integration/doctors?specialty=Cardiology&slots_per_doctor=3`
   returns doctors (same fields as `/api/doctors`) each with `next_slots: [{slot_id, date, time}]`.
   Also `GET /api/specialties` and `GET /api/locations` for valid values.
   To book, the recommendation UI calls `POST /api/appointments` with a doctor's `id` and one of its slots.
2. In Python:
   ```python
   import database, recommender
   conn = database.get_connection()
   try:
       docs = recommender.get_doctors_with_slots(conn, specialty="Cardiology", location="Hyderabad")
       soonest = recommender.suggest_doctors(conn, specialty="Cardiology", limit=3)
   finally:
       conn.close()
   ```
   `recommender.suggest_doctors` is only a placeholder (earliest free slot first). Replace it
   with your own logic. No API key is needed, and the backend never calls any AI service.

Contract: do not rename `id`, `specialty`, `location`, `date`, `time`, `slot_id`;
treat `specialty` values from `/api/specialties` as the allowed list.

## Testing

`python -m unittest discover -s tests -v`

Each test builds its own temporary database, so `healthcare.db` is never touched.
Manual check of the booking life cycle with curl.exe: book (201), book the same slot again (409),
cancel (200), book the same slot again (201), reschedule to another free time (200).

## Safety notes

- All SQL uses `?` / named parameters; user text is never pasted into SQL.
- Input is validated on the server. Database errors return a generic message; details go only to the server log.
- No passwords are stored. Contacts in the sample data are fake (`example.com`, `+9190000000xx`).
- Double-booking is blocked twice: a transaction (`BEGIN IMMEDIATE`) and a database unique index on active appointments per slot.
- Appointment listing requires a filter so the API cannot dump every patient record.
- This demo has no login. Anyone who knows an appointment ID can cancel it. Real deployments would need authentication.

## Accounts and login (added)

- **Patients** register on the "Login / Register" page and use the *Patient login* tab. Booking an appointment requires a patient (or any logged-in) account.
- **Admin** uses the *Admin login* tab. A demo admin is created automatically on first start:
  `admin@medislot.demo` / `Admin@12345`. This is for the LOCAL DEMO only. Choose your own before starting the server:
  PowerShell: `$env:ADMIN_EMAIL="you@example.com"; $env:ADMIN_PASSWORD="a-strong-password"; python app.py`
  (the admin is only created if no admin exists yet, so set these before the first run, or delete `healthcare.db`).
- Set `SECRET_KEY` the same way for anything beyond a local demo.
- The admin panel can add, edit and delete doctors and add or delete free slots. Doctors with appointment records and slots with booking history cannot be deleted.
- Open the site at http://127.0.0.1:5000 (served by Flask) so the login cookie works.

| Method | URL | Who |
|---|---|---|
| POST | `/api/register`, `/api/login` | anyone (patients) |
| POST | `/api/admin/login` | admins |
| GET | `/api/me`, POST `/api/logout` | logged in |
| POST | `/api/appointments` | **logged in** |
| POST / PUT / DELETE | `/api/admin/doctors`, `/api/admin/doctors/<id>` | admin |
| GET / POST | `/api/admin/doctors/<id>/slots` | admin |
| DELETE | `/api/admin/slots/<slot_id>` | admin |

## Profile and password reset (added)

- Once signed in, the top-right account button opens **My profile** (instead of the login page): change your name and email, and change your password. Changing the email, or the password, asks for your current password.
- **Forgot password?** is on the Patient login tab. It makes a 6-digit one-time code that is valid for 15 minutes and locks after 5 wrong tries. Only a hash of the code is stored.
- **Demo limitation:** this project sends no real email, so the code is shown on screen (and printed in the server console). Set `DEMO_SHOW_RESET_CODE=0` to stop showing it on screen. A real deployment must email the code and never return it.
- Admin accounts cannot use forgot-password. Reset an admin by setting `ADMIN_PASSWORD` and deleting `healthcare.db` (this erases demo data).

| Method | URL | Who |
|---|---|---|
| PUT | `/api/profile` `{name, email, current_password?}` | logged in |
| POST | `/api/profile/password` `{current_password, new_password}` | logged in |
| POST | `/api/forgot-password` `{email}` | anyone |
| POST | `/api/reset-password` `{email, code, new_password}` | anyone |

## Landing page (added)

The site now opens on an animated landing page (`templates/index.html`, section `view-home`) with a Login button at the top right and a Login / Create account call-to-action in the middle of the page. Illustrations are in `static/images/` (`hero.svg`, `care.svg`). The statistics and the specialty strip use live data from `/api/doctors`, `/api/specialties` and `/api/locations`. Animations switch off automatically for visitors who prefer reduced motion.
