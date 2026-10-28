#!/usr/bin/env bash
# Starts the guardrail test chatbot on this machine, with its settings read from one private file.
#
#   ./start.sh
#   ./start.sh --langfuse     the same, after asking for the keys of the testers' Langfuse project
#
# The settings file is $HOME/.guardrail-chatbot.env: NAME=value lines, as .env.example shows
# (README.md).
#
#   * It is outside the repository, so a key in it cannot be committed.
#   * It is readable by you alone. The first run asks for your Gemini key without showing what
#     you type, and writes the file that way (chmod 600). The script refuses to start while
#     anyone else can read or change the file.
#   * It is the only place settings come from. What is exported in your shell is not used, so a
#     key that is there for other work does not connect a model by surprise.
#   * It is read as text, not run as a script: each value is taken exactly as written.
#
# The first run also installs the app, into .venv in this folder, with uv (https://docs.astral.sh/uv):
# `uv sync` installs the chatbot and the guardrail beside it (../guardrail) at the versions in
# uv.lock. uv brings Python 3.11 with it when this machine has none, so there is no Python to
# install and no virtual environment to make by hand. After an update that changes uv.lock, the
# next start brings .venv up to date.
#
# The guardrail: when one answers at GUARDRAIL_URL (http://localhost:8001 unless the settings file
# says otherwise), for example the one Docker runs, that one is used. When none does, one is started
# here from .venv, without Docker, and stopped again with the chatbot. On Windows, start.ps1 does the same.
#
# Langfuse: with LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in the settings file (./start.sh --langfuse
# writes them there), every run is sent to the testers' Langfuse project in full: the email, the
# question, the prompts and the replies (app/langfuse_trace.py). A guardrail started here sends its own
# spans for each check to the same project, inside the run's trace. That project is not the platform's.
#
# The app listens on localhost only.
# No value from the settings file, and nothing you type at the prompt, is ever printed.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd -P)
SETTINGS="$HOME/.guardrail-chatbot.env"
# Every setting the app reads. A name in the file that is not one of these is a typing mistake.
KNOWN="CHATBOT_MODEL GOOGLE_API_KEY OPENAI_API_KEY GROQ_API_KEY OLLAMA_BASE_URL CHATBOT_LOG_DIR GUARDRAIL_URL
GUARDRAIL_TIMEOUT_MS CHATBOT_CALL_MODEL_ON_REVIEW CHATBOT_MODEL_TIMEOUT_SECONDS CHATBOT_GEMINI_MODEL
CHATBOT_OPENAI_MODEL CHATBOT_GROQ_MODEL CHATBOT_OLLAMA_MODEL CHATBOT_PASSWORD
LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_BASE_URL CHATBOT_TESTER"
LANGFUSE_NAMES="LANGFUSE_PUBLIC_KEY|LANGFUSE_SECRET_KEY|LANGFUSE_BASE_URL|CHATBOT_TESTER"
stop() { echo "start.sh: $1" >&2; exit 1; }

ASK_LANGFUSE=""
for argument in "$@"; do
    case "$argument" in
        --langfuse) ASK_LANGFUSE=1 ;;
        *) stop "the one option is --langfuse, which asks for the keys of the testers' Langfuse project. Start with  ./start.sh  or  ./start.sh --langfuse" ;;
    esac
done

# --- the app itself: installed by uv into .venv here, the first time and after an update ------------
# uv's installer puts it in ~/.local/bin, which a terminal opened before the install does not
# search yet, so it is looked for there as well.
UV=$(command -v uv 2>/dev/null || true)
if [ -z "$UV" ]; then
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then UV=$candidate; break; fi
    done
fi
INSTALLED="$HERE/.venv/uv.lock.installed"           # a copy of what was installed, to tell when an update changes it
install_app() {
    # --frozen: exactly what uv.lock says, never a newer version. uv makes .venv, or makes it again
    # with Python 3.11 when it was made with another.
    "$UV" sync --frozen --project "$HERE" \
        || stop "uv could not install the app; its own message is above. Check the internet connection and run ./start.sh again."
    cp "$HERE/uv.lock" "$INSTALLED"
}
if [ ! -x "$HERE/.venv/bin/streamlit" ]; then
    [ -n "$UV" ] || stop "the app is not installed yet, and uv, which installs it, was not found. Install uv with:  curl -LsSf https://astral.sh/uv/install.sh | sh   Then run ./start.sh again."
    echo "Installing the app into $HERE/.venv. This happens once and takes a minute or two, longer on a"
    echo "slow connection: it downloads about 90 packages (about 500 MB on disk), and Python 3.11 if"
    echo "this machine does not have it."
    install_app
    echo "Installed."
    echo
