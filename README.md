# SIRE Campaign Streamlit App - Supabase Version

This version saves all progress to Supabase PostgreSQL using `DATABASE_URL` from Streamlit Secrets.

Required Streamlit Secret:

```toml
DATABASE_URL = "postgresql://postgres.xxxxx:password@aws-0-xxxx.pooler.supabase.com:6543/postgres"
```

Main file: `app.py`

Upload to GitHub:
- app.py
- requirements.txt
- README.md
- .gitignore
- data/master_questions.csv
- data/initial_users.csv
- data/vessels.csv

Do not upload any database file.
