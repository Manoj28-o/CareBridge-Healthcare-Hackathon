
"""
Rule-based Appointment Agent for Tech's MediSlot.
Uses the existing database connection and recommender.
No AI API key or external network calls required.
"""

import re

import recommender


def understand_request(message, conn):
    """Identify a simple doctor-search request."""
    if not isinstance(message, str) or not message.strip():
        return {"action": "help"}

    text = message.strip().lower()

    specialties = recommender.get_specialties(conn)
    locations = recommender.get_locations(conn)

    
    specialty_aliases = {
        "cardiology": ["cardiology", "cardiologist", "heart doctor"],
        "dermatology": ["dermatology", "dermatologist", "skin doctor"],
        "general medicine": ["general medicine", "general physician",
                             "general doctor", "family doctor"],
        "orthopedics": ["orthopedics", "orthopedic", "bone doctor"],
        "neurology": ["neurology", "neurologist", "brain specialist"],
        "pediatrics": ["pediatrics", "pediatrician", "child doctor"],
        "gynecology": ["gynecology", "gynecologist"],
    }

    def mentions(phrase):
        # Whole-word match (plural "s" allowed). Plain "in" would wrongly find
        # the specialty "ENT" inside words like "appointment".
        return re.search(r"\b" + re.escape(phrase) + r"s?\b", text) is not None

    specialty = next(
        (
            item for item in specialties
            if any(mentions(p) for p in [item.lower(),
                                         *specialty_aliases.get(item.lower(), [])])
        ),
        None,
    )
    location = next(
        (item for item in locations if mentions(item.lower())), None
    )

    if any(word in text for word in (
        "slot", "available", "availability", "appointment"
    )):
        action = "find_slots"
    elif any(word in text for word in (
        "find", "search", "doctor", "specialist"
    )):
        action = "find_doctors"
    else:
        action = "help"

    return {
        "action": action,
        "specialty": specialty,
        "location": location,
    }


def run_agent(message, conn):
    """Search for doctors and available slots without modifying data."""
    intent = understand_request(message, conn)

    if intent["action"] == "help":
        return {
            "success": True,
            "reply": (
                "Try asking me to find a doctor by specialty or location, "
                "or to find available appointment slots."
            ),
            "actions": [
                {"tool": "understand_request", "status": "completed"}
            ],
            "results": [],
        }

    doctors = recommender.get_doctors_with_slots(
        conn,
        specialty=intent["specialty"],
        location=intent["location"],
        available_only=True,
        slots_per_doctor=5,
    )

    # Return the full doctor records so the page can reuse its normal doctor card.
    results = [dict(doctor) for doctor in doctors]

    return {
        "success": True,
        "reply": f"I found {len(results)} matching doctor(s).",
        "actions": [
            {"tool": "understand_request", "status": "completed"},
            {"tool": "search_doctors_and_slots", "status": "completed"},
        ],
        "results": results,
    }