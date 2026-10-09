"""The test chatbot's one screen: an email, an optional question, Run, and what came of it.

Screens only. What a run does is in app/chat.py; what is logged is in app/runlog.py.
"""
import os
import sys

# Add the project root to sys.path so 'app' can be imported
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import streamlit as st  # noqa: E402

from app import access, chat, runlog  # noqa: E402
from app.backend.llm import provider  # noqa: E402
from app.config import settings  # noqa: E402
from app.guardrail import guardrail_io  # noqa: E402

RESULT = {"pass": "PASS", "review": "REVIEW", "fail": "FAIL", "needs review": "NEEDS REVIEW"}
QUESTION_HELP = ("Intent chat: type a question to test chat, for example 'write me a Python script' (should be refused) "
                 "or 'when does the vessel arrive?'. Leave the question empty and the email is read as the question.",
                 "Intent rfq: tests an incoming email, which is how the extraction pipeline works. The question is not used.")
# The extraction intents the picker offers after `chat`. The policy has more (GET /rules names them); one added here is offered too.
EMAIL_INTENTS = ("rfq",)

st.set_page_config(page_title="Guardrail test chatbot", page_icon="🤖", layout="wide")


# --- who may open it -----------------------------------------------------------------------------

def gate() -> None:
    """Stops the page unless this visitor may use it."""
    refused = access.refusal(settings, st.get_option("server.address"))
    if refused:
        st.error(refused)
        st.stop()
    if not access.password_wanted(settings) or st.session_state.get("authorised"):
        return
    st.title("Guardrail test chatbot")
    with st.form("password"):
        given = st.text_input("Shared password", type="password")
        if st.form_submit_button("Open") and access.password_matches(settings, given):
            st.session_state["authorised"] = True
            st.rerun()
        elif given:
            st.error("That is not the password.")
    st.stop()


# --- the top of the page -------------------------------------------------------------------------

@st.cache_data(ttl=15, show_spinner=False)
def policy() -> str | None:
    health = guardrail_io.get_json("/health")
    return str(health.get("rules_version")) if health and health.get("rules_version") else None


@st.cache_data(ttl=60, show_spinner=False)
def rules() -> dict:
    """The policy in force, as the guardrail gives it. Empty when the guardrail does not answer."""
    found = guardrail_io.get_json("/rules")
    return found if isinstance(found, dict) else {}


def intent_schemas() -> dict[str, dict | None]:
    """Each intent and the JSON Schema of its fields, from the policy in force."""
    output = rules().get("output")
    intents = output.get("intents") if isinstance(output, dict) else None
    return {name: (spec or {}).get("json_schema") for name, spec in sorted(intents.items())} if isinstance(intents, dict) else {}


def guardrail_prompt() -> str:
    """The system prompt the guardrail gives the model with every message, `{marker}` where its fence
    goes. Empty when the guardrail does not answer."""
    checks = rules().get("input")
    written = checks.get("system_instructions") if isinstance(checks, dict) else None
    return written if isinstance(written, str) else ""


def system_prompt(folder) -> str:
    """The tester's own system prompt, or "" while the box at the bottom of the page holds the guardrail's.
    An edit is kept beside the run log, so every tab has it and it is still there after a restart."""
    own, state = guardrail_prompt(), st.session_state
    if state.get("system_prompt") != state.get("system_prompt_seen"):
        # The tester has just changed the box in this tab: that is what is kept from now on, here and in every tab.
        state["system_prompt_here_only"] = not runlog.save_prompt(folder, chat.edited_prompt(state["system_prompt"], own))
        state["system_prompt_open"] = True      # and the box stays open here: the way back is a button inside it
    elif not state.get("system_prompt_here_only"):
        # Nothing was typed here since the last look: the box follows what is kept, by this tab or by another.
        state["system_prompt"] = runlog.saved_prompt(folder) or own
    state["system_prompt_seen"] = state["system_prompt"]
    return chat.edited_prompt(state["system_prompt"], own)


def log_folder():
    """The folder runs are logged to, or None with the reason on the page."""
    try:
        return runlog.folder(settings.log_dir)
    except runlog.LogRefused as refused:
        st.error(f"Runs cannot be logged, so Run is switched off. {refused}")
        return None


def header(folder) -> None:
    st.title("Guardrail test chatbot")
    going_to = provider.describe(settings)
    stored = ["the guardrail's database, for a day", f"the run log on this machine ({folder / runlog.FILE})" if folder else None]
    st.warning("**Use fake or anonymised emails only.** Whatever you paste is kept in "
               + " and in ".join(place for place in stored if place)
               + (". When it passes the guardrail it is also **sent to a model provider**: " + "; if that call fails, to ".join(going_to) + "."
                  if going_to else ". No model provider is connected, so it is sent nowhere else."))
    if provider.problem(settings):
        st.error(provider.problem(settings))
    left, right = st.columns(2)
    label = policy()
    left.markdown(f"**Guardrail policy:** `{label}`" if label else "**Guardrail policy:** unknown. The guardrail did not answer.")
    left.caption("Every message is on its own. Nothing from an earlier message is sent to the guardrail or to a model.")
    if going_to:
        right.markdown("**Model:** " + ", then ".join(f"`{p.id}`" for p in provider.configured(settings)))
        right.caption("Called only after the guardrail answers pass." if not settings.call_model_on_review else
                      "Called after a pass, and after a review where the backend calls it too (CHATBOT_CALL_MODEL_ON_REVIEW=1).")
    else:
        right.markdown("**Model:** none (guardrail only)")
        right.caption("The message is checked, the prompt that would be sent is shown, and no model is called.")