elif [ -n "$UV" ] && ! cmp -s "$HERE/uv.lock" "$INSTALLED"; then
    # An update changed uv.lock, or .venv was made before this script installed with uv sync (then
    # the guardrail is added to it, and anything uv.lock does not hold is taken out).
    [ ! -e "$INSTALLED" ] || echo "uv.lock has changed since the app was installed. Bringing it up to date."
    install_app
fi
# Without uv, a .venv that already holds the app (made by hand with pip) is started as it is.

# --- the settings file: outside the repository, a plain file, yours, and private -------------------
REPOSITORY=$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || true)
case "$(cd "$HOME" && pwd -P)/" in
    "${REPOSITORY:-/nonexistent}"/*) stop "your home folder is inside this repository, so $SETTINGS would be too. Keys are kept outside it." ;;
esac

if [ ! -e "$SETTINGS" ] && [ ! -L "$SETTINGS" ]; then
    echo "There is no settings file yet ($SETTINGS)."
    echo "Paste your own Gemini API key and press Enter. What you paste is not shown."
    echo "To start without a model (guardrail only), just press Enter."
    key=""
    IFS= read -rs -p "Gemini API key: " key || true
    echo
    key=${key#"${key%%[![:space:]]*}"}                      # spaces pasted round it
    key=${key%"${key##*[![:space:]]}"}
    case "$key" in
        *[[:space:]]*|'<'*'>'|\"*|\'*) stop "that does not look like a key: it has spaces, quotes or < > in it. Nothing was written. Start again and paste the key itself." ;;
    esac
    # Written by the shell itself, so the key is never an argument of another program.
    (
        umask 077
        {
            printf '%s\n' "# Settings for the guardrail test chatbot. Written by start.sh. Keep this file private."
            printf '%s\n' "# Every setting is listed in .env.example. One NAME=value a line; a value is taken as written."
            if [ -n "$key" ]; then
                printf '%s\n' "CHATBOT_MODEL=gemini"
                printf 'GOOGLE_API_KEY=%s\n' "$key"
            else
                printf '%s\n' "CHATBOT_MODEL=none"
            fi
        } > "$SETTINGS"
    )
    chmod 600 "$SETTINGS"
    if [ -n "$key" ]; then
        echo "Wrote $SETTINGS, readable by you alone, with your key and the model set to gemini."
    else
        echo "Wrote $SETTINGS, readable by you alone, with no key: the app starts guardrail-only."
        echo "To connect Gemini later, add your key to that file (see .env.example) and start again."
    fi
    key=""
    echo
fi

[ ! -L "$SETTINGS" ] || stop "$SETTINGS is a link. Keep the settings in a real file there."
[ -f "$SETTINGS" ] || stop "$SETTINGS is not a file."
[ -O "$SETTINGS" ] || stop "$SETTINGS does not belong to you."
MODE=$(ls -ln "$SETTINGS" | cut -c1-11)
# Characters 5 to 10 are what your group and everyone else may do; a + after them is an access list.
case "$MODE" in
    ????------+*) stop "$SETTINGS has an access list, so others may be able to read it. Remove it (chmod -N on macOS, setfacl -b on Linux) and start again." ;;
    ????------*) ;;
    *) stop "$SETTINGS can be read or changed by others (it is $(echo "$MODE" | cut -c1-10)). It holds keys. Run:  chmod 600 \"$SETTINGS\"  and start again." ;;
esac

# --- ./start.sh --langfuse: the testers' Langfuse project, written into the settings file ---------
trimmed() {
    local value=$1
    value=${value#"${value%%[![:space:]]*}"}
    printf '%s' "${value%"${value##*[![:space:]]}"}"
}
if [ -n "$ASK_LANGFUSE" ]; then
    echo "Connecting the testers' Langfuse project. Its owner gives you the two keys, and its address when it"
    echo "is not in Langfuse Cloud's EU region. From now on every run is sent there in full: the email, the"
    echo "question, the prompts and the replies. Fake or anonymised emails only, as always."
    public="" secret="" address="" tester=""
    IFS= read -r -p "Public key (pk-lf-...): " public || true
    IFS= read -rs -p "Secret key (sk-lf-..., not shown): " secret || true
    echo
    IFS= read -r -p "Address (press Enter for https://cloud.langfuse.com): " address || true
    IFS= read -r -p "Your name or initials, shown on your runs (press Enter to leave it out): " tester || true
    public=$(trimmed "$public"); secret=$(trimmed "$secret"); address=$(trimmed "$address"); tester=$(trimmed "$tester")
    case "$public" in
        pk-lf-*[![:alnum:]-]*|pk-lf-) stop "that does not look like a public key. Nothing was written. Run ./start.sh --langfuse again." ;;
        pk-lf-*) ;;
        *) stop "a public key starts with pk-lf-. Nothing was written. Run ./start.sh --langfuse again." ;;
    esac
    case "$secret" in
        sk-lf-*[![:alnum:]-]*|sk-lf-) stop "that does not look like a secret key. Nothing was written. Run ./start.sh --langfuse again." ;;
        sk-lf-*) ;;
        *) stop "a secret key starts with sk-lf-. Nothing was written. Run ./start.sh --langfuse again." ;;
    esac
    address=${address%/}
    case "$address" in
        '') ;;
        https://*[![:alnum:]./:-]*|http://*[![:alnum:]./:-]*) stop "that address has something in it an address does not. Nothing was written." ;;
        https://?*|http://localhost:*|http://127.0.0.1:*) ;;
        *) stop "the address starts with https:// (for example https://us.cloud.langfuse.com). Nothing was written." ;;
    esac
    case "$tester" in
        *[!\ [:alnum:]._-]*) stop "use letters, digits, spaces, dots and dashes in your name. Nothing was written." ;;
    esac
    # The other settings are kept as they are. Written by the shell itself into a new private file,
    # which then takes the place of the old one, so the keys are never an argument of another program.
    fresh=$(umask 077 && mktemp "$SETTINGS.XXXXXX") || stop "a new settings file could not be made beside $SETTINGS."
    {
        grep -v -E "^[[:space:]]*(export[[:space:]]+)?($LANGFUSE_NAMES)[[:space:]]*=|^# The testers' Langfuse project" "$SETTINGS" || true
        printf '%s\n' "# The testers' Langfuse project. Written by start.sh --langfuse; run it again to change it."
        printf 'LANGFUSE_PUBLIC_KEY=%s\n' "$public"
        printf 'LANGFUSE_SECRET_KEY=%s\n' "$secret"
        [ -z "$address" ] || printf 'LANGFUSE_BASE_URL=%s\n' "$address"
        [ -z "$tester" ] || printf 'CHATBOT_TESTER=%s\n' "$tester"
    } > "$fresh"
    chmod 600 "$fresh"
    mv -f "$fresh" "$SETTINGS"
    secret=""
    echo "Wrote the Langfuse keys into $SETTINGS. Every start from now on sends runs to Langfuse;"
    echo "to stop that, take the two LANGFUSE_ lines out of that file."
    echo
fi

# --- settings come from that file and nowhere else ------------------------------------------------
# Langfuse's and OpenTelemetry's own variables too: a key or an endpoint in your shell for other
# work (the platform's Langfuse, say) never takes a test run anywhere.
for name in $(env | sed -E -n 's/^((CHATBOT|LANGFUSE|OTEL)_[A-Za-z0-9_]*)=.*/\1/p') $KNOWN; do
    unset "$name"
