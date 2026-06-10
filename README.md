# SIRE Campaign Streamlit App

This app is a rank-based SIRE campaign tracker using your uploaded master question list and vessel fleet list.

## What it does

- Vessel login: each vessel sees only its own dashboard and questions.
- Office login: office sees all vessel cards, progress, rank/chapter dashboard and defect list.
- Admin login: admin can reset one vessel or all vessels, and replace the master question list.
- Rank filter: chapter cards and question pages show only questions relevant to the selected rank.
- Question status: Satisfactory, Defect or NA.
- Defect remarks are mandatory.
- Each saved entry records updated timestamp and updated-by user.
- Central SQLite database keeps progress live for all users.

## First run

```bash
cd sire_streamlit_app
pip install -r requirements.txt
streamlit run app.py
```

## Default logins

- Admin: `admin` / `admin123`
- Office: `office` / `office123`
- Vessel users: see `data/initial_users.csv`

Change these before live rollout.

## Files included

- `app.py` - main Streamlit app
- `data/master_questions.csv` - extracted from your recovered SIRE rank question file
- `data/vessels.csv` - extracted from your vessel list
- `data/initial_users.csv` - first-run user/password seed file
- `requirements.txt`

## Important production notes

For onboard fleet use, deploy this on one central server, not on separate local vessel computers. Recommended simple deployment:

- Office-hosted VM or cloud server
- HTTPS enabled
- Regular backup of `sire_campaign.db`
- Replace default passwords immediately
- Add a password-change page before wider rollout
- For multi-user heavy use, upgrade database from SQLite to PostgreSQL

SQLite is okay for a pilot and small internal campaign. PostgreSQL is better for a long-term fleet-wide system.
