import base64
import hashlib
import hmac
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
MASTER_CSV = DATA_DIR / "master_questions.csv"
VESSELS_CSV = DATA_DIR / "vessels.csv"
USERS_CSV = DATA_DIR / "initial_users.csv"

BUILTIN_USERS = [
    ("admin", "admin123", "admin", "", "System Admin"),
    ("office", "office123", "office", "", "Office Dashboard"),
]

STATUS_OPTIONS = ["", "Satisfactory", "Defect", "NA"]
COMPLETE_STATUSES = ("Satisfactory", "Defect", "NA")

st.set_page_config(page_title="SIRE Campaign", page_icon="🛳️", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 0.8rem; padding-bottom: 2rem;}
      .metric-card {border:1px solid #e5e7eb;border-radius:16px;padding:16px;background:#fff;margin-bottom:8px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
      .small-muted {color:#6b7280;font-size:0.85rem;}
      .defect {border-left:6px solid #dc2626;}
      .ok {border-left:6px solid #16a34a;}
      .chapter {border-left:6px solid #2563eb;}
      .stickybar {position: sticky; top: 0; z-index: 999; background: rgba(255,255,255,.97); border-bottom: 1px solid #e5e7eb; padding: 0.55rem 0 0.45rem 0; margin-bottom: 0.8rem;}
      div[data-testid="stProgress"] > div > div > div {height: 10px;}
      section[data-testid="stSidebar"] {min-width: 260px;}
    </style>
    """,
    unsafe_allow_html=True,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_database_url() -> str:
    url = st.secrets.get("DATABASE_URL", None) if hasattr(st, "secrets") else None
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        st.error("DATABASE_URL is missing. Add it in Streamlit Cloud → Manage app → Settings → Secrets.")
        st.stop()
    return url


def connect():
    return psycopg2.connect(get_database_url(), sslmode="require")


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or base64.urlsafe_b64encode(os.urandom(16)).decode()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 150_000)
    return f"pbkdf2_sha256${salt}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, _digest = stored.split("$", 2)
        return hmac.compare_digest(hash_password(password, salt), stored)
    except Exception:
        # Allows emergency migration from plain text if any old rows exist.
        return hmac.compare_digest(password, stored or "")


def qid_to_int(value) -> int:
    m = re.search(r"(\d+)", str(value))
    if not m:
        raise ValueError(f"Invalid question_id: {value}")
    return int(m.group(1))


def exec_sql(sql: str, params: tuple | list = ()):
    with connect() as con:
        with con.cursor() as cur:
            cur.execute(sql, params)
        con.commit()


def fetchone(sql: str, params: tuple | list = ()) -> dict | None:
    with connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


@st.cache_data(ttl=5)
def read_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as con:
        return pd.read_sql_query(sql, con, params=params)


def clear_cache():
    read_df.clear()


def ensure_schema():
    with connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users_app (
                    username TEXT PRIMARY KEY,
                    password TEXT NOT NULL,
                    role TEXT NOT NULL,
                    vessel TEXT,
                    display_name TEXT,
                    active BOOLEAN DEFAULT TRUE
                );
                CREATE TABLE IF NOT EXISTS vessels (
                    vessel TEXT PRIMARY KEY,
                    vessel_name TEXT,
                    imo_no TEXT,
                    flag TEXT,
                    year_built TEXT
                );
                CREATE TABLE IF NOT EXISTS master_questions (
                    question_id INTEGER PRIMARY KEY,
                    sire_chapter TEXT,
                    question TEXT,
                    rank TEXT
                );
                CREATE TABLE IF NOT EXISTS responses (
                    vessel TEXT NOT NULL,
                    question_id INTEGER NOT NULL,
                    status TEXT,
                    remarks TEXT,
                    office_comment TEXT,
                    updated_by TEXT,
                    updated_at TIMESTAMP,
                    office_updated_by TEXT,
                    office_updated_at TIMESTAMP,
                    PRIMARY KEY (vessel, question_id)
                );
                """
            )
            # Safe ALTERs for tables already created manually in Supabase.
            alter_statements = [
                "ALTER TABLE users_app ADD COLUMN IF NOT EXISTS display_name TEXT",
                "ALTER TABLE users_app ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE",
                "ALTER TABLE vessels ADD COLUMN IF NOT EXISTS vessel_name TEXT",
                "ALTER TABLE vessels ADD COLUMN IF NOT EXISTS imo_no TEXT",
                "ALTER TABLE vessels ADD COLUMN IF NOT EXISTS flag TEXT",
                "ALTER TABLE vessels ADD COLUMN IF NOT EXISTS year_built TEXT",
                "ALTER TABLE responses ADD COLUMN IF NOT EXISTS office_comment TEXT",
                "ALTER TABLE responses ADD COLUMN IF NOT EXISTS office_updated_by TEXT",
                "ALTER TABLE responses ADD COLUMN IF NOT EXISTS office_updated_at TIMESTAMP",
                "ALTER TABLE responses ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
                "ALTER TABLE responses ADD COLUMN IF NOT EXISTS updated_by TEXT",
            ]
            for stmt in alter_statements:
                cur.execute(stmt)
        con.commit()


def seed_data_if_needed():
    ensure_schema()
    with connect() as con:
        with con.cursor() as cur:
            # Built-in admin and office accounts repaired every startup.
            for username, password, role, vessel, display_name in BUILTIN_USERS:
                cur.execute(
                    """
                    INSERT INTO users_app(username,password,role,vessel,display_name,active)
                    VALUES(%s,%s,%s,%s,%s,TRUE)
                    ON CONFLICT(username) DO UPDATE SET
                        password=EXCLUDED.password,
                        role=EXCLUDED.role,
                        vessel=EXCLUDED.vessel,
                        display_name=EXCLUDED.display_name,
                        active=TRUE
                    """,
                    (username, hash_password(password), role, vessel, display_name),
                )

            cur.execute("SELECT COUNT(*) FROM vessels")
            if cur.fetchone()[0] == 0 and VESSELS_CSV.exists():
                vessels = pd.read_csv(VESSELS_CSV).fillna("")
                for _, r in vessels.iterrows():
                    cur.execute(
                        """
                        INSERT INTO vessels(vessel,vessel_name,imo_no,flag,year_built)
                        VALUES(%s,%s,%s,%s,%s)
                        ON CONFLICT(vessel) DO UPDATE SET
                            vessel_name=EXCLUDED.vessel_name,
                            imo_no=EXCLUDED.imo_no,
                            flag=EXCLUDED.flag,
                            year_built=EXCLUDED.year_built
                        """,
                        (str(r["vessel_id"]), str(r["vessel_name"]), str(r.get("imo_no", "")), str(r.get("flag", "")), str(r.get("year_built", ""))),
                    )

            cur.execute("SELECT COUNT(*) FROM master_questions")
            if cur.fetchone()[0] == 0 and MASTER_CSV.exists():
                questions = pd.read_csv(MASTER_CSV).fillna("")
                for _, r in questions.iterrows():
                    cur.execute(
                        """
                        INSERT INTO master_questions(question_id,sire_chapter,question,rank)
                        VALUES(%s,%s,%s,%s)
                        ON CONFLICT(question_id) DO UPDATE SET
                            sire_chapter=EXCLUDED.sire_chapter,
                            question=EXCLUDED.question,
                            rank=EXCLUDED.rank
                        """,
                        (qid_to_int(r["question_id"]), str(r["sire_chapter"]), str(r["question"]), str(r["rank"])),
                    )

            if USERS_CSV.exists():
                users = pd.read_csv(USERS_CSV).fillna("")
                for _, r in users.iterrows():
                    username = str(r["username"]).strip()
                    if not username or username in {"admin", "office"}:
                        continue
                    cur.execute(
                        """
                        INSERT INTO users_app(username,password,role,vessel,display_name,active)
                        VALUES(%s,%s,%s,%s,%s,TRUE)
                        ON CONFLICT(username) DO NOTHING
                        """,
                        (username, hash_password(str(r["password"]).strip()), str(r["role"]).strip(), str(r.get("vessel_id", "")).strip(), str(r.get("display_name", username)).strip()),
                    )
        con.commit()


def login_box():
    st.title("🛳️ SIRE Campaign Login")
    c1, _ = st.columns([1, 1])
    with c1:
        username = st.text_input("User ID")
        password = st.text_input("Password", type="password")
        if st.button("Login", type="primary", use_container_width=True):
            clean_username = username.strip()
            row = fetchone("SELECT * FROM users_app WHERE username=%s AND active=TRUE", (clean_username,))
            if row and verify_password(password.strip(), row.get("password", "")):
                st.session_state.user = row
                st.session_state.view = "vessel" if row["role"] == "vessel" else "office"
                st.rerun()
            else:
                st.error("Invalid login or inactive user.")


def sidebar():
    user = st.session_state.user
    st.sidebar.markdown(f"**Logged in:** {user.get('display_name') or user['username']}")
    st.sidebar.caption(f"Role: {user['role'].upper()}")
    if st.sidebar.checkbox("Auto refresh dashboards", value=False):
        if st_autorefresh:
            st_autorefresh(interval=15_000, key="auto_refresh")
        else:
            st.sidebar.warning("Auto-refresh package not available. Use browser refresh.")
    if st.sidebar.button("Logout", use_container_width=True):
        st.session_state.clear()
        st.rerun()


def rank_options():
    df = read_df("SELECT DISTINCT rank FROM master_questions ORDER BY rank")
    return ["All"] + df["rank"].dropna().astype(str).tolist()


def selected_vessel_header(vessel: str):
    v = read_df("SELECT * FROM vessels WHERE vessel=%s", (vessel,))
    if not v.empty:
        r = v.iloc[0]
        st.subheader(str(r.get("vessel_name") or r["vessel"]))
        st.caption(f"IMO: {r.get('imo_no','')} | Flag: {r.get('flag','')} | Built: {r.get('year_built','')}")


def progress_for(vessel: str, rank: str | None = None, chapter: str | None = None) -> tuple[int, int, float]:
    where = []
    params: list = [vessel]
    if rank and rank != "All":
        where.append("q.rank=%s")
        params.append(rank)
    if chapter is not None:
        where.append("q.sire_chapter=%s")
        params.append(str(chapter))
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    row = fetchone(
        f"""
        SELECT COUNT(q.question_id) AS total,
               COALESCE(SUM(CASE WHEN r.status IN ('Satisfactory','Defect','NA') THEN 1 ELSE 0 END),0) AS completed
        FROM master_questions q
        LEFT JOIN responses r ON r.question_id=q.question_id AND r.vessel=%s
        {where_sql}
        """,
        tuple(params),
    )
    total = int(row["total"] or 0)
    completed = int(row["completed"] or 0)
    pct = completed / total if total else 0.0
    return completed, total, pct


def defect_count(vessel: str, rank: str | None = None) -> int:
    params: list = [vessel]
    rank_sql = ""
    if rank and rank != "All":
        rank_sql = "AND q.rank=%s"
        params.append(rank)
    df = read_df(
        f"""
        SELECT COUNT(*) AS cnt
        FROM responses r
        JOIN master_questions q ON q.question_id=r.question_id
        WHERE r.vessel=%s AND r.status='Defect' {rank_sql}
        """,
        tuple(params),
    )
    return int(df.loc[0, "cnt"])


def has_unsaved_changes(df: pd.DataFrame, vessel: str) -> bool:
    for _, r in df.iterrows():
        qid = int(r["question_id"])
        old_status = str(r.get("status", "") or "")
        old_remarks = str(r.get("remarks", "") or "")
        new_status = st.session_state.get(f"status_{vessel}_{qid}", old_status)
        new_remarks = st.session_state.get(f"remarks_{vessel}_{qid}", old_remarks)
        if (new_status or "") != old_status or (new_remarks or "").strip() != old_remarks.strip():
            return True
    return False


def top_action_bar(title: str, back_label: str | None = None, back_target: str | None = None, save_label: str | None = None):
    st.markdown("<div class='stickybar'>", unsafe_allow_html=True)
    cols = st.columns([2.2, 1, 1, 1])
    with cols[0]:
        st.markdown(f"**{title}**")
    back_clicked = False
    save_clicked = False
    with cols[1]:
        if back_label:
            back_clicked = st.button(back_label, use_container_width=True)
    with cols[2]:
        if save_label:
            save_clicked = st.button(save_label, type="primary", use_container_width=True)
    with cols[3]:
        if st.button("🔄 Refresh", use_container_width=True):
            clear_cache()
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
    return back_clicked, save_clicked


def vessel_dashboard(vessel: str, office_view: bool = False):
    selected_vessel_header(vessel)
    rank = st.selectbox("Rank filter", rank_options(), key=f"rank_{vessel}")
    completed, total, pct = progress_for(vessel, rank)
    defects = defect_count(vessel, rank)

    m1, m2, m3 = st.columns(3)
    m1.metric("Completed", f"{completed}/{total}")
    m2.metric("Progress", f"{pct:.0%}")
    m3.metric("Open defects", defects)
    st.progress(pct)

    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button(f"🔴 Defect list ({defects})", key=f"defects_{vessel}", use_container_width=True):
            st.session_state.view = "defects"
            st.session_state.vessel = vessel
            st.session_state.rank_filter = rank
            st.rerun()
    with c2:
        if office_view and st.button("⬅ Back to office dashboard", use_container_width=True):
            st.session_state.view = "office"
            st.rerun()

    chapters = read_df("SELECT DISTINCT sire_chapter FROM master_questions ORDER BY sire_chapter")['sire_chapter'].astype(str).tolist()
    st.markdown("### Chapters")
    cols = st.columns(4)
    for i, ch in enumerate(chapters):
        c, t, p = progress_for(vessel, rank, ch)
        with cols[i % 4]:
            with st.container(border=True):
                st.markdown(f"**Chapter {ch}**")
                st.caption(f"{c}/{t} completed | {p:.0%}")
                st.progress(p)
                if st.button("Open chapter", key=f"open_{vessel}_{rank}_{ch}", use_container_width=True):
                    st.session_state.view = "questions"
                    st.session_state.vessel = vessel
                    st.session_state.rank_filter = rank
                    st.session_state.chapter = ch
                    st.rerun()


def load_question_df(vessel: str, chapter: str, rank: str) -> pd.DataFrame:
    params: list = [vessel, chapter]
    rank_sql = ""
    if rank != "All":
        rank_sql = "AND q.rank=%s"
        params.append(rank)
    return read_df(
        f"""
        SELECT q.question_id, q.sire_chapter, q.rank, q.question,
               COALESCE(r.status, '') AS status,
               COALESCE(r.remarks, '') AS remarks,
               COALESCE(r.office_comment, '') AS office_comment,
               r.updated_at, r.updated_by, r.office_updated_at, r.office_updated_by
        FROM master_questions q
        LEFT JOIN responses r ON r.question_id=q.question_id AND r.vessel=%s
        WHERE q.sire_chapter=%s {rank_sql}
        ORDER BY q.rank, q.question_id
        """,
        tuple(params),
    )


def save_question_page(df: pd.DataFrame, vessel: str) -> bool:
    user = st.session_state.user["username"]
    now = utc_now()
    errors = []
    rows_to_save = []
    for _, r in df.iterrows():
        qid = int(r["question_id"])
        status = st.session_state.get(f"status_{vessel}_{qid}", "")
        remarks = st.session_state.get(f"remarks_{vessel}_{qid}", "").strip()
        if status == "Defect" and not remarks:
            errors.append(f"Q{qid:04d}: defect remarks are mandatory.")
        if status in COMPLETE_STATUSES:
            rows_to_save.append((vessel, qid, status, remarks, user, now))
    if errors:
        st.error("Please correct before saving:\n\n" + "\n".join(errors[:10]))
        return False
    with connect() as con:
        with con.cursor() as cur:
            for row in rows_to_save:
                cur.execute(
                    """
                    INSERT INTO responses(vessel,question_id,status,remarks,updated_by,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(vessel,question_id) DO UPDATE SET
                        status=EXCLUDED.status,
                        remarks=EXCLUDED.remarks,
                        updated_by=EXCLUDED.updated_by,
                        updated_at=EXCLUDED.updated_at
                    """,
                    row,
                )
        con.commit()
    clear_cache()
    st.success(f"Saved {len(rows_to_save)} answered questions at {now}.")
    return True


def question_page(vessel: str, chapter: str, rank: str):
    df = load_question_df(vessel, chapter, rank)
    title = f"Chapter {chapter} — {rank}"
    back_clicked, save_clicked = top_action_bar(title, "⬅ Dashboard", "dashboard", "💾 Save progress")

    if df.empty:
        st.warning("No questions found for this filter.")
        return

    editable = False
    user = st.session_state.user
    if user["role"] == "vessel" and user.get("vessel") == vessel:
        editable = True
    if user["role"] == "admin":
        editable = True

    if save_clicked:
        if editable:
            if save_question_page(df, vessel):
                st.rerun()
        else:
            st.warning("This login cannot edit vessel responses.")

    if back_clicked:
        if editable and has_unsaved_changes(df, vessel):
            st.warning("You have unsaved changes. Press **Save progress** first, then return to dashboard.")
        else:
            st.session_state.view = "vessel_dashboard" if user["role"] in ["office", "admin"] else "vessel"
            st.rerun()

    selected_vessel_header(vessel)
    st.caption("Use one Save progress button at the top. Defect remarks are mandatory.")

    for _, r in df.iterrows():
        qid = int(r["question_id"])
        border = "defect" if r['status'] == 'Defect' else "ok" if r['status'] in ['Satisfactory', 'NA'] else ""
        st.markdown(f"<div class='metric-card {border}'><b>{r['rank']} | Q{qid:04d}</b><br>{r['question']}</div>", unsafe_allow_html=True)
        c1, c2 = st.columns([1, 2])
        current_status = r['status'] if r['status'] in STATUS_OPTIONS else ""
        with c1:
            st.selectbox("Status", STATUS_OPTIONS, index=STATUS_OPTIONS.index(current_status), key=f"status_{vessel}_{qid}", disabled=not editable)
        with c2:
            st.text_area("Vessel remarks", value=str(r['remarks'] or ""), key=f"remarks_{vessel}_{qid}", disabled=not editable, height=80)
        if str(r.get("office_comment") or "").strip():
            st.info(f"Office comment: {r['office_comment']}")
        meta = []
        if r.get('updated_at'):
            meta.append(f"Vessel update: {r['updated_at']} by {r.get('updated_by') or '-'}")
        if r.get('office_updated_at'):
            meta.append(f"Office comment update: {r['office_updated_at']} by {r.get('office_updated_by') or '-'}")
        if meta:
            st.caption(" | ".join(meta))
        st.divider()


def defects_page(vessel: str, rank: str = "All"):
    user = st.session_state.user
    selected_vessel_header(vessel)
    params: list = [vessel]
    rank_sql = ""
    if rank and rank != "All":
        rank_sql = "AND q.rank=%s"
        params.append(rank)
    df = read_df(
        f"""
        SELECT q.question_id, q.sire_chapter, q.rank, q.question,
               r.remarks, COALESCE(r.office_comment, '') AS office_comment,
               r.updated_at, r.updated_by, r.office_updated_at, r.office_updated_by
        FROM responses r
        JOIN master_questions q ON q.question_id=r.question_id
        WHERE r.vessel=%s AND r.status='Defect' {rank_sql}
        ORDER BY q.sire_chapter, q.rank, q.question_id
        """,
        tuple(params),
    )
    st.markdown(f"### Defect list {'' if rank == 'All' else '— ' + rank}")
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("⬅ Dashboard", use_container_width=True):
            st.session_state.view = "vessel_dashboard" if user['role'] in ['office','admin'] else "vessel"
            st.rerun()
    can_comment = user["role"] in ["office", "admin"]
    save_office = False
    with c2:
        if can_comment:
            save_office = st.button("💾 Save office comments", type="primary", use_container_width=True)

    if df.empty:
        st.success("No defects recorded for this filter.")
        return

    if save_office and can_comment:
        now = utc_now()
        with connect() as con:
            with con.cursor() as cur:
                for _, r in df.iterrows():
                    qid = int(r["question_id"])
                    comment = st.session_state.get(f"office_comment_{vessel}_{qid}", "").strip()
                    cur.execute(
                        """
                        UPDATE responses
                        SET office_comment=%s, office_updated_by=%s, office_updated_at=%s
                        WHERE vessel=%s AND question_id=%s
                        """,
                        (comment, user["username"], now, vessel, qid),
                    )
            con.commit()
        clear_cache()
        st.success("Office comments saved.")
        st.rerun()

    for _, r in df.iterrows():
        qid = int(r["question_id"])
        with st.container(border=True):
            st.markdown(f"**Chapter {r['sire_chapter']} | {r['rank']} | Q{qid:04d}**")
            st.write(r["question"])
            st.markdown("**Vessel remarks:**")
            st.write(r["remarks"] or "-")
            if can_comment:
                st.text_area("Office comments", value=str(r["office_comment"] or ""), key=f"office_comment_{vessel}_{qid}", height=80)
            else:
                st.markdown("**Office comments:**")
                st.info(str(r["office_comment"] or "No office comment entered yet."))
            st.caption(f"Vessel update: {r.get('updated_at') or '-'} by {r.get('updated_by') or '-'} | Office update: {r.get('office_updated_at') or '-'} by {r.get('office_updated_by') or '-'}")

    export = df.copy()
    export["question_id"] = export["question_id"].apply(lambda x: f"Q{int(x):04d}")
    csv = export.to_csv(index=False).encode("utf-8")
    st.download_button("Download defect list CSV", csv, f"defects_{vessel}.csv", "text/csv")


def office_dashboard():
    st.title("Office Dashboard")
    vessels = read_df("SELECT * FROM vessels ORDER BY vessel_name NULLS LAST, vessel")
    st.caption("Cards show live central Supabase progress. Open button is inside each vessel card.")
    cols = st.columns(3)
    for i, v in vessels.iterrows():
        vessel = v["vessel"]
        c, t, p = progress_for(vessel)
        d = defect_count(vessel)
        with cols[i % 3]:
            with st.container(border=True):
                st.markdown(f"### {v.get('vessel_name') or vessel}")
                st.caption(f"{c}/{t} completed | {p:.0%} | Defects: {d}")
                st.progress(p)
                b1, b2 = st.columns(2)
                with b1:
                    if st.button("Open", key=f"office_open_{vessel}", use_container_width=True):
                        st.session_state.view = "vessel_dashboard"
                        st.session_state.vessel = vessel
                        st.rerun()
                with b2:
                    if st.button("Defects", key=f"office_defects_{vessel}", use_container_width=True):
                        st.session_state.view = "defects"
                        st.session_state.vessel = vessel
                        st.session_state.rank_filter = "All"
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
    df['sire_chapter'] = df['sire_chapter'].astype(str).str.strip()
    df['question'] = df['question'].astype(str).str.strip()
    df['rank'] = df['rank'].astype(str).str.strip()
    df = df.drop_duplicates(['sire_chapter','question','rank']).reset_index(drop=True)
    df.insert(0, 'question_id', range(1, len(df) + 1))
    return df


def admin_panel():
    st.title("Admin Panel")
    st.warning("Admin changes affect the central Supabase database.")

    st.markdown("### Reset progress")
    vessels = read_df("SELECT vessel, COALESCE(vessel_name, vessel) AS vessel_name FROM vessels ORDER BY vessel_name")
    target = st.selectbox("Reset target", ["ALL VESSELS"] + vessels['vessel_name'].tolist())
    if st.button("Reset selected progress", type="secondary"):
        with connect() as con:
            with con.cursor() as cur:
                if target == "ALL VESSELS":
                    cur.execute("DELETE FROM responses")
                else:
                    vessel = vessels.loc[vessels['vessel_name'] == target, 'vessel'].iloc[0]
                    cur.execute("DELETE FROM responses WHERE vessel=%s", (vessel,))
            con.commit()
        clear_cache()
        st.success("Progress reset done.")

    st.markdown("### Replace master question list")
    upload = st.file_uploader("Upload Excel/CSV with SIRE Chapter, Question and Rank columns", type=["xlsx", "xlsm", "csv"])
    if upload:
        try:
            newq = validate_master_upload(upload)
            st.write(f"Validated {len(newq)} questions.")
            st.dataframe(newq.head(20), use_container_width=True, hide_index=True)
            if st.button("Replace master list and reset all responses", type="primary"):
                with connect() as con:
                    with con.cursor() as cur:
                        cur.execute("DELETE FROM responses")
                        cur.execute("DELETE FROM master_questions")
                        for _, r in newq.iterrows():
                            cur.execute(
                                "INSERT INTO master_questions(question_id,sire_chapter,question,rank) VALUES(%s,%s,%s,%s)",
                                (int(r["question_id"]), str(r["sire_chapter"]), str(r["question"]), str(r["rank"])),
                            )
                    con.commit()
                clear_cache()
                st.success("Master list replaced and all progress reset.")
        except Exception as e:
            st.error(f"Upload rejected: {e}")

    st.markdown("### Users")
    users = read_df("SELECT username, role, vessel, display_name, active FROM users_app ORDER BY role, username")
    st.dataframe(users, use_container_width=True, hide_index=True)


def main():
    seed_data_if_needed()
    if "user" not in st.session_state:
        login_box()
        return
    sidebar()
    user = st.session_state.user

    if user['role'] == 'vessel':
        st.session_state.setdefault("view", "vessel")
        vessel = user.get("vessel")
        if st.session_state.view == "questions":
            question_page(vessel, st.session_state.chapter, st.session_state.rank_filter)
        elif st.session_state.view == "defects":
            defects_page(vessel, st.session_state.get('rank_filter', 'All'))
        else:
            vessel_dashboard(vessel)
        return

    menu = st.sidebar.radio("Menu", ["Office Dashboard", "Admin Panel"] if user['role'] == 'admin' else ["Office Dashboard"])
    if menu == "Admin Panel":
        admin_panel()
        return

    view = st.session_state.get("view", "office")
    if view == "vessel_dashboard":
        vessel_dashboard(st.session_state.vessel, office_view=True)
    elif view == "questions":
        question_page(st.session_state.vessel, st.session_state.chapter, st.session_state.rank_filter)
    elif view == "defects":
        defects_page(st.session_state.vessel, st.session_state.get('rank_filter', 'All'))
    else:
        office_dashboard()


if __name__ == "__main__":
    main()
