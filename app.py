import json
import os
from datetime import datetime
from typing import List, Dict

import streamlit as st
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from openai import OpenAI


load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing env var: {name}")
    return value


def sheets_client():
    email = require_env("GOOGLE_SERVICE_ACCOUNT_EMAIL")
    private_key = require_env("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": email,
            "private_key": private_key,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=creds)


def _normalize_header_map(header: List[str]) -> Dict[str, int]:
    return {name.strip().lower(): i for i, name in enumerate(header)}


def _cell(row: List[str], idx: Dict[str, int], key: str) -> str:
    pos = idx.get(key)
    if pos is None or len(row) <= pos:
        return ""
    return row[pos]


def read_recent_workouts(
    service, sheet_id: str, tab_name: str, limit: int = 10
) -> List[Dict[str, str]]:
    range_name = f"{tab_name}!A:H"
    resp = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_name)
        .execute()
    )
    rows = resp.get("values", [])
    if not rows:
        return []

    header = rows[0]
    body = rows[1:]
    idx = _normalize_header_map(header)
    workouts = []

    # Current live sheet format:
    # Week | Date | Day Type | Exercise | Set | ... | Notes
    if {"date", "day type", "exercise", "set"}.issubset(set(idx.keys())):
        for row in body:
            date = _cell(row, idx, "date").strip()
            day_type = _cell(row, idx, "day type").strip()
            exercise = _cell(row, idx, "exercise").strip()
            set_num = _cell(row, idx, "set").strip()
            week = _cell(row, idx, "week").strip()
            sheet_notes = _cell(row, idx, "notes").strip()

            # Ignore malformed/freeform trailing rows and prior AI rows.
            if not day_type or not exercise:
                continue
            if day_type.lower() in {"ai plan", "ai_plan"}:
                continue

            workout = f"{day_type}: {exercise}"
            if set_num:
                workout = f"{workout} (set {set_num})"

            notes_parts = []
            if week:
                notes_parts.append(f"week {week}")
            if sheet_notes:
                notes_parts.append(sheet_notes)
            notes = " | ".join(notes_parts)

            workouts.append({"date": date, "workout": workout, "notes": notes})
    else:
        # Backward-compatible legacy format:
        # date | type | workout | notes | ai_output
        for row in body:
            row_type = _cell(row, idx, "type").strip()
            if row_type != "workout_log":
                continue
            workouts.append(
                {
                    "date": _cell(row, idx, "date").strip(),
                    "workout": _cell(row, idx, "workout").strip(),
                    "notes": _cell(row, idx, "notes").strip(),
                }
            )

    return workouts[-limit:]


def _parse_response_json(content: str) -> Dict[str, str]:
    text = (content or "{}").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)


def generate_advice_and_next_workout(workouts: List[Dict[str, str]]) -> Dict[str, str]:
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-2025-04-14")
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))

    prompt = (
        "You are a practical fitness coach. Analyze these recent workouts and respond with JSON only.\n"
        "Return object keys: tips (string), next_workout (string).\n"
        f"Recent workouts: {json.dumps(workouts)}"
    )

    response = client.responses.create(
        model=model,
        temperature=0.4,
        input=[
            {
                "role": "system",
                "content": "You are an practical fitness coach.  You have the demeanor of a gruff high school football coach. Return valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
    )
    parsed = _parse_response_json(response.output_text)
    return {
        "tips": parsed.get("tips", "").strip(),
        "next_workout": parsed.get("next_workout", "").strip(),
    }


def append_ai_output(service, sheet_id: str, tab_name: str, tips: str, next_workout: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header_resp = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{tab_name}!A1:Z1")
        .execute()
    )
    header = (header_resp.get("values", [[]]) or [[]])[0]
    idx = _normalize_header_map(header)

    if {"date", "day type", "exercise", "set"}.issubset(set(idx.keys())):
        values = [
            ["", now, "AI Plan", "Tips", tips],
            ["", now, "AI Plan", "Next Workout", next_workout],
        ]
    else:
        values = [[now, "ai_plan", "", tips, next_workout]]

    (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A:E",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )


st.set_page_config(page_title="Workout Helper", page_icon="💪")
st.title("Workout Helper")
st.caption(
    "Workflow: log workouts directly in Google Sheets, then click one button here to get tips + next workout."
)

st.markdown(
    """
Supported sheet formats in row 1:
`date | type | workout | notes | ai_output`
or
`Week | Date | Day Type | Exercise | Set`
"""
)

if st.button("Analyze recent workouts and write next plan"):
    try:
        sheet_id = require_env("GOOGLE_SHEETS_ID")
        tab_name = os.getenv("SHEET_TAB", "Workouts")
        service = sheets_client()
        workouts = read_recent_workouts(service, sheet_id, tab_name, limit=10)
        if not workouts:
            st.warning("No workout_log rows found yet.")
        else:
            ai = generate_advice_and_next_workout(workouts)
            append_ai_output(
                service,
                sheet_id,
                tab_name,
                tips=ai["tips"],
                next_workout=ai["next_workout"],
            )
            st.success("Saved AI tips + next workout to Google Sheets.")
            st.subheader("Tips")
            st.write(ai["tips"])
            st.subheader("Next Workout")
            st.write(ai["next_workout"])
    except Exception as exc:
        st.error(str(exc))
