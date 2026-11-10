import asyncio
import hmac
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import kb
from .agent import build_graph, make_llm
from .auth import check_password, current_user, get_db, require_admin
from .config import Settings, load_settings
from .db import Database, SQLiteDB

PAGE_SIZE = 10
MAX_TRIAGE_ATTEMPTS = 3  # per ticket; protects the API spend cap
STATUSES = ("open", "replied", "closed")
OVERDUE_AFTER = 24 * 3600  # seconds without a reply before a ticket is flagged

# Open, older than 24h and nobody has replied yet. Takes one parameter: the cutoff time.
OVERDUE_SQL = """(t.status = 'open' AND t.created_at < ?
  AND NOT EXISTS (SELECT 1 FROM replies r WHERE r.ticket_id = t.id))"""


def create_app(settings: Settings | None = None, db: Database | None = None, llm=None) -> FastAPI:
    """db/llm are injected on Cloudflare (D1 binding) and in tests (fakes)."""
    settings = settings or load_settings()
    settings.validate()

    app = FastAPI(title="Support Inbox Triage")
    app.state.settings = settings
    app.state.db = db or SQLiteDB(settings.db_path)
    app.state.ready = False  # schema + seed run lazily on the first request
    app.state.init_lock = asyncio.Lock()
    app.state.llm = llm  # real OpenRouter client is created lazily when a key is set
    app.state.graph = None
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        max_age=12 * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    register_routes(app)
    return app


# ---------- request bodies ----------


class LoginIn(BaseModel):
    email: str
    password: str
    role: Literal["agent", "admin"] | None = None  # which sign-in tab was used


class TicketIn(BaseModel):
    from_: str = Field(alias="from")
    subject: str = Field(min_length=1, max_length=200)
    body: str = ""
    text: str = ""  # alias many inbound-email webhooks use
    message_id: str | None = None


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class StatusIn(BaseModel):
    status: Literal["open", "replied", "closed"]


class AssignIn(BaseModel):
    assignee_id: int | None


# ---------- helpers ----------


async def insert_ticket(db: Database, data: TicketIn, source: str) -> tuple[int, bool]:
    """Store a ticket. A repeated message_id (webhook retry) returns the existing id."""
    name, addr = parseaddr(data.from_)
    if "@" not in addr:
        raise HTTPException(422, "from must be a valid email address")
    body = (data.body or data.text).strip()
    if not body or len(body) > 20000:
        raise HTTPException(422, "body is required (max 20000 chars)")
    msg_id = (data.message_id or "").strip() or None
    rows = await db.query(
        """INSERT INTO tickets (message_id, from_name, from_email, subject, body, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(message_id) DO NOTHING RETURNING id""",
        (msg_id, name, addr.lower(), data.subject.strip(), body, source, int(time.time())),
    )
    if rows:
        return rows[0]["id"], False
    return (await db.query("SELECT id FROM tickets WHERE message_id = ?", (msg_id,)))[0]["id"], True


def triage_view(t: dict) -> dict | None:
    if not t.get("draft_reply"):
        return None
    return {
        "category": t["category"],
        "urgency": t["urgency"],
        "draft_reply": t["draft_reply"],
        "article": kb.get_article(t["article_id"]),
    }


async def ticket_or_404(db: Database, ticket_id: int) -> dict:
    rows = await db.query(
        """SELECT t.*, u.name AS assignee_name FROM tickets t
           LEFT JOIN users u ON u.id = t.assignee_id WHERE t.id = ?""",
        (ticket_id,),
    )
    if not rows:
        raise HTTPException(404, "Ticket not found")
    return rows[0]


# ---------- routes ----------


