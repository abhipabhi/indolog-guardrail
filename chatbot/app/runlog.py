"""The run log: every run as one row of a CSV file, where git will not pick it up.

The folder comes from CHATBOT_LOG_DIR; start.sh sets it to `logs` inside this tool's folder unless
the settings file names another. The file holds the test emails themselves, so:

  * It must never be committed. A folder inside the repository is used only where git ignores the
    log file (this folder's .gitignore ignores `logs/` and every `*.csv`); anywhere else inside the
    repository is refused. A folder outside the repository is always allowed.
  * Running with no folder at all is refused too: a run that cannot be logged is not started.
  * The file is opened in a spreadsheet, and the text in it is written by people trying to attack
    a model. A cell that a spreadsheet would run as a formula (it starts with = + - or @) gets a
    leading apostrophe, which a spreadsheet shows as plain text.

One file, `chatbot-runs.csv`, one row a run. `mode` says which kind of run it was: `email` (no
question: the email is handled as an incoming email is) or `chat`. The last column, `notes`, is the
tester's: it is filled in from the screen (see `set_note`), or by hand in a spreadsheet once the
app is stopped.

`system_prompt` is empty unless the tester gave the model a system prompt of their own, and then holds it.

A log begun before a column was added is given it the next time it is written to.
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import tempfile
import threading
from pathlib import Path

FILE = "chatbot-runs.csv"
PROMPT_FILE = "system-prompt.txt"       # a tester's own system prompt, kept beside the log
SET_ASIDE_FILE = "system-prompt.set-aside.txt"      # the same, while the guardrail's own is in use again
COLUMNS = ["time", "request_id", "mode", "intent", "input_text", "question", "status", "rule_ids", "user_saw",
           "model_reply", "system_prompt", "notes"]
# The columns this app wrote before others were added, oldest first. In the first of them every
# run was a chat run: a question was required.
_EARLIER = (
    ["time", "request_id", "input_text", "question", "status", "rule_ids", "user_saw", "model_reply", "notes"],
    ["time", "request_id", "mode", "intent", "input_text", "question", "status", "rule_ids", "user_saw", "model_reply", "notes"],
)
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_writing = threading.Lock()      # one process serves every tester; rows are written one at a time


class LogRefused(Exception):
    """There is nowhere the log may be written. The message says what to set."""


def repository_root(start: Path | None = None) -> Path | None:
    """The git checkout this file is in, or None when it is not in one."""
    here = (start or Path(__file__)).resolve()
    return next((folder for folder in [here, *here.parents] if (folder / ".git").exists()), None)


def _ignored(path: Path, repository: Path) -> bool:
    """Whether git ignores this file, so that an ordinary `git add` cannot commit it. False when git cannot say."""
    try:
        asked = subprocess.run(["git", "-C", str(repository), "check-ignore", "-q", str(path)], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return asked.returncode == 0


def folder(configured: str, repository: Path | None = None) -> Path:
    """The folder the log is written to, created if need be."""
    if not configured:
        raise LogRefused("CHATBOT_LOG_DIR is not set, so there is nowhere to log a run. Start the app with ./start.sh, "
                         "or set it to a folder and start the app again.")
    where = Path(configured).expanduser().resolve()
    repository = repository_root() if repository is None else repository.resolve()
    inside = repository is not None and (where == repository or repository in where.parents)
    if inside and not _ignored(where / FILE, repository):          # asked before anything is created there
        raise LogRefused(f"{where} is inside the repository and git does not ignore it. The log holds test emails and "
                         "is never committed: use this tool's own logs folder, or set CHATBOT_LOG_DIR to a folder outside "
                         "the repository.")
    try:
        where.mkdir(parents=True, exist_ok=True)
    except OSError as problem:
        raise LogRefused(f"{where} cannot be created ({type(problem).__name__}).") from None
    if not os.access(where, os.W_OK):
        raise LogRefused(f"{where} cannot be written to.")
    return where


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _out_of_gits_reach(where: Path, path: Path) -> bool:
    """Whether a file kept here cannot be committed by an ordinary `git add`: the same rule as the log's."""
    repository = repository_root()
    inside = repository is not None and (where == repository or repository in where.parents)
    return not inside or _ignored(path, repository)


def saved_prompt(where: Path | None) -> str:
    """The system prompt a tester wrote in place of the guardrail's, kept beside the run log so that
    a reload, a restart or another browser tab still has it. "" when there is none."""
    return _read(where / PROMPT_FILE) if where is not None else ""


def set_aside_prompt(where: Path | None) -> str:
    """The tester's prompt as it was when they went back to the guardrail's own. It is kept, so that
    going back is not the end of it. "" when there is none."""
    return _read(where / SET_ASIDE_FILE) if where is not None else ""


def save_prompt(where: Path | None, text: str) -> bool:
    """Keeps the tester's system prompt. With "" the guardrail's own is in use again, and the
    tester's prompt is set aside, not thrown away. False when it cannot be kept: there is no log
    folder, it cannot be written, or it is where git would pick the file up."""
    if where is None:
        return False
    path, aside = where / PROMPT_FILE, where / SET_ASIDE_FILE
    try:
        if not text:
            if _read(path).strip() and _out_of_gits_reach(where, aside):
                os.replace(path, aside)
            else:
                path.unlink(missing_ok=True)
            return True
        if not _out_of_gits_reach(where, path):
            return False
        handle, temporary = tempfile.mkstemp(dir=where, prefix=".system-prompt-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(text)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        if _read(aside).strip() == text.strip():        # it is in use again: nothing is set aside any more
            aside.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def safe(value: object) -> str:
    """A cell a spreadsheet will show and not run."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


def _replace(where: Path, logged: list[dict]) -> None:
    """Writes the whole log again. The file is replaced in one step, so a reader never sees half of it."""
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(logged)
    handle, temporary = tempfile.mkstemp(dir=where, prefix=".chatbot-runs-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8-sig") as file:
            file.write(text.getvalue())
        os.replace(temporary, where / FILE)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _bring_up_to_date(where: Path) -> None:
    """Gives a log with an earlier set of columns the ones added since. Rows from before `mode` are marked `chat`."""
    path = where / FILE
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames not in _EARLIER:
            return
        before_modes = "mode" not in reader.fieldnames
        logged = list(reader)
    _replace(where, [{**row, "mode": "chat"} if before_modes else row for row in logged])


def append(where: Path, row: dict) -> Path:
    """Adds one run. `row` holds the columns by name; one that is missing is left empty."""
    path = where / FILE
    with _writing:
        _bring_up_to_date(where)
        new = not path.exists() or path.stat().st_size == 0
        # A byte-order mark, once, so a spreadsheet reads the file as UTF-8 (test emails are not all English).
        with path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as file:
            writer = csv.writer(file)
            if new:
                writer.writerow(COLUMNS)
            writer.writerow([safe(row.get(column, "")) for column in COLUMNS])
    return path


def rows(where: Path) -> list[dict]:
    path = where / FILE
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def set_note(where: Path, request_id: str, note: str) -> bool:
    """Writes the tester's note into the row of that run. False when the run is not in the log."""
    with _writing:
        _bring_up_to_date(where)
        logged = rows(where)
        found = [row for row in logged if row.get("request_id") == request_id]
        if not found:
            return False
        for row in found:
            row["notes"] = safe(note)
        _replace(where, logged)
    return True