# --- one run ---------------------------------------------------------------------------------------

def form(folder) -> None:
    edited = system_prompt(folder)  # read here, before Run; the box itself is at the bottom of the page
    st.text_area("Email text", key="email", height=220, placeholder="Paste the test email here.")
    st.text_area("Attachment text (optional)", key="attachment", height=120,
                 placeholder="The text an attached PDF or scan would contain, if the email has one.")
    # One row: the question, and beside it the intent, which says what kind of test this is.
    choices = [chat.CHAT, *EMAIL_INTENTS]
    if st.session_state.get("intent") not in choices:
        st.session_state["intent"] = chat.CHAT
    ask, pick = st.columns([5, 2])
    ask.text_input("Question (optional)", key="question", placeholder="What a member of staff asks. Left empty, the email is the question.")
    pick.selectbox("Intent", choices, key="intent",
                   help="chat: a member of staff asks a question. "
                        "rfq: an incoming email, handled as the extraction pipeline handles a request for a quote; "
                        "the model extracts its fields.")
    st.caption("  \n".join(QUESTION_HELP))
    if edited:
        st.caption(":orange[An edited system prompt is in use.] It is at the bottom of the page.")
    if not st.button("Run", type="primary", disabled=folder is None):
        return
    picked = st.session_state["intent"]
    message = chat.Message(email=st.session_state["email"], attachment=st.session_state["attachment"],
                           question=st.session_state["question"], intent="" if picked == chat.CHAT else picked)
    email_run = chat.mode(message) == chat.EMAIL
    if email_run and not (message.email.strip() or message.attachment.strip()):
        st.error("Paste an email to test.")
        return
    if not email_run and not (message.question.strip() or message.email.strip()):
        st.error("Type a question, or paste an email to be read as the question.")
        return
    try:
        model = provider.model_for(settings)
    except Exception as problem:  # noqa: BLE001  a provider's client can refuse its settings in many ways
        st.error(f"The model could not be set up ({type(problem).__name__}). Nothing was sent anywhere.")
        return
    if edited and model is not None:
        model = chat.EditedSystemPrompt(model, edited)
    with st.spinner("Checking"):
        run = chat.run_message(message, model, call_model_on_review=settings.call_model_on_review,
                               schema=intent_schemas().get(message.intent) if email_run else None)
    last = {"run": run, "answered_by": getattr(model, "answered_by", None), "logged": None, "log_problem": None,
            "email_is_question": chat.email_is_question(message), "system_prompt": edited,
            "system_sent": getattr(model, "sent", None)}
    try:
        last["logged"] = str(runlog.append(folder, chat.log_row(message, run, system_prompt=edited)))
    except OSError as problem:
        last["log_problem"] = type(problem).__name__
    st.session_state["last"] = last
    st.session_state["note"] = ""


