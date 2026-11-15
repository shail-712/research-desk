"""
Research Desk: the Streamlit frontend (API client only).

    streamlit run ui/app.py            # from the repo root; secrets in .streamlit/secrets.toml

Secrets (Streamlit secrets, never in code): API_URL (the stack's ApiUrl output) and ACCESS_CODE
(APP_ACCESS_CODE). The app sends the code to the API as `x-access-code`. Viewers must type the same
code once per browser session (the gate), so a shared app URL alone doesn't open the API.
The theme (warm beige and brown) is in .streamlit/config.toml; display helpers are in ui/view.py.

Tabs (the open tab is kept in session state under "tab", so reruns never reset it):
- Ask: Quick or Deep -> POST /ask -> poll GET /ask/{id} -> the answer, then its sources and a short
  trace. The job id is kept in session state while it runs, so a rerun (switching tabs, an example
  button, Refresh) resumes the polling instead of losing the answer; the last answer stays shown.
- Watchlist: live quotes as tiles (GET /watchlist, GET /quotes), re-read every 60 s in a fragment,
  which reruns only that tab's content.
- Alerts: the Watchdog's latest alerts (GET /feed) as cards; demo replays are marked.
- How it works: static text.

Adapted from v1 app.py (73b6fda): the API is the only backend (no core/ imports), the access gate
is kept, the add/remove watchlist controls are cut.
"""

import hmac

import streamlit as st

import view
from client import DeskClient, DeskError
from view import md

st.set_page_config(page_title="Research Desk", page_icon="📈", layout="wide")

TABS = ["Ask", "Watchlist", "Alerts", "How it works"]   # fixed labels: a changed label resets the tab
REFRESH_SECONDS = 60
MODES = {"Quick": "One Gemini call over live quotes, news and filings (~6 s)",
         "Deep": "A planner and specialist agents with a fact-checker (~10-25 s)"}
EXAMPLES = [("AMD market cap & P/E", "What is AMD's market cap and P/E ratio, and the latest headlines?"),
            ("Compare NVIDIA and AMD revenue", "Compare NVIDIA and AMD revenue"),
            ("AMD selloff & guidance", "Is AMD's selloff related to its guidance?")]
INTRO = ("Ask questions about US stocks. Answers draw on live quotes, recent news and SEC filings, and "
         "list their sources. A Watchdog also watches a few tickers and posts an alert when one moves.")

# Small additions to the theme: the answer card, the "not covered" callout and replay alert cards.
# The containers are styled by their `key` (Streamlit adds the class st-key-<key>).
CSS = """<style>
.st-key-answer-card { border-left: 5px solid #8B5A2B !important; background: #FFFCF7; }
.st-key-not-covered { background: #F3E3C8; border: 1px solid #B9874F; border-left: 5px solid #8B5A2B;
                      border-radius: 0.5rem; padding: 0.75rem 1rem; }
[class*="st-key-replay-"] { border: 2px dashed #B9874F !important; background: #F1EADF; }
[class*="st-key-alert-"] { background: #FFFCF7; }
</style>"""


def secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:  # no secrets file at all
        return ""


# ── access gate ──

def require_access(code: str) -> None:
    if st.session_state.get("authed"):
        return
    st.title("📈 Research Desk")
    st.caption(INTRO)
    entered = st.text_input("Access code", type="password")
    if entered:
        if hmac.compare_digest(entered.encode(), code.encode()):
            st.session_state.authed = True
            st.rerun()
        st.error("Wrong access code.")
    st.stop()


@st.cache_resource
def get_client(api_url: str, access_code: str) -> DeskClient:
    return DeskClient(api_url, access_code)


@st.cache_data(ttl=REFRESH_SECONDS - 10, show_spinner=False)   # expires before the next 60 s re-read
def load_watchlist(_client: DeskClient) -> tuple[list[dict], dict]:
    return _client.watchlist(), _client.quotes()


@st.cache_data(ttl=30, show_spinner=False)
def load_feed(_client: DeskClient) -> list[dict]:
    return _client.feed(limit=10)


def refresh() -> None:
    load_watchlist.clear()
    load_feed.clear()


# ── header ──

def header() -> None:
    title, button = st.columns([6, 1], vertical_alignment="bottom")
    title.title("📈 Research Desk")
    button.button("Refresh data", icon=":material/refresh:", on_click=refresh, width="stretch",
                  help="Fetch the watchlist quotes and alerts again now")
    st.markdown(INTRO)


