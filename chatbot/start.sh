#!/usr/bin/env bash
# Starts the guardrail test chatbot on this machine, with its settings read from one private file.
#
#   ./start.sh
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
# The app listens on localhost only.
# No value from the settings file, and nothing you type at the prompt, is ever printed.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd -P)
SETTINGS="$HOME/.guardrail-chatbot.env"
# Every setting the app reads. A name in the file that is not one of these is a typing mistake.
KNOWN="CHATBOT_MODEL GOOGLE_API_KEY OPENAI_API_KEY GROQ_API_KEY OLLAMA_BASE_URL CHATBOT_LOG_DIR GUARDRAIL_URL
GUARDRAIL_TIMEOUT_MS CHATBOT_CALL_MODEL_ON_REVIEW CHATBOT_MODEL_TIMEOUT_SECONDS CHATBOT_GEMINI_MODEL
CHATBOT_OPENAI_MODEL CHATBOT_GROQ_MODEL CHATBOT_OLLAMA_MODEL CHATBOT_PASSWORD"
stop() { echo "start.sh: $1" >&2; exit 1; }

if [ ! -x "$HERE/.venv/bin/streamlit" ]; then
    # A .venv made with an older Python cannot install the app, and pip's own error for that
    # ("No matching distribution found") does not say why.
    if [ -x "$HERE/.venv/bin/python" ] && ! "$HERE/.venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        stop "the .venv folder here was made with $("$HERE/.venv/bin/python" --version 2>&1), and the app needs Python 3.11. Delete the .venv folder, then in $HERE run:  python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    fi
    stop "the app is not installed yet. In $HERE run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

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

# --- settings come from that file and nowhere else ------------------------------------------------
for name in $(env | sed -n 's/^\(CHATBOT_[A-Za-z0-9_]*\)=.*/\1/p') $KNOWN; do
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

# --- start ------------------------------------------------------------------------------------------
cd "$HERE"
echo "Settings from $SETTINGS:${NAMES:- none set}"
echo "The screen names every provider a test email can reach, before anything is sent."
echo "Results are logged to $CHATBOT_LOG_DIR/chatbot-runs.csv"
echo "Starting on http://localhost:8502, for this machine only. Stop it with Ctrl+C."
# The address is given here so that nothing in the environment can widen it.
exec .venv/bin/streamlit run app.py --server.address localhost