done

# Read as text. Nothing in the file is run, and a mistake is said by its line and its NAME, never
# by its value.
NAMES=""
number=0
RETURN=$(printf '\r')
while IFS= read -r line || [ -n "$line" ]; do
    number=$((number + 1))
    line=${line%"$RETURN"}                                  # a file saved on Windows
    case "$line" in
        *[![:space:]]*) ;;
        *) continue ;;                                      # blank
    esac
    trimmed=${line#"${line%%[![:space:]]*}"}
    case "$trimmed" in
        '#'*) continue ;;                                   # a comment
        'export '*) trimmed=${trimmed#export }; trimmed=${trimmed#"${trimmed%%[![:space:]]*}"} ;;
    esac
    case "$trimmed" in
        *=*) ;;
        *) stop "line $number of $SETTINGS is not NAME=value." ;;
    esac
    name=${trimmed%%=*}
    name=${name%"${name##*[![:space:]]}"}                   # spaces around the = and at the end of the
    value=${trimmed#*=}                                     # line are not part of the name or the value
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    case " $(echo $KNOWN) " in
        *" $name "*) ;;
        *) stop "line $number of $SETTINGS sets a name this app does not read. The names are in .env.example." ;;
    esac
    case "$value" in                                        # one pair of quotes around a value is taken off
        \"*\") value=${value#\"}; value=${value%\"} ;;
        \'*\') value=${value#\'}; value=${value%\'} ;;
    esac
    case "$value" in
        '<'*'>') stop "$name on line $number of $SETTINGS is written inside < >. Write the value itself, without the brackets." ;;
    esac
    export "$name=$value"
    [ -z "$value" ] || NAMES="$NAMES $name"
