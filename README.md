# IndoLog guardrail, with its test chatbot

The guardrail sits before and after a language model. Before, it checks the email, the
attachment text and the question that are about to be sent, and stops an instruction aimed at the
model that is hidden in them. After, it checks what the model returned before anyone sees it or
anything is stored. It never calls a model itself.

This repository holds the guardrail and a small chatbot for testing it on your own machine.

| Folder | What it is |
|---|---|
| [`guardrail/`](guardrail/README.md) | The guardrail service. It runs in Docker, with its own database. |
| [`chatbot/`](chatbot/README.md) | The test chatbot. You paste a test email, with or without a question, and see what the guardrail does and what would happen next. |

## Testing it

**[chatbot/README.md](chatbot/README.md) is the guide.** It goes from nothing to a first test:
what to install, how to start the guardrail and the chatbot, four tests to check that it works,
what the results mean, and what to do when something goes wrong.

> **Use fake or anonymised emails only.** When an email passes the guardrail, the whole email is
> sent to the model's provider. Never paste a real customer's email.

It runs on a Mac, and on Windows inside WSL (the Linux terminal that Windows provides). The guide
has the one-time setup for each. On Windows, every command below is typed in the Ubuntu terminal.

**Without Docker**, the chatbot's start script installs the guardrail beside it and starts it for
you, with the same policy and the same results: on a Mac `./start.sh`, and on Windows `start.bat`,
with no WSL. The guide's [Without Docker](chatbot/README.md#without-docker) section has the steps.

### The first time

For someone who has Docker Desktop running, git and [uv](https://docs.astral.sh/uv/):

```bash
cd ~
git clone https://github.com/abhipabhi/indolog-guardrail.git
cd ~/indolog-guardrail/guardrail
docker compose up --build -d
```

Wait until `docker compose ps` shows the `guardrail` line as `(healthy)`. The first build takes
several minutes. Then:

```bash
cd ~/indolog-guardrail/chatbot
./start.sh
```

and open http://localhost:8502. The first time, `start.sh` installs the app with uv (a minute or
two; uv brings the Python it needs), then asks for your own Gemini API key; the guide says where
to get one.

### Every time after that

After you have stopped it, or shut down or restarted the computer: start Docker Desktop, wait
until it is running, then

```bash
cd ~/indolog-guardrail/guardrail
docker compose up -d
```

and, once `docker compose ps` shows the `guardrail` line as `(healthy)` (up to a minute):

```bash
cd ~/indolog-guardrail/chatbot
./start.sh
```

Then open http://localhost:8502. Nothing is installed or built again, and the key is not asked
for again.

## What is not here

Only what is needed to run the two is in this repository. The guardrail's tests, its evaluation
data and its design documents are kept elsewhere.

## Licence

[`chatbot/LICENSE`](chatbot/LICENSE) covers the chatbot's own files. It does not cover the
`guardrail/` folder, or the three files in `chatbot/app/guardrail/` that are copies of the IndoLog
platform's code for calling the guardrail; each of those says so at its top.
