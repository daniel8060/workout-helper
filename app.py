import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple

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


def _normalize_key(key: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (key or "").strip().lower())
    return cleaned.strip("_")


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


def _parse_response_json(content: str) -> Dict[str, Any]:
    text = (content or "{}").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)


def generate_advice_and_next_workout(workouts: List[Dict[str, str]]) -> Dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-2025-04-14")
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))

    prompt = (
        "You are a practical fitness coach. Analyze these recent workouts and respond with JSON only.\n"
        "Return object keys:\n"
        "- tips (string)\n"
        "- workout_plan (array of rows)\n"
        "Each workout_plan row must include exactly these keys:\n"
        "week, date, day_type, exercise, set, weight_lbs, reps, notes\n"
        "Use date format YYYY-MM-DD. Keep all values as strings.\n"
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
    plan_rows = parsed.get("workout_plan", [])
    if not isinstance(plan_rows, list):
        plan_rows = []

    normalized_rows: List[Dict[str, str]] = []
    for row in plan_rows:
        if not isinstance(row, dict):
            continue
        nrow = {_normalize_key(k): str(v).strip() for k, v in row.items()}
        if not nrow.get("day_type") or not nrow.get("exercise"):
            continue
        normalized_rows.append(nrow)

    return {
        "tips": parsed.get("tips", "").strip(),
        "workout_plan": normalized_rows,
    }


def _parse_updated_range_rows(updated_range: str) -> Tuple[int, int]:
    # Example: "'workout table'!A333:H340"
    _, cells = updated_range.split("!", 1)
    start_ref, end_ref = cells.split(":", 1)
    start_row = int(re.search(r"\d+", start_ref).group(0))
    end_row = int(re.search(r"\d+", end_ref).group(0))
    return start_row, end_row


def _get_sheet_gid(service, sheet_id: str, tab_name: str) -> int:
    meta = (
        service.spreadsheets()
        .get(
            spreadsheetId=sheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute()
    )
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab_name:
            return int(props["sheetId"])
    raise ValueError(f"Could not find tab: {tab_name}")


def _color_appended_rows(
    service, sheet_id: str, tab_name: str, start_row: int, end_row: int
) -> None:
    gid = _get_sheet_gid(service, sheet_id, tab_name)
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": gid,
                            "startRowIndex": start_row - 1,
                            "endRowIndex": end_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": 8,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "foregroundColor": {
                                        "red": 0.12,
                                        "green": 0.47,
                                        "blue": 0.71,
                                    }
                                }
                            }
                        },
                        "fields": "userEnteredFormat.textFormat.foregroundColor",
                    }
                }
            ]
        },
    ).execute()


def append_ai_output(
    service,
    sheet_id: str,
    tab_name: str,
    tips: str,
    workout_plan: List[Dict[str, str]],
):
    today = datetime.now().strftime("%Y-%m-%d")
    values: List[List[str]] = []
    for row in workout_plan:
        values.append(
            [
                row.get("week", ""),
                row.get("date", today),
                row.get("day_type", ""),
                row.get("exercise", ""),
                row.get("set", ""),
                row.get("weight_lbs", ""),
                row.get("reps", ""),
                row.get("notes", ""),
            ]
        )

    if not values:
        # Fallback keeps behavior deterministic even if model returns no rows.
        values = [["", today, "AI Plan", "No plan rows returned", "", "", "", tips]]

    append_result = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A:H",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )
    updated_range = append_result.get("updates", {}).get("updatedRange", "")
    if updated_range:
        start_row, end_row = _parse_updated_range_rows(updated_range)
        _color_appended_rows(service, sheet_id, tab_name, start_row, end_row)


st.set_page_config(page_title="Workout Helper", page_icon="💪")
st.title("Workout Helper")
st.caption(
    "Workflow: log workouts directly in Google Sheets, then click one button here to get tips + an inline AI workout plan appended to your table."
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
                workout_plan=ai["workout_plan"],
            )
            st.success("Saved AI tips + inline workout plan to Google Sheets.")
            st.subheader("Tips")
            st.write(ai["tips"])
            st.subheader("Planned Rows")
            if ai["workout_plan"]:
                st.dataframe(ai["workout_plan"], use_container_width=True)
            else:
                st.caption("No workout rows were returned by the model.")
    except Exception as exc:
        st.error(str(exc))
