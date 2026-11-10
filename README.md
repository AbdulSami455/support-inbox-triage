# Support Inbox Triage

A small tool for a four-person support team that shares one inbox. Tickets arrive by
webhook or a form, an AI agent proposes a category, urgency and draft reply, and a
human reviews and sends it.


**Stack:** Python · FastAPI · LangGraph · OpenRouter · SQLite (local) / Cloudflare D1 (prod) ·
Cloudflare Python Workers (free plan) · plain HTML/JS frontend (no build step).

## Run locally (one command)

```bash
cp .env.example .env        # set SESSION_SECRET, WEBHOOK_SECRET, DEMO_PASSWORD, OPENROUTER_API_KEY
docker compose up --build   # http://localhost:8000
```

Load the sample emails through the webhook:

```bash
WEBHOOK_SECRET=<same as .env> ./scripts/send_samples.sh http://localhost:8000
```

Sign in with any seeded user and `DEMO_PASSWORD`:

| User | Role |
|---|---|
| `admin@support.test` | admin |
| `sam@support.test`, `jordan@support.test`, `riley@support.test` | agent |

Tests (no network, the model is faked):

```bash
uv venv && uv pip install -r pyproject.toml pytest
.venv/bin/python -m pytest
```

## What I built

**Core**
- **Sign-in with two roles.** Separate Agent and Admin tabs on the sign-in page; the server rejects an account used on the wrong tab. Signed session cookie (HttpOnly, Secure in prod), PBKDF2 password
  hashes. Only admins can reassign tickets. This is enforced on the server (`403` for agents),
  and the UI also hides the control.
- **Inbound webhook** `POST /api/webhook/email`. It needs the `X-Webhook-Secret` header, which is
  checked before the body is read. It accepts `from`, `subject`, `body` (or `text`) and an
  optional `message_id`. A repeated `message_id` returns the existing ticket instead of creating
  a duplicate, so provider retries are safe.
- **Form** to create tickets in the UI (`POST /api/tickets`).
- **Ticket list** filtered by status (all / open / overdue / replied / closed), 10 per page.
- **One AI call per ticket**, run as a LangGraph graph:
  `retrieve` (keyword search over help articles, free) → `draft` (the single OpenRouter call)
  → `validate` (strict Pydantic schema: no extra keys, exact enum values, non-empty draft, the
  cited article must be one we supplied). If the output does not match, the UI shows the
  validation error instead of a draft.
- **Human-in-the-loop sending.** The draft goes into an editable box, and only clicking
  *Send reply* (with a confirm step) records a reply. Each reply stores who sent it and when, and
  the ticket shows them as an audit trail. Nothing is sent automatically.
- **Public URL** on Cloudflare (see Deploy).

**Stretch goals (all three done)**
- **Help article next to the draft.** The model picks which of the retrieved articles the draft
  is based on (`article_id`). The app checks the id against the articles it actually supplied
  and shows that article beside the reply.
- **Overdue marker.** A ticket is overdue when it is still open, is older than 24 hours, and has no
  sent reply. The server computes this, so the badge, the "overdue" filter and the counter always agree.
- **Live updates.** While the tab is visible, the page checks `GET /api/updates?after_id=<newest id>`
  every 10 seconds. New tickets slide into the list with a toast that can open them. Polling was
  chosen over WebSockets because it's one cheap indexed query, works the same on Cloudflare's free
  plan and locally, and is plenty for a four-person team.

**Cost controls** (the key has a spend cap)
- A valid result is stored and reused. Opening a ticket again never triggers a second call.
- At most 3 AI calls per ticket, counting retries and *Regenerate*. After that the agent writes the reply by hand. A failed regenerate keeps the previous draft.
- A short lock stops two agents opening the same ticket from paying twice.
- Email text is cut to 4,000 characters and output is capped at 1,500 tokens. The model is `nvidia/nemotron-3.5-super-vl:free` ($0 on OpenRouter). It's a free model that supports both JSON mode (`response_format`) and reasoning. Reasoning runs at low effort and is excluded from the reply, and the strict validator still checks every response.
- Article retrieval is keyword-based, so it costs nothing (no embeddings).