def show(last: dict, folder) -> None:
    run = last["run"]
    status = chat.status(run)
    email_run = chat.mode_of(run) == chat.EMAIL
    with st.container(border=True):
        # First, right under Run, what a tester looks for: what came back. Then how the guardrail scored it.
        st.markdown("**What happens to the email**" if email_run else "**What the user would see**")
        st.caption(run.outcome.title + ".")
        if run.outcome.shown is not None:
            st.code(run.outcome.shown, language=None, wrap_lines=True)
        else:
            if not email_run:
                st.write("No text was shown to a user in this run.")
            for line in run.outcome.detail:
                st.write(f"- {line}")
            hint = chat.hint(run, [p.model_variable for p in provider.configured(settings)])
            if hint:
                st.info(hint)

        st.markdown("**The model's extraction**" if email_run else "**The model's reply**")
        if run.model_reply is not None:
            st.caption("As the model wrote it, before the guardrail checked it"
                       + (f" · answered by `{last['answered_by']}`" if last["answered_by"] else "") + ".")
            st.code(run.model_reply, language=None, wrap_lines=True)
        elif run.model_called:
            st.write("The model was called and gave no reply.")
        elif run.model_mode == "none":
            st.write("No model was called. None is connected (guardrail only).")
        else:
            # Said in full: a withheld reply reads like a fault, and a tester then suspects their key.
            st.write("No model was called. The message did not pass the guardrail, so nothing was sent to the model, "
                     "and your key was not used.")

        st.divider()
        st.caption(f"{run.started_at} · request id `{run.request_ids[0]}` · policy `{run.policy or 'unknown'}` · model `{run.model_mode}`"
                   + (f" · an incoming email, intent `{run.test.intent}`" if email_run else
                      " · chat, the email read as the question" if last.get("email_is_question") else " · chat")
                   + (" · system prompt edited" if last.get("system_prompt") else ""))
        st.markdown(f"## Result: {RESULT[status]}")
        st.write(chat.IN_PLAIN_WORDS[status])

        st.markdown("**Rules that fired**")
        findings = [(call.stage, finding) for call in run.calls for finding in call.findings]
        if findings:
            # A plain table: a tester copies rule ids out of it.
            st.table([{"rule id": f["rule"] or f["check"], "asks for": f["action"],
                       "found in": chat.found_in(f["source"], f["field"], stage),
                       "what the rule is about": f["message"]} for stage, f in findings])
        else:
            st.write("None.")
        for name in chat.rule_ids(run)[len(run.rules_fired):]:
            st.write(f"The model's reply did not pass a check ({name.removeprefix('reply check: ')}).")
        for call in run.calls:
            if call.unavailable:
                st.write(f"The guardrail could not be used for this step ({call.unavailable}). That is never treated as a pass.")

        if run.prompt:
            with st.expander("The prompt " + ("sent to the model" if run.model_called else "that would be sent to the model"),
                             expanded=not run.model_called and run.outcome.kind == "prompt_ready"):
                if last.get("system_prompt"):
                    st.caption("System prompt (edited: yours, not the guardrail's own)")
                    st.code(last.get("system_sent") or chat.system_prompt_sent(last["system_prompt"], run.prompt["system_instructions"]),
                            language=None, wrap_lines=True)
                else:
                    st.caption("System prompt")
                    st.code(run.prompt["system_instructions"], language=None, wrap_lines=True)
                st.caption("User message")
                st.code(run.prompt["user_message"], language=None, wrap_lines=True)

        st.divider()
        if last["log_problem"]:
            st.error(f"This run was NOT written to the run log ({last['log_problem']}).")
            return
        st.caption(f"Logged as one row in `{last['logged']}`.")
        st.text_input("Notes for this run (optional)", key="note")
        if st.button("Save note"):
            if runlog.set_note(folder, run.request_ids[0], st.session_state["note"]):
                st.success("Note saved in the run log.")
            else:
                st.error("That run is no longer in the run log.")


# --- the system prompt, at the bottom ---------------------------------------------------------------

def _guardrails_own_again() -> None:
    # Remembered in this tab as well, for where nothing can be kept on the machine.
    st.session_state["system_prompt_set_aside"] = st.session_state.get("system_prompt", "")
    st.session_state["system_prompt"] = guardrail_prompt()


def _my_prompt_again(set_aside: str) -> None:
    st.session_state["system_prompt"] = set_aside


def system_prompt_editor(folder) -> None:
    own = guardrail_prompt()
    edited = chat.edited_prompt(st.session_state.get("system_prompt", ""), own)
    # Open whenever an edited prompt is in use, so that one is never at work out of sight, and in a tab
    # where the tester has changed it, so that going back does not shut the box on the button that undoes it.
    with st.expander("System prompt (click to edit)", expanded=bool(edited) or bool(st.session_state.get("system_prompt_open"))):
        st.caption("This is the system prompt the guardrail gives the model with every message. Edit it to try your own: the "
                   "model is then sent your text in its place. Keep `{marker}` where the guardrail's fence should go; that is the "
                   "line it puts around the documents. The guardrail's own checks do not change, and its check of the reply "
                   "expects the model to answer REFUSE and NO_ANSWER as this prompt tells it to.")
        if not own:
            st.caption("The guardrail did not answer, so its own prompt cannot be shown here.")
        st.text_area("System prompt", key="system_prompt", height=320, label_visibility="collapsed")
        if edited:
            kept = chat.edited_prompt(runlog.saved_prompt(folder), own) == edited
            st.warning("Your edited system prompt is in use, for every run until you go back. Runs with it are marked on the "
                       "screen and in the run log. "
                       + ("It is kept on this machine, beside the run log: it is still in use after a reload or a restart, and in "
                          "every browser tab that opens this app." if kept else
                          "It could not be kept on this machine, so it lasts for this browser tab only."))
            st.button("Go back to the guardrail's own", on_click=_guardrails_own_again)
            st.caption("Going back does not throw your prompt away. \"Go back to my prompt\" will be here to bring it back.")
            return
        # The guardrail's own is in use. A prompt the tester set aside by going back is one press away.
        set_aside = chat.edited_prompt(runlog.set_aside_prompt(folder) or st.session_state.get("system_prompt_set_aside", ""), own)
        if set_aside:
            st.caption("In use: the guardrail's own. Your own prompt is set aside, not thrown away.")
            st.button("Go back to my prompt", on_click=_my_prompt_again, args=(set_aside,))
        else:
            st.caption("In use: the guardrail's own.")


gate()
folder = log_folder()
header(folder)
form(folder)
if st.session_state.get("last"):
    show(st.session_state["last"], folder)
system_prompt_editor(folder)