def register_routes(app: FastAPI) -> None:
    index = Path(__file__).parent / "static" / "index.html"

    @app.get("/", include_in_schema=False)
    async def home():
        # Locally FastAPI serves the UI; on Cloudflare it is a static asset.
        return FileResponse(index)

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    # --- auth ---
    @app.post("/api/login")
    async def login(data: LoginIn, request: Request, db: Database = Depends(get_db)):
        rows = await db.query("SELECT * FROM users WHERE email = ?", (data.email.strip().lower(),))
        if not rows or not check_password(rows[0]["password_hash"], data.password):
            raise HTTPException(401, "Wrong email or password")
        if data.role and rows[0]["role"] != data.role:
            other = "Admin" if rows[0]["role"] == "admin" else "Agent"
            raise HTTPException(403, f"This is an {rows[0]['role']} account. Use the {other} sign-in.")
        request.session["user_id"] = rows[0]["id"]
        return {k: rows[0][k] for k in ("id", "email", "name", "role")}

    @app.post("/api/logout", status_code=204)
    async def logout(request: Request):
        request.session.clear()

    @app.get("/api/me")
    async def me(user: dict = Depends(current_user)):
        return user

    @app.get("/api/users")
    async def users(_: dict = Depends(current_user), db: Database = Depends(get_db)):
        return await db.query("SELECT id, name, role FROM users ORDER BY id")

    # --- tickets ---
    @app.get("/api/tickets")
    async def list_tickets(
        status: str = "all", page: int = 1, _: dict = Depends(current_user), db: Database = Depends(get_db)
    ):
        if status not in ("all", "overdue", *STATUSES):
            raise HTTPException(422, "Unknown status filter")
        page = max(1, page)
        cutoff = int(time.time()) - OVERDUE_AFTER
        if status == "all":
            where, args = "", ()
        elif status == "overdue":
            where, args = f"WHERE {OVERDUE_SQL}", (cutoff,)
        else:
            where, args = "WHERE t.status = ?", (status,)
        total = (await db.query(f"SELECT COUNT(*) AS n FROM tickets t {where}", args))[0]["n"]
        overdue_total = (await db.query(f"SELECT COUNT(*) AS n FROM tickets t WHERE {OVERDUE_SQL}", (cutoff,)))[0]["n"]
        items = await db.query(
            f"""SELECT t.id, t.from_name, t.from_email, t.subject, t.status, t.source, t.category,
                       t.urgency, t.created_at, t.assignee_id, u.name AS assignee_name,
                       CASE WHEN {OVERDUE_SQL} THEN 1 ELSE 0 END AS overdue
                FROM tickets t LEFT JOIN users u ON u.id = t.assignee_id {where}
                ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?""",
            (cutoff, *args, PAGE_SIZE, (page - 1) * PAGE_SIZE),
        )
        for item in items:
            item["overdue"] = bool(item["overdue"])
        return {"items": items, "page": page, "page_size": PAGE_SIZE, "total": total, "overdue_total": overdue_total}

    @app.get("/api/updates")
    async def updates(after_id: int = 0, _: dict = Depends(current_user), db: Database = Depends(get_db)):
        """Cheap live-update check the UI polls: newest ticket id plus anything newer than after_id."""
        latest = (await db.query("SELECT COALESCE(MAX(id), 0) AS id FROM tickets"))[0]["id"]
        new = []
        if after_id and latest > after_id:
            new = await db.query(
                "SELECT id, subject, from_name, from_email, source FROM tickets WHERE id > ? ORDER BY id DESC LIMIT 5",
                (after_id,),
            )
        return {"latest_id": latest, "new": new}

    @app.post("/api/tickets", status_code=201)
    async def create_ticket(data: TicketIn, _: dict = Depends(current_user), db: Database = Depends(get_db)):
        data.message_id = None  # form tickets are never de-duplicated
        ticket_id, _dup = await insert_ticket(db, data, "form")
        return {"id": ticket_id}

    @app.get("/api/tickets/{ticket_id}")
    async def get_ticket(ticket_id: int, _: dict = Depends(current_user), db: Database = Depends(get_db)):
        t = await ticket_or_404(db, ticket_id)
        replies = await db.query(
            """SELECT r.id, r.body, r.sent_at, u.name AS sent_by FROM replies r
               JOIN users u ON u.id = r.sent_by WHERE r.ticket_id = ? ORDER BY r.sent_at, r.id""",
            (ticket_id,),
        )
        keep = ("id", "from_name", "from_email", "subject", "body", "status", "source", "created_at",
                "assignee_id", "assignee_name", "triage_error")
        return {
            **{k: t[k] for k in keep},
            "overdue": t["status"] == "open" and not replies and t["created_at"] < time.time() - OVERDUE_AFTER,
            "triage": triage_view(t),
            "triage_attempts_left": max(0, MAX_TRIAGE_ATTEMPTS - t["triage_attempts"]),
            "replies": replies,
        }

    @app.post("/api/tickets/{ticket_id}/triage")
    async def triage(
        ticket_id: int,
        request: Request,
        regenerate: bool = False,
        _: dict = Depends(current_user),
        db: Database = Depends(get_db),
    ):
        """Runs the LangGraph agent. A valid result is cached and reused unless the agent asks to
        regenerate; every call counts toward MAX_TRIAGE_ATTEMPTS to protect the spend cap."""
        t = await ticket_or_404(db, ticket_id)
        cached = triage_view(t)
        if cached and not regenerate:
            return {"triage": cached, "cached": True, "attempts_left": max(0, MAX_TRIAGE_ATTEMPTS - t["triage_attempts"])}
        state = request.app.state
        if state.llm is None:
            if not state.settings.openrouter_api_key:
                raise HTTPException(503, "AI is not configured (OPENROUTER_API_KEY is empty)")
            state.llm = make_llm(state.settings)
        if t["triage_attempts"] >= MAX_TRIAGE_ATTEMPTS:
            raise HTTPException(429, "AI limit reached for this ticket")

        # Claim the call so two agents opening the same ticket don't pay twice.
        now = int(time.time())
        claimed = await db.execute(
            """UPDATE tickets SET triage_attempts = triage_attempts + 1, triage_lock_until = ?
               WHERE id = ? AND (draft_reply IS NULL OR ?) AND triage_attempts < ? AND triage_lock_until < ?""",
            (now + 45, ticket_id, int(regenerate), MAX_TRIAGE_ATTEMPTS, now),
        )
        if not claimed:
            raise HTTPException(409, "Triage already in progress, try again in a moment")

        state.graph = state.graph or build_graph(state.llm)
        out = await state.graph.ainvoke({"from_name": t["from_name"], "subject": t["subject"], "body": t["body"]})
        if err := out.get("error"):
            # On a failed regenerate the previous valid draft stays in place.
            await db.execute("UPDATE tickets SET triage_error = ?, triage_lock_until = 0 WHERE id = ?", (err, ticket_id))
            raise HTTPException(502, err)
        r = out["result"]
        await db.execute(
            """UPDATE tickets SET category = ?, urgency = ?, draft_reply = ?, article_id = ?,
                      triage_error = NULL, triage_lock_until = 0 WHERE id = ?""",
            (r["category"], r["urgency"], r["draft_reply"], r["article_id"], ticket_id),
        )
        return {"triage": triage_view(r), "cached": False, "attempts_left": max(0, MAX_TRIAGE_ATTEMPTS - t["triage_attempts"] - 1)}

    @app.post("/api/tickets/{ticket_id}/replies", status_code=201)
    async def send_reply(
        ticket_id: int, data: ReplyIn, user: dict = Depends(current_user), db: Database = Depends(get_db)
    ):
        """The only way a reply goes out: an agent explicitly submits the reviewed text."""
        await ticket_or_404(db, ticket_id)
        now = int(time.time())
        await db.execute(
            "INSERT INTO replies (ticket_id, body, sent_by, sent_at) VALUES (?, ?, ?, ?)",
            (ticket_id, data.body.strip(), user["id"], now),
        )
        await db.execute("UPDATE tickets SET status = 'replied' WHERE id = ? AND status = 'open'", (ticket_id,))
        return {"body": data.body.strip(), "sent_by": user["name"], "sent_at": now}

    @app.patch("/api/tickets/{ticket_id}/status", status_code=204)
    async def set_status(
        ticket_id: int, data: StatusIn, _: dict = Depends(current_user), db: Database = Depends(get_db)
    ):
        if not await db.execute("UPDATE tickets SET status = ? WHERE id = ?", (data.status, ticket_id)):
            raise HTTPException(404, "Ticket not found")

    @app.patch("/api/tickets/{ticket_id}/assign", status_code=204)
    async def assign(ticket_id: int, data: AssignIn, _: dict = Depends(require_admin), db: Database = Depends(get_db)):
        if data.assignee_id is not None and not await db.query("SELECT 1 FROM users WHERE id = ?", (data.assignee_id,)):
            raise HTTPException(422, "Unknown assignee")
        if not await db.execute("UPDATE tickets SET assignee_id = ? WHERE id = ?", (data.assignee_id, ticket_id)):
            raise HTTPException(404, "Ticket not found")

    # --- inbound email webhook ---
    async def check_webhook_secret(request: Request, x_webhook_secret: str = Header(default="")):
        """Runs before the body is parsed, so unauthenticated callers learn nothing."""
        expected = request.app.state.settings.webhook_secret
        if not hmac.compare_digest(x_webhook_secret.encode(), expected.encode()):
            raise HTTPException(401, "Bad webhook secret")

    @app.post("/api/webhook/email", dependencies=[Depends(check_webhook_secret)])
    async def webhook(data: TicketIn, response: Response, db: Database = Depends(get_db)):
        ticket_id, duplicate = await insert_ticket(db, data, "webhook")
        response.status_code = 200 if duplicate else 201
        return {"id": ticket_id, "duplicate": duplicate}