**Safety**
- The prompt treats the email as untrusted data and forbids promising refunds or dates, and asking
  for passwords. `samples/emails/06-prompt-injection.json` is there to demo this.
- All email text is rendered with `textContent`, never as HTML.

## API

| Method | Path | Who |
|---|---|---|
| POST | `/api/login`, `/api/logout` | anyone |
| GET | `/api/me`, `/api/users` | signed in |
| GET | `/api/tickets?status=open&page=1` (also `overdue`) | signed in |
| GET | `/api/updates?after_id=N` (live updates) | signed in |
| POST | `/api/tickets` | signed in |
| GET | `/api/tickets/{id}` | signed in |
| POST | `/api/tickets/{id}/triage` (`?regenerate=true` for a new draft) | signed in |
| POST | `/api/tickets/{id}/replies` | signed in |
| PATCH | `/api/tickets/{id}/status` | signed in |
| PATCH | `/api/tickets/{id}/assign` | **admin** |
| POST | `/api/webhook/email` | `X-Webhook-Secret` |

## Deploy (Cloudflare, free plan)

The brief asked for AWS. This is deployed on Cloudflare instead, as requested.

```
Browser ──> Workers Assets (src/app/static/index.html)
        └─> /api/*  ──> Python Worker (src/worker.py → same FastAPI app) ──> D1 binding
                                                                      └──> OpenRouter
```

The same FastAPI app runs in both places. Locally it uses SQLite under uvicorn. On Cloudflare
it runs as a Python Worker, with the D1 binding for storage and Worker secrets for config.

```bash
npm install                                   # wrangler
npx wrangler login
npx wrangler secret bulk secrets.json         # {"SESSION_SECRET": ..., "WEBHOOK_SECRET": ..., "DEMO_PASSWORD": ...}
npx wrangler secret put OPENROUTER_API_KEY
uvx --from workers-py pywrangler deploy       # needs uv >= 0.13 (uv self update)
```

Set `database_id` in `wrangler.jsonc` to your own D1 database. Use `secret bulk` with JSON
rather than `deploy --secrets-file`: Wrangler loads the local `.env` into its environment, and
those values override the ones in a dotenv secrets file.

## What I left out

- **Real outbound email.** "Send" records the reply and marks the ticket replied, but no email
  provider is wired up. In production this would call SES/Postmark/Resend from the send endpoint.
- **User management, password reset, login rate limiting.** There are four seeded users with a shared demo password.
- **Push-based live updates** (WebSockets / Server-Sent Events). The UI polls every 10s instead; see Stretch goals.
- **Webhook payload signatures** (HMAC of the body). A shared-secret header is used instead.
- **Embeddings/RAG** for article search. Keyword matching is enough for 8 articles and costs nothing.

## Known problem and how I'd fix it

**The AI call runs synchronously when an agent first opens a ticket.** The agent waits a few
seconds for the draft. Until someone opens a ticket, the list has no category or urgency to sort by.

**Fix:** triage on arrival in the background. The webhook would put the ticket id on a queue
(Cloudflare Queues, which Python Workers can consume, or SQS on AWS), and a consumer would run
the same LangGraph graph and store the result. The existing attempt counter and lock already make
this safe to retry. The UI would then show urgency in the list immediately, and the inbox could
sort by it.

Smaller one: Cloudflare's Python runtime has no OpenSSL, so PBKDF2 falls back to a pure-Python
loop, and production uses 10k rounds (200k locally) to fit the free plan's CPU budget. The fix is
to hash with WebCrypto (`crypto.subtle`) through the JS bridge, which is native-speed.

## Project layout

```
src/app/
  main.py        FastAPI routes, ticket logic, triage endpoint
  agent.py       LangGraph graph, strict output schema, OpenRouter call
  kb.py          help articles + keyword retriever
  auth.py        password hashing, sessions, role checks, seeding
  db.py          SQLite / D1 storage (same SQL)
  config.py      settings from env (os.environ or Worker env)
  static/index.html  UI
src/worker.py        Cloudflare Python Worker entry
samples/emails/      sample inbound emails
scripts/send_samples.sh
tests/               API + agent tests (fake model)
```