# ── ask ──

def fill_question(text: str) -> None:
    st.session_state.question = text   # fills the box only; the reviewer still picks the mode and asks


def ask_panel(client: DeskClient) -> None:
    st.caption("Try one (fills in the question):")
    for col, (short, full) in zip(st.columns(len(EXAMPLES)), EXAMPLES):
        col.button(short, key=f"example-{short}", help=full, on_click=fill_question, args=(full,), width="stretch")
    with st.form("ask"):
        question = st.text_input("Question", key="question", placeholder=EXAMPLES[0][1])
        mode = st.radio("Mode", list(MODES), key="mode", horizontal=True, captions=list(MODES.values()))
        submitted = st.form_submit_button("Ask", type="primary", icon=":material/send:")
    if submitted and question.strip():
        try:
            job_id = client.ask(question.strip(), mode.lower())
        except DeskError as e:
            st.error(str(e))
        else:
            st.session_state.pending = {"job_id": job_id, "mode": mode.lower(), "question": question.strip()}
            st.session_state.pop("ask_error", None)
    if "pending" in st.session_state:
        poll(client, st.session_state.pending)
    if st.session_state.get("ask_error"):
        st.error(st.session_state.ask_error)
    elif "last" in st.session_state and "pending" not in st.session_state:
        show_answer(st.session_state.last["question"], st.session_state.last["result"])


def poll(client: DeskClient, pending: dict) -> None:
    """Waits for the job. A rerun stops this midway; `pending` stays in session state, so the next run
    picks the same job up again (the answer isn't lost, it just keeps polling)."""
    typical = view.TYPICAL_SECONDS[pending["mode"]]
    st.markdown(f"**Answering:** {md(pending['question'])}")
    with st.status(f"Asking ({pending['mode'].title()} mode)…", expanded=True) as status:
        bar = st.progress(0.0, text="Sent. Waiting for the worker…")

        def on_poll(seconds: float) -> None:
            bar.progress(view.progress_fraction(seconds, pending["mode"]),
                         text=f"Working… {seconds:.0f} s (usually up to {typical:.0f} s)")
            status.update(label=f"Working… {seconds:.0f} s")

        try:
            result = client.wait(pending["job_id"], on_poll=on_poll)
        except DeskError as e:
            status.update(label="Failed", state="error", expanded=False)
            st.session_state.ask_error = str(e)
        else:
            bar.progress(1.0, text="Done")
            status.update(label="Done", state="complete", expanded=False)
            st.session_state.last = {"question": pending["question"], "result": result}
            st.session_state.pop("ask_error", None)
    st.session_state.pop("pending", None)


def show_answer(question: str, r: dict) -> None:
    if r.get("status") == "error" or not r.get("answer"):
        st.error(r.get("error") or "No answer.")
        return
    not_covered, other = view.split_notice(r.get("notice"))
    if not_covered:
        with st.container(key="not-covered"):
            st.markdown("**📄 Not covered**")
            st.markdown(md(not_covered))
            st.caption("So the answer below doesn't use SEC filings for that company.")
    if other:
        st.warning(md(other), icon=":material/info:")
    with st.container(border=True, key="answer-card"):
        st.caption(f"Q: {md(question)}")
        st.markdown(md(r["answer"]))
        st.caption(view.answer_meta(r))
    for title, items in view.group_sources(r.get("sources") or []):
        cited = sum(1 for s in items if s.get("cited"))
        with st.expander(f"Sources: {title} ({len(items)})" + (f" · {cited} cited" if cited else "")):
            for s in items:
                mark = "✅ " if s.get("cited") else ""
                label = md(s.get("label"))
                st.markdown(f"- {mark}[{label}]({s['url']})" if s.get("url") else f"- {mark}{label}")
    with st.expander("How this answer was made"):
        st.markdown("\n".join(f"- {line if '**' in line else md(line)}" for line in view.trace_lines(r)))
        rows = view.tool_timings(r) if not r.get("trace") else []
        if rows:
            st.dataframe(rows, hide_index=True)


# ── watchlist ──

