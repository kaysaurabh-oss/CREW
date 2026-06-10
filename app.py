import base64
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:  # optional dependency
    st_autorefresh = None

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
DB_PATH = APP_DIR / "sire_campaign.db"
MASTER_CSV = DATA_DIR / "master_questions.csv"
VESSELS_CSV = DATA_DIR / "vessels.csv"
USERS_CSV = DATA_DIR / "initial_users.csv"

STATUS_OPTIONS = ["", "Satisfactory", "Defect", "NA"]
COMPLETE_STATUSES = ["Satisfactory", "Defect", "NA"]

st.set_page_config(page_title="SIRE Campaign", page_icon="🛳️", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.3rem;}
      .metric-card {border:1px solid #e5e7eb;border-radius:16px;padding:16px;background:#fff;margin-bottom:12px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
      .small-muted {color:#6b7280;font-size:0.85rem;}
      .defect {border-left:6px solid #dc2626;}
      .ok {border-left:6px solid #16a34a;}
      .chapter {border-left:6px solid #2563eb;}
      div[data-testid="stProgress"] > div > div > div {height: 10px;}
    </style>
    """,
    unsafe_allow_html=True,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or base64.urlsafe_b64encode(os.urandom(16)).decode()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 150_000)
    return f"pbkdf2_sha256${salt}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$", 2)
        return hmac.compare_digest(hash_password(password, salt), stored)
    except Exception:
        return False


def slug(s: str) -> str:
    import re
    out = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    return out[:40] or "vessel"


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    con = connect()
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS vessels (
            vessel_id TEXT PRIMARY KEY,
            vessel_name TEXT NOT NULL,
            imo_no TEXT,
            flag TEXT,
            year_built TEXT
        );
        CREATE TABLE IF NOT EXISTS questions (
            question_id TEXT PRIMARY KEY,
            sire_chapter INTEGER NOT NULL,
            question TEXT NOT NULL,
            rank TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','office','vessel')),
            vessel_id TEXT,
            display_name TEXT,
            active INTEGER DEFAULT 1,
            FOREIGN KEY(vessel_id) REFERENCES vessels(vessel_id)
        );
        CREATE TABLE IF NOT EXISTS responses (
            vessel_id TEXT NOT NULL,
            question_id TEXT NOT NULL,
            status TEXT CHECK(status IN ('Satisfactory','Defect','NA')),
            remarks TEXT,
            updated_at TEXT,
            updated_by TEXT,
            PRIMARY KEY(vessel_id, question_id),
            FOREIGN KEY(vessel_id) REFERENCES vessels(vessel_id),
            FOREIGN KEY(question_id) REFERENCES questions(question_id)
        );
        """
    )
    con.commit()

    if cur.execute("SELECT COUNT(*) FROM vessels").fetchone()[0] == 0 and VESSELS_CSV.exists():
        pd.read_csv(VESSELS_CSV).to_sql("vessels", con, if_exists="append", index=False)

    if cur.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0 and MASTER_CSV.exists():
        pd.read_csv(MASTER_CSV).to_sql("questions", con, if_exists="append", index=False)

    if cur.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 and USERS_CSV.exists():
        users = pd.read_csv(USERS_CSV).fillna("")
        rows = []
        for _, r in users.iterrows():
            rows.append((r["username"], hash_password(str(r["password"])), r["role"], r.get("vessel_id", ""), r.get("display_name", r["username"]), 1))
        cur.executemany("INSERT INTO users(username,password_hash,role,vessel_id,display_name,active) VALUES(?,?,?,?,?,?)", rows)
        con.commit()
    con.close()


@st.cache_data(ttl=5)
def read_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    con = connect()
    df = pd.read_sql_query(sql, con, params=params)
    con.close()
    return df


def clear_cache():
    read_df.clear()


def login_box():
    st.title("🛳️ SIRE Campaign Login")
    c1, c2 = st.columns([1, 1])
    with c1:
        username = st.text_input("User ID")
        password = st.text_input("Password", type="password")
        if st.button("Login", type="primary", use_container_width=True):
            con = connect()
            row = con.execute("SELECT * FROM users WHERE username=? AND active=1", (username.strip(),)).fetchone()
            con.close()
            if row and verify_password(password, row["password_hash"]):
                st.session_state.user = dict(row)
                st.rerun()
            else:
                st.error("Invalid login or inactive user.")
    with c2:
        st.info("Default first login: admin / admin123, office / office123. Vessel logins are in data/initial_users.csv. Change passwords before live rollout.")


def sidebar():
    user = st.session_state.user
    st.sidebar.markdown(f"**Logged in:** {user.get('display_name') or user['username']}")
    st.sidebar.caption(f"Role: {user['role'].upper()}")
    if st.sidebar.checkbox("Auto refresh office/live dashboard", value=False):
        if st_autorefresh:
            st_autorefresh(interval=15_000, key="auto_refresh")
        else:
            st.sidebar.warning("Install streamlit-autorefresh to enable automatic refresh. Manual refresh still shows latest central data.")
    if st.sidebar.button("Logout", use_container_width=True):
        st.session_state.clear()
        st.rerun()


def progress_for(vessel_id: str, rank: str | None = None, chapter: int | None = None) -> tuple[int, int, float]:
    where = ["1=1"]
    params: list = []
    if rank and rank != "All":
        where.append("q.rank=?")
        params.append(rank)
    if chapter:
        where.append("q.sire_chapter=?")
        params.append(chapter)
    where_sql = " AND ".join(where)
    sql = f"""
      SELECT COUNT(q.question_id) total,
             SUM(CASE WHEN r.status IN ('Satisfactory','Defect','NA') THEN 1 ELSE 0 END) completed
      FROM questions q
      LEFT JOIN responses r ON r.question_id=q.question_id AND r.vessel_id=?
      WHERE {where_sql}
    """
    con = connect()
    row = con.execute(sql, (vessel_id, *params)).fetchone()
    con.close()
    total = int(row["total"] or 0)
    completed = int(row["completed"] or 0)
    pct = completed / total if total else 0
    return completed, total, pct


def defect_count(vessel_id: str, rank: str | None = None) -> int:
    params: list = [vessel_id]
    rank_sql = ""
    if rank and rank != "All":
        rank_sql = "AND q.rank=?"
        params.append(rank)
    df = read_df(f"SELECT COUNT(*) cnt FROM responses r JOIN questions q ON q.question_id=r.question_id WHERE r.vessel_id=? AND r.status='Defect' {rank_sql}", tuple(params))
    return int(df.loc[0, "cnt"])


def rank_options():
    ranks = read_df("SELECT DISTINCT rank FROM questions ORDER BY rank")['rank'].tolist()
    return ["All"] + ranks


def selected_vessel_header(vessel_id: str):
    v = read_df("SELECT * FROM vessels WHERE vessel_id=?", (vessel_id,))
    if not v.empty:
        r = v.iloc[0]
        st.subheader(f"{r['vessel_name']}")
        st.caption(f"IMO: {r.get('imo_no','')} | Flag: {r.get('flag','')} | Built: {r.get('year_built','')}")


def vessel_dashboard(vessel_id: str, office_view: bool = False):
    selected_vessel_header(vessel_id)
    rank = st.selectbox("Rank filter", rank_options(), key=f"rank_{vessel_id}")
    completed, total, pct = progress_for(vessel_id, rank)
    defects = defect_count(vessel_id, rank)

    m1, m2, m3 = st.columns(3)
    m1.metric("Completed", f"{completed}/{total}")
    m2.metric("Progress", f"{pct:.0%}")
    m3.metric("Open defects", defects)
    st.progress(pct)

    if st.button(f"🔴 Show defect list for this vessel ({defects})", key=f"defects_{vessel_id}", use_container_width=True):
        st.session_state.view = "defects"
        st.session_state.vessel_id = vessel_id
        st.session_state.rank_filter = rank
        st.rerun()

    chapters = read_df("SELECT DISTINCT sire_chapter FROM questions ORDER BY sire_chapter")['sire_chapter'].tolist()
    st.markdown("### Chapters")
    cols = st.columns(4)
    for i, ch in enumerate(chapters):
        c, t, p = progress_for(vessel_id, rank, int(ch))
        with cols[i % 4]:
            st.markdown(f"<div class='metric-card chapter'><b>Chapter {ch}</b><br><span class='small-muted'>{c}/{t} completed | {p:.0%}</span></div>", unsafe_allow_html=True)
            st.progress(p)
            if st.button("Open", key=f"open_{vessel_id}_{rank}_{ch}", use_container_width=True):
                st.session_state.view = "questions"
                st.session_state.vessel_id = vessel_id
                st.session_state.rank_filter = rank
                st.session_state.chapter = int(ch)
                st.rerun()

    if office_view and st.button("⬅ Back to office dashboard"):
        st.session_state.view = "office"
        st.rerun()


def question_page(vessel_id: str, chapter: int, rank: str):
    selected_vessel_header(vessel_id)
    st.markdown(f"### Chapter {chapter} — {rank} questions")
    if st.button("⬅ Back to chapter dashboard"):
        if st.session_state.user['role'] in ['office','admin']:
            st.session_state.view = "vessel_dashboard"
        else:
            st.session_state.view = "vessel"
        st.rerun()

    params: list = [vessel_id, chapter]
    rank_sql = ""
    if rank != "All":
        rank_sql = "AND q.rank=?"
        params.append(rank)
    df = read_df(
        f"""
        SELECT q.question_id, q.sire_chapter, q.rank, q.question,
               COALESCE(r.status, '') status, COALESCE(r.remarks, '') remarks,
               r.updated_at, r.updated_by
        FROM questions q
        LEFT JOIN responses r ON r.question_id=q.question_id AND r.vessel_id=?
        WHERE q.sire_chapter=? {rank_sql}
        ORDER BY q.rank, q.question_id
        """,
        tuple(params),
    )
    if df.empty:
        st.warning("No questions found for this filter.")
        return

    editable = st.session_state.user['role'] == 'vessel' and st.session_state.user.get('vessel_id') == vessel_id
    if st.session_state.user['role'] == 'admin':
        editable = True

    for _, r in df.iterrows():
        border = "defect" if r['status'] == 'Defect' else "ok" if r['status'] in ['Satisfactory', 'NA'] else ""
        st.markdown(f"<div class='metric-card {border}'><b>{r['rank']}</b><br>{r['question']}</div>", unsafe_allow_html=True)
        c1, c2 = st.columns([1, 2])
        key_prefix = f"{vessel_id}_{r['question_id']}"
        current_status = r['status'] if r['status'] in STATUS_OPTIONS else ""
        with c1:
            status = st.selectbox("Status", STATUS_OPTIONS, index=STATUS_OPTIONS.index(current_status), key=f"status_{key_prefix}", disabled=not editable)
        with c2:
            remarks = st.text_area("Remarks", value=r['remarks'], key=f"remarks_{key_prefix}", disabled=not editable, height=80)
        if r['updated_at']:
            st.caption(f"Last updated: {r['updated_at']} by {r['updated_by'] or '-'}")
        if editable and st.button("Save", key=f"save_{key_prefix}"):
            if status == "":
                st.warning("Please select Satisfactory, Defect or NA before saving.")
            elif status == "Defect" and not remarks.strip():
                st.error("Remarks are mandatory when status is Defect.")
            else:
                con = connect()
                con.execute(
                    """
                    INSERT INTO responses(vessel_id,question_id,status,remarks,updated_at,updated_by)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(vessel_id,question_id) DO UPDATE SET
                        status=excluded.status,
                        remarks=excluded.remarks,
                        updated_at=excluded.updated_at,
                        updated_by=excluded.updated_by
                    """,
                    (vessel_id, r['question_id'], status, remarks.strip(), utc_now(), st.session_state.user['username']),
                )
                con.commit()
                con.close()
                clear_cache()
                st.success("Saved.")
                st.rerun()
        st.divider()


def defects_page(vessel_id: str, rank: str = "All"):
    selected_vessel_header(vessel_id)
    params: list = [vessel_id]
    rank_sql = ""
    if rank and rank != "All":
        rank_sql = "AND q.rank=?"
        params.append(rank)
    df = read_df(
        f"""
        SELECT q.sire_chapter, q.rank, q.question, r.remarks, r.updated_at, r.updated_by
        FROM responses r
        JOIN questions q ON q.question_id=r.question_id
        WHERE r.vessel_id=? AND r.status='Defect' {rank_sql}
        ORDER BY q.sire_chapter, q.rank, r.updated_at DESC
        """,
        tuple(params),
    )
    st.markdown(f"### Defect list {'' if rank == 'All' else '— ' + rank}")
    if st.button("⬅ Back"):
        st.session_state.view = "vessel_dashboard" if st.session_state.user['role'] in ['office','admin'] else "vessel"
        st.rerun()
    if df.empty:
        st.success("No defects recorded for this filter.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Download defect list CSV", csv, f"defects_{vessel_id}.csv", "text/csv")


def office_dashboard():
    st.title("Office Dashboard")
    vessels = read_df("SELECT * FROM vessels ORDER BY vessel_name")
    st.caption("Click any vessel card to open its rank/chapter dashboard. Progress counts Satisfactory, Defect and NA as completed.")
    cols = st.columns(3)
    for i, v in vessels.iterrows():
        c, t, p = progress_for(v['vessel_id'])
        d = defect_count(v['vessel_id'])
        with cols[i % 3]:
            st.markdown(f"<div class='metric-card'><b>{v['vessel_name']}</b><br><span class='small-muted'>{c}/{t} completed | {p:.0%} | Defects: {d}</span></div>", unsafe_allow_html=True)
            st.progress(p)
            if st.button("Open vessel", key=f"office_open_{v['vessel_id']}", use_container_width=True):
                st.session_state.view = "vessel_dashboard"
                st.session_state.vessel_id = v['vessel_id']
                st.rerun()


def validate_master_upload(file) -> pd.DataFrame:
    if file.name.lower().endswith(".csv"):
        df = pd.read_csv(file)
    else:
        df = pd.read_excel(file, sheet_name=0)
    aliases = {
        "SIRE Chapter": "sire_chapter", "Chapter Number": "sire_chapter", "sire_chapter": "sire_chapter",
        "Question": "question", "question": "question",
        "Rank": "rank", "Checked by": "rank", "rank": "rank"
    }
    df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})
    required = ["sire_chapter", "question", "rank"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {', '.join(missing)}")
    df = df[required].dropna(subset=required).copy()
    df['sire_chapter'] = df['sire_chapter'].astype(int)
    df['question'] = df['question'].astype(str).str.strip()
    df['rank'] = df['rank'].astype(str).str.strip()
    df = df.drop_duplicates(['sire_chapter','question','rank']).reset_index(drop=True)
    df.insert(0, 'question_id', [f"Q{i+1:04d}" for i in range(len(df))])
    return df


def admin_panel():
    st.title("Admin Panel")
    st.warning("Use this page carefully. Master-list replacement resets existing responses because question IDs may change.")

    st.markdown("### Reset progress")
    vessels = read_df("SELECT vessel_id, vessel_name FROM vessels ORDER BY vessel_name")
    target = st.selectbox("Reset target", ["ALL VESSELS"] + vessels['vessel_name'].tolist())
    if st.button("Reset selected progress", type="secondary"):
        con = connect()
        if target == "ALL VESSELS":
            con.execute("DELETE FROM responses")
        else:
            vid = vessels.loc[vessels['vessel_name'] == target, 'vessel_id'].iloc[0]
            con.execute("DELETE FROM responses WHERE vessel_id=?", (vid,))
        con.commit(); con.close(); clear_cache()
        st.success("Progress reset done.")

    st.markdown("### Replace master question list")
    upload = st.file_uploader("Upload Excel/CSV with SIRE Chapter, Question and Rank columns", type=["xlsx", "xlsm", "csv"])
    if upload:
        try:
            newq = validate_master_upload(upload)
            st.write(f"Validated {len(newq)} questions.")
            st.dataframe(newq.head(20), use_container_width=True, hide_index=True)
            if st.button("Replace master list and reset all responses", type="primary"):
                con = connect()
                con.execute("DELETE FROM responses")
                con.execute("DELETE FROM questions")
                newq.to_sql("questions", con, if_exists="append", index=False)
                con.commit(); con.close(); clear_cache()
                newq.to_csv(MASTER_CSV, index=False)
                st.success("Master list replaced and all progress reset.")
        except Exception as e:
            st.error(f"Upload rejected: {e}")

    st.markdown("### Users")
    st.caption("For the production version, add a password-change screen. For now, edit initial_users.csv before first deployment or directly update the DB.")
    users = read_df("SELECT username, role, vessel_id, display_name, active FROM users ORDER BY role, username")
    st.dataframe(users, use_container_width=True, hide_index=True)


def main():
    init_db()
    if "user" not in st.session_state:
        login_box()
        return
    sidebar()
    user = st.session_state.user

    if user['role'] == 'vessel':
        st.session_state.setdefault("view", "vessel")
        if st.session_state.view == "questions":
            question_page(user['vessel_id'], st.session_state.chapter, st.session_state.rank_filter)
        elif st.session_state.view == "defects":
            defects_page(user['vessel_id'], st.session_state.get('rank_filter', 'All'))
        else:
            vessel_dashboard(user['vessel_id'])
        return

    menu = st.sidebar.radio("Menu", ["Office Dashboard", "Admin Panel"] if user['role'] == 'admin' else ["Office Dashboard"])
    if menu == "Admin Panel":
        admin_panel(); return

    view = st.session_state.get("view", "office")
    if view == "vessel_dashboard":
        vessel_dashboard(st.session_state.vessel_id, office_view=True)
    elif view == "questions":
        question_page(st.session_state.vessel_id, st.session_state.chapter, st.session_state.rank_filter)
    elif view == "defects":
        defects_page(st.session_state.vessel_id, st.session_state.get('rank_filter', 'All'))
    else:
        office_dashboard()


if __name__ == "__main__":
    main()
