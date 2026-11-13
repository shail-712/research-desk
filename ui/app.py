"""
Research Desk: the Streamlit frontend (API client only).

    streamlit run ui/app.py            # from the repo root; secrets in .streamlit/secrets.toml

Secrets (Streamlit secrets, never in code): API_URL (the stack's ApiUrl output) and ACCESS_CODE
(APP_ACCESS_CODE). The app sends the code to the API as `x-access-code`. Viewers must type the same
code once per browser session (the gate), so a shared app URL alone doesn't open the API.

- Ask: a question in Quick or Deep mode -> POST /ask -> poll GET /ask/{id} -> the answer with its
  sources, the "as of" time and a short trace.
- Sidebar: the watchlist with live quotes (GET /watchlist, GET /quotes).
- Alert feed: the Watchdog's latest alerts (GET /feed).

Adapted from v1 app.py (73b6fda): the API is the only backend (no core/ imports), the access gate
is kept, the add/remove watchlist controls and price cards are cut.
"""

import hmac

import streamlit as st

from client import DeskClient, DeskError

st.set_page_config(page_title="Research Desk", page_icon="📈", layout="wide")

MODES = {"Quick": "One Gemini call over live quotes, news and filings (~6 s)",
         "Deep": "A planner and specialist agents with a fact-checker (~10-25 s)"}
EXAMPLES = ["What is AMD's market cap and P/E ratio, and the latest headlines?",
            "Compare NVIDIA and AMD revenue",
            "Is AMD's selloff related to its guidance?"]


def md(text: str | None) -> str:
    """Streamlit reads $...$ as maths; money amounts must stay text."""
    return (text or "").replace("$", r"\$")


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


@st.cache_data(ttl=60, show_spinner=False)
def load_watchlist(_client: DeskClient) -> tuple[list[dict], dict]:
    return _client.watchlist(), _client.quotes()


@st.cache_data(ttl=30, show_spinner=False)
def load_feed(_client: DeskClient) -> list[dict]:
    return _client.feed(limit=10)


# ── sidebar: watchlist ──

def sidebar(client: DeskClient) -> None:
    with st.sidebar:
        st.header("Watchlist")
        if st.button("Refresh", width="stretch"):
            load_watchlist.clear()
            load_feed.clear()
        try:
            watchlist, quotes = load_watchlist(client)
        except DeskError as e:
            st.error(str(e))
            return
        for w in watchlist:
            q = quotes.get(w["ticker"], {})
            if q.get("error") or q.get("price") is None:
                st.metric(w["ticker"], "n/a", help=q.get("error"))
                continue
            st.metric(w["ticker"], f"${q['price']:,.2f}",
                      f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else None,
                      help=f"{w.get('name') or w['ticker']}: session {q.get('trading_date')}, "
                           f"source {q.get('source')}; alert at ±{w.get('pct_move')}%")
        st.caption("Quotes refresh every 60 s.")


# ── ask ──

def ask_panel(client: DeskClient) -> None:
    st.title("📈 Research Desk")
    with st.form("ask"):
        question = st.text_input("Question", placeholder=EXAMPLES[0])
        mode = st.radio("Mode", list(MODES), horizontal=True, captions=list(MODES.values()))
        submitted = st.form_submit_button("Ask", type="primary")
    st.caption("Try: " + " · ".join(f"“{e}”" for e in EXAMPLES))
    if submitted and question.strip():
        with st.status(f"Asking ({mode})…", expanded=False) as status:
            try:
                job_id = client.ask(question.strip(), mode.lower())
                result = client.wait(job_id, on_poll=lambda s: status.update(label=f"Working… {s:.0f} s"))
            except DeskError as e:
                status.update(label="Failed", state="error")
                st.error(str(e))
                return
            status.update(label="Done", state="complete")
        st.session_state.last = result
    if "last" in st.session_state:
        show_answer(st.session_state.last)


def show_answer(r: dict) -> None:
    if r.get("status") == "error" or not r.get("answer"):
        st.error(r.get("error") or "No answer.")
        return
    if r.get("notice"):
        st.info(r["notice"])
    st.markdown(md(r["answer"]))
    timings = r.get("timings") or {}
    st.caption(f"Mode: {r.get('mode')} · as of {r.get('as_of')} · {timings.get('total', '?')} s in the worker · "
               f"{timings.get('llm_calls', '?')} Gemini call(s)")
    sources = r.get("sources") or []
    with st.expander(f"Sources ({len(sources)})"):
        for s in sources:
            mark = "✅ " if s.get("cited") else ""
            label = md(s.get("label"))
            st.markdown(f"- {mark}[{label}]({s['url']})" if s.get("url") else f"- {mark}{label}")
    with st.expander("Trace"):
        show_trace(r)


def show_trace(r: dict) -> None:
    trace = r.get("trace")
    if not trace:   # Quick mode
        st.write("Tools:", ", ".join(r.get("used_tools") or []) or "none")
        st.json(r.get("timings") or {}, expanded=False)
        return
    if trace.get("fallback"):
        st.warning(f"Fell back to Quick: {trace['fallback']}")
    st.write("Path:", "fast path (one specialist, no planner)" if trace.get("fast_path") else "planner")
    if trace.get("plan"):
        st.write("Planned:", ", ".join(t["agent"] for t in trace["plan"].get("tasks", [])))
    for i, round_ in enumerate(trace.get("rounds") or [], start=1):
        for rep in round_:
            tools = [s.get("tool") for s in rep.get("steps", []) if s.get("action") == "tool"]
            st.write(f"Round {i} · **{rep['agent']}** · {rep.get('seconds')} s · {len(rep.get('findings', []))} "
                     f"finding(s) · {len(rep.get('gaps', []))} gap(s) · tools: {', '.join(tools) or 'none'}")
    checker = trace.get("checker") or {}
    if checker:
        st.write(f"Fact-checker: {checker.get('supported')}/{checker.get('numbers_checked')} numbers matched "
                 f"the evidence; derived {len(checker.get('derived') or [])}, "
                 f"unsupported {len(checker.get('unsupported') or [])}")


# ── alert feed ──

def feed_panel(client: DeskClient) -> None:
    st.subheader("Alert feed")
    try:
        events = load_feed(client)
    except DeskError as e:
        st.error(str(e))
        return
    if not events:
        st.caption("No alerts yet. The Watchdog scans the watchlist every 15 min in market hours.")
    for e in events:
        with st.container(border=True):
            st.markdown(f"**{md(e.get('title'))}** · {e.get('created_at', '')[:16].replace('T', ' ')} UTC")
            if e.get("body"):
                st.caption(md(e["body"]))


def main() -> None:
    api_url, code = secret("API_URL"), secret("ACCESS_CODE")
    if not api_url or not code:
        st.error("Missing secrets: set API_URL and ACCESS_CODE in Streamlit secrets "
                 "(locally: .streamlit/secrets.toml, see .streamlit/secrets.toml.example).")
        st.stop()
    require_access(code)
    client = get_client(api_url, code)
    sidebar(client)
    ask_panel(client)
    st.divider()
    feed_panel(client)


main()
