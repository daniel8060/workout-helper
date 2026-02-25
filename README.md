# workout-helper

Simple local Streamlit app:
1. You log workouts in Google Sheets.
2. At home, you run the app.
3. It reads recent workouts, asks OpenAI for tips + next workout, and writes the output back to the sheet.

## Sheet format

Create a Google Sheet tab named `Workouts` (or set `SHEET_TAB` in `.env`) with this header row:

`date | type | workout | notes | ai_output`

- Manual workout rows: set `type=workout_log`
- AI output rows are auto-written with `type=ai_plan`

## Setup

1. Create and activate env:
```bash
uv sync
```

2. Copy env file:
```bash
cp .env.example .env
```

3. Fill `.env` with:
- `OPENAI_API_KEY`
- `GOOGLE_SHEETS_ID`
- `GOOGLE_SERVICE_ACCOUNT_EMAIL`
- `GOOGLE_PRIVATE_KEY`

4. Google service account steps:
- Create a Google Cloud project
- Enable Google Sheets API
- Create a service account + JSON key
- Share your target sheet with the service account email as editor

## Run

```bash
uv run streamlit run app.py
```

Click: **Analyze recent workouts and write next plan**