done < "$SETTINGS"

# Every run is logged, so there must be somewhere to log it: the logs folder here, which git
# ignores, unless the settings file names another. The app checks that it cannot be committed.
# A folder may be written from the home folder as ~/... or $HOME/...; nothing else is expanded.
CHATBOT_LOG_DIR=${CHATBOT_LOG_DIR:-$HERE/logs}
case "$CHATBOT_LOG_DIR" in
    '~'/*) CHATBOT_LOG_DIR="$HOME/${CHATBOT_LOG_DIR#'~'/}" ;;
    '$HOME'/*) CHATBOT_LOG_DIR="$HOME/${CHATBOT_LOG_DIR#'$HOME'/}" ;;
esac
export CHATBOT_LOG_DIR

# --- Langfuse: on when both keys are set ------------------------------------------------------------
LANGFUSE_AUTH=""
if [ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ]; then
    LANGFUSE_AT=${LANGFUSE_BASE_URL:-https://cloud.langfuse.com}
    LANGFUSE_AT=${LANGFUSE_AT%/}
    # What the guardrail started below sends with its spans: Basic, public key:secret key. Made by the
    # shell's own printf, so the keys are never an argument of another program; kept unexported.
    LANGFUSE_AUTH=$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 | tr -d '\n')
elif [ -n "${LANGFUSE_PUBLIC_KEY:-}${LANGFUSE_SECRET_KEY:-}" ]; then
    echo "NOTE: only one of the two Langfuse keys is in $SETTINGS, so Langfuse is off. Run ./start.sh --langfuse to set both."
fi

# --- the guardrail: the one that answers at GUARDRAIL_URL, or one started here ---------------------
GUARDRAIL=${GUARDRAIL_URL:-http://localhost:8001}
GUARDRAIL=${GUARDRAIL%/}
answers() {
    "$HERE/.venv/bin/python" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1] + "/health", timeout=2)' \
        "$GUARDRAIL" >/dev/null 2>&1
}
GUARDRAIL_PID=""
if answers; then
    echo "Using the guardrail at $GUARDRAIL."
    [ -z "$LANGFUSE_AUTH" ] || echo "It was not started here, so its own spans are not in Langfuse. Each run's calls to it are."
    # One left running from before, Docker's above all, keeps the policy it was given, whatever this
    # copy now holds. Say so when the two differ: results from an older policy are not this one's.
    running=$("$HERE/.venv/bin/python" -c 'import json, sys, urllib.request; print(json.load(urllib.request.urlopen(sys.argv[1] + "/health", timeout=5)).get("rules_version") or "")' \
        "$GUARDRAIL" 2>/dev/null || true)
    this_copy=$("$HERE/.venv/bin/python" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' \
        "$HERE/../guardrail/rules.rollout.json" 2>/dev/null || true)
    if [ -n "$running" ] && [ -n "$this_copy" ] && [ "$running" != "$this_copy" ]; then
        echo
        echo "NOTE: that guardrail runs policy $running, and this copy's policy is $this_copy. It is most likely"
        echo "an older guardrail still running in Docker. To test this copy's, stop it and start again, and this"
        echo "script starts its own:  cd $(cd "$HERE/../guardrail" && pwd -P) && docker compose down"
        echo
    fi
else
    case "$GUARDRAIL" in                                    # only a guardrail on this machine is started here
        http://localhost:*|http://127.0.0.1:*) PORT=${GUARDRAIL##*:} ;;
        *) PORT="" ;;
    esac
    case "$PORT" in
        ''|*[!0-9]*) PORT="" ;;
    esac
    if [ -n "$PORT" ] && "$HERE/.venv/bin/python" -c 'import guardrail' 2>/dev/null; then
        RULES=$(cd "$HERE/../guardrail" && pwd -P)/rules.rollout.json
        mkdir -p "$CHATBOT_LOG_DIR"
        # A program that already listens on the port, and does not answer as a guardrail, keeps the one
        # started here from opening it. Say which program before trying.
        # Opening the port is the test that does not depend on the other program answering.
        if ! "$HERE/.venv/bin/python" -c 'import socket, sys; socket.socket().bind(("127.0.0.1", int(sys.argv[1])))' \
                "$PORT" 2>/dev/null; then
            program=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR == 2 {print $1 ", process " $2}')
            stop "port $PORT is already in use by another program${program:+ ($program)}, which does not answer as a guardrail, so none can be started here. If it is a guardrail left from an earlier start, stop that program; if it is the Docker guardrail, wait until it is ready. Otherwise give the guardrail another port: add the line  GUARDRAIL_URL=http://localhost:18001  to $SETTINGS. Then run ./start.sh again."
        fi
        echo "No guardrail answers at $GUARDRAIL, so one is started here, without Docker: the rollout"
        echo "policy, as the Docker image runs it, with no database and without the classifier, which"
        echo "only observes in that policy. It stops when the chatbot stops. Its log: $CHATBOT_LOG_DIR/guardrail.log"
        [ -z "$LANGFUSE_AUTH" ] || echo "It sends its own spans for each check to Langfuse, inside each run's trace."
        # A clean environment: the guardrail needs no key, so it is given none. With Langfuse on, it is
        # told where its spans go (guardrail/guardrail/tracing.py): exported here, in the shell itself,
        # and never on a command line, where another program could read them.
        (
            # Nothing is passed on but what is exported below. Un-exported, not unset: the script's own
            # values (PORT, say, which a shell may export for other work) are still here to use.
            for name in $(compgen -e); do export -n "$name" 2>/dev/null || true; done
            export PATH HOME LANG="${LANG:-C.UTF-8}" GUARDRAIL_RULES="$RULES"
            if [ -n "$LANGFUSE_AUTH" ]; then
                export OTEL_EXPORTER_OTLP_ENDPOINT="$LANGFUSE_AT/api/public/otel" OTEL_SERVICE_NAME=indolog-guardrail \
                       OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20$LANGFUSE_AUTH"
                # A Python with no certificates of its own (python.org's, on a Mac) cannot check
                # Langfuse's: the exporter is given certifi's (app/langfuse_trace.py, trust_for_traces).
                certificates=$("$HERE/.venv/bin/python" -c 'import ssl, certifi; print("" if ssl.create_default_context().cert_store_stats()["x509_ca"] else certifi.where())' 2>/dev/null || true)
                [ -z "$certificates" ] || export OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE="$certificates"
            fi
            exec "$HERE/.venv/bin/python" -m uvicorn guardrail.app:app --host 127.0.0.1 --port "$PORT"
        ) > "$CHATBOT_LOG_DIR/guardrail.log" 2>&1 &
        GUARDRAIL_PID=$!
        trap 'kill "$GUARDRAIL_PID" 2>/dev/null || true' EXIT
        waited=0
        until answers; do
            if ! kill -0 "$GUARDRAIL_PID" 2>/dev/null; then
                echo "The end of $CHATBOT_LOG_DIR/guardrail.log:" >&2
                tail -n 15 "$CHATBOT_LOG_DIR/guardrail.log" | sed 's/^/    /' >&2
                stop "the guardrail stopped while starting; the lines above say why."
            fi
            [ "$waited" -lt 120 ] || stop "the guardrail did not answer within a minute. See $CHATBOT_LOG_DIR/guardrail.log."
            sleep 0.5
            waited=$((waited + 1))
        done
        echo "The guardrail is running at $GUARDRAIL."
    elif [ -n "$PORT" ]; then
        echo "No guardrail answers at $GUARDRAIL, and the guardrail is not installed in .venv here to start one."
        echo "Start it with Docker (README.md); until then the chatbot says it cannot reach it."
    else
        echo "No guardrail answers at $GUARDRAIL. Until it does, the chatbot says it cannot reach it."
    fi
fi

# --- start ------------------------------------------------------------------------------------------
cd "$HERE"
echo "Settings from $SETTINGS:${NAMES:- none set}"
echo "The screen names every provider a test email can reach, before anything is sent."
echo "Results are logged to $CHATBOT_LOG_DIR/chatbot-runs.csv"
[ -z "$LANGFUSE_AUTH" ] || echo "Every run is also sent, in full, to the testers' Langfuse project at $LANGFUSE_AT."
echo "Starting on http://localhost:8502, for this machine only. Stop it with Ctrl+C."
# The address is given here so that nothing in the environment can widen it.
if [ -n "$GUARDRAIL_PID" ]; then
    .venv/bin/streamlit run app.py --server.address localhost    # not exec: the guardrail is stopped after it
else
    exec .venv/bin/streamlit run app.py --server.address localhost
fi