@st.fragment(run_every=REFRESH_SECONDS)   # re-reads quotes every 60 s; reruns only this tab's content
def watchlist_panel(client: DeskClient) -> None:
    try:
        watchlist, quotes = load_watchlist(client)
    except DeskError as e:
        st.error(str(e))
        return
    if not watchlist:
        st.caption("The watchlist is empty.")
        return
    per_row = 4
    for start in range(0, len(watchlist), per_row):
        for col, w in zip(st.columns(per_row), watchlist[start:start + per_row]):
            with col, st.container(border=True):
                tile(w, quotes.get(w["ticker"], {}))
    sessions = sorted({q.get("trading_date") for q in quotes.values() if q.get("trading_date")})
    sources = sorted({q.get("source") for q in quotes.values() if q.get("source")})
    st.caption(f"Session {', '.join(sessions) or 'n/a'} · source {', '.join(sources) or 'n/a'} · "
               f"quotes re-read every {REFRESH_SECONDS} s")


def tile(w: dict, q: dict) -> None:
    st.caption(w.get("name") or w["ticker"])
    if q.get("error") or q.get("price") is None:
        st.metric(w["ticker"], "n/a", help=q.get("error"))
        st.caption("Quote unavailable right now.")
        return
    change = q.get("change_pct")
    st.metric(w["ticker"], f"${q['price']:,.2f}", f"{change:+.2f}%" if change is not None else None,
              help=f"Previous close {q.get('prev_close')}, session {q.get('trading_date')}, source {q.get('source')}")
    if w.get("pct_move") is not None:
        st.caption(f"Alert when it moves ±{w['pct_move']}% in a day")


# ── alerts ──

@st.fragment(run_every=REFRESH_SECONDS)
def alerts_panel(client: DeskClient) -> None:
    try:
        events = load_feed(client)
    except DeskError as e:
        st.error(str(e))
        return
    if not events:
        st.caption("No alerts yet. The Watchdog scans the watchlist every 15 min in market hours.")
        return
    st.caption("Newest first. Alerts are kept for 30 days.")
    for i, e in enumerate(events):
        replay = view.is_replay(e)
        with st.container(border=True, key=f"{'replay' if replay else 'alert'}-{i}"):
            arrow = ":red[▼]" if view.is_down(e) else ":green[▲]"
            st.markdown(f"{arrow} **{md(e.get('title'))}**")
            if replay:
                st.badge("Replay: a demo, not a real move", icon=":material/replay:", color="orange")
            st.caption(view.fmt_time(e.get("created_at")))
            if e.get("body"):
                st.markdown(md(e["body"]))


# ── how it works ──

HOW = """
**What happens when you ask**

1. The app sends your question to the Research Desk API, which starts a background job.
2. A worker gathers evidence: live quotes, recent news and SEC filing excerpts.
3. The app checks the job every 2 s and shows progress until the answer is ready.
4. You get the answer, the time it is "as of", its sources and a short account of how it was made.

**Quick or Deep**

- **Quick** (~6 s): one Gemini call over the evidence.
- **Deep** (~10-25 s): a planner sends the question to specialist agents (market, news, filings), and a
  fact-checker compares the numbers in the answer with the evidence. If Deep fails you get a Quick
  answer instead, and if it runs out of time a partial one; a notice above the answer says so.

**Coverage**

SEC filings are indexed for a few companies only (AMD, Intel and NVIDIA). For any other company, the
answer says so in a "Not covered" box and relies on quotes and news.

**Watchdog alerts**

In US market hours the Watchdog checks the watchlist every 15 minutes and posts an alert when a stock
moves more than its threshold. Alerts marked **Replay** are demos (a made-up move used to show the
alert path outside market hours), not real moves.
"""


def main() -> None:
    api_url, code = secret("API_URL"), secret("ACCESS_CODE")
    if not api_url or not code:
        st.error("Missing secrets: set API_URL and ACCESS_CODE in Streamlit secrets "
                 "(locally: .streamlit/secrets.toml, see .streamlit/secrets.toml.example).")
        st.stop()
    require_access(code)
    st.html(CSS)
    client = get_client(api_url, code)
    header()
    ask, watch, alerts, how = st.tabs(TABS, key="tab", on_change="rerun")
    # Ask is drawn last: a slow answer then doesn't hold back the other tabs.
    with watch:
        watchlist_panel(client)
    with alerts:
        alerts_panel(client)
    with how:
        st.markdown(HOW)
    with ask:
        ask_panel(client)


main()
