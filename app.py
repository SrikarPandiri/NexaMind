"""
NexaMind - AI Assistance Web Application
==========================================
A Flask-based AI chat application with conversation history, quick AI tools,
and a modern chat interface.

Run with:  python app.py
"""

import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nexamind-dev-secret-change-me")
app.json.ensure_ascii = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database", "database.db")

# AI provider configuration (read from environment, never hard-coded)
AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower()   # "gemini" or "openrouter"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

AI_REQUEST_TIMEOUT = 30  # seconds

# ---------------------------------------------------------------------------
# Quick AI Tools - system prompt presets
# ---------------------------------------------------------------------------

TOOL_PROMPTS = {
    "explain": "You are NexaMind, a friendly AI assistant. Explain the user's topic clearly, "
               "using simple language, short paragraphs, and an example where useful.",
    "summarize": "You are NexaMind, a friendly AI assistant. Summarize the user's text into "
                 "concise, well-organized key points.",
    "rewrite": "You are NexaMind, a friendly AI assistant. Rewrite the user's text to improve "
               "clarity, tone, and flow while preserving the original meaning.",
    "ideas": "You are NexaMind, a friendly AI assistant. Brainstorm creative, practical ideas "
             "related to the user's request. Present them as a short numbered list.",
    "code": "You are NexaMind, a coding assistant. Generate clean, well-commented, working code "
            "for the user's request. Use fenced code blocks with the correct language tag.",
    "debug": "You are NexaMind, a coding assistant. Find and fix bugs in the user's code. "
             "Explain the root cause briefly, then show the corrected code in a fenced code block.",
    "study": "You are NexaMind, a study assistant. Help the user understand and learn the topic "
             "with clear explanations, key takeaways, and (if useful) a short quiz at the end.",
}

DEFAULT_SYSTEM_PROMPT = (
    "You are NexaMind, an original AI assistant with the tagline 'Think smarter. Ask anything. "
    "Get more done.' Be helpful, clear, and honest. If you are not sure about something, say so "
    "plainly instead of guessing. Never claim to have taken an action (like browsing, saving a "
    "file, or sending an email) that you did not actually perform. Keep answers concise unless "
    "the user asks for more detail. Use markdown formatting and fenced code blocks for code."
)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Return a request-scoped SQLite connection."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they do not already exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_guest INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT 'New conversation',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user_id():
    return session.get("user_id")


def login_required(view):
    def wrapped(*args, **kwargs):
        if not current_user_id():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated."}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


# ---------------------------------------------------------------------------
# AI integration
# ---------------------------------------------------------------------------

class AIError(Exception):
    """Raised when the AI backend cannot produce a response."""


def build_system_prompt(tool):
    if tool and tool in TOOL_PROMPTS:
        return TOOL_PROMPTS[tool]
    return DEFAULT_SYSTEM_PROMPT


def call_gemini(system_prompt, history):
    if not GEMINI_API_KEY:
        raise AIError("missing_key")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    contents = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
    }

    try:
        resp = requests.post(url, json=payload, timeout=AI_REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise AIError("timeout")
    except requests.exceptions.RequestException:
        raise AIError("network")

    if resp.status_code == 429:
        raise AIError("rate_limit")
    if resp.status_code == 401 or resp.status_code == 403:
        raise AIError("missing_key")
    if resp.status_code >= 500:
        raise AIError("service_down")
    if resp.status_code != 200:
        app.logger.warning("Gemini request failed with status %s", resp.status_code)
        raise AIError("api_error")

    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise AIError("api_error")


def call_openrouter(system_prompt, history):
    if not OPENROUTER_API_KEY:
        raise AIError("missing_key")

    url = "https://openrouter.ai/api/v1/chat/completions"
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": OPENROUTER_MODEL, "messages": messages, "temperature": 0.7}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=AI_REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise AIError("timeout")
    except requests.exceptions.RequestException:
        raise AIError("network")

    if resp.status_code == 429:
        raise AIError("rate_limit")
    if resp.status_code in (401, 403):
        raise AIError("missing_key")
    if resp.status_code >= 500:
        raise AIError("service_down")
    if resp.status_code != 200:
        app.logger.warning("OpenRouter request failed with status %s", resp.status_code)
        raise AIError("api_error")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise AIError("api_error")


def stream_gemini(system_prompt, history):
    """Yield text chunks from Gemini's streaming endpoint as they arrive."""
    if not GEMINI_API_KEY:
        raise AIError("missing_key")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:streamGenerateContent?alt=sse&key={GEMINI_API_KEY}"
    )
    contents = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
    }

    try:
        resp = requests.post(url, json=payload, timeout=AI_REQUEST_TIMEOUT, stream=True)
    except requests.exceptions.Timeout:
        raise AIError("timeout")
    except requests.exceptions.RequestException:
        raise AIError("network")

    if resp.status_code == 429:
        raise AIError("rate_limit")
    if resp.status_code in (401, 403):
        raise AIError("missing_key")
    if resp.status_code >= 500:
        raise AIError("service_down")
    if resp.status_code != 200:
        app.logger.warning("Gemini streaming request failed with status %s", resp.status_code)
        raise AIError("api_error")

    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            text = obj["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            continue
        if text:
            yield text


def stream_openrouter(system_prompt, history):
    """Yield text chunks from OpenRouter's streaming (SSE) endpoint as they arrive."""
    if not OPENROUTER_API_KEY:
        raise AIError("missing_key")

    url = "https://openrouter.ai/api/v1/chat/completions"
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": OPENROUTER_MODEL, "messages": messages, "temperature": 0.7, "stream": True}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=AI_REQUEST_TIMEOUT, stream=True)
    except requests.exceptions.Timeout:
        raise AIError("timeout")
    except requests.exceptions.RequestException:
        raise AIError("network")

    if resp.status_code == 429:
        raise AIError("rate_limit")
    if resp.status_code in (401, 403):
        raise AIError("missing_key")
    if resp.status_code >= 500:
        raise AIError("service_down")
    if resp.status_code != 200:
        app.logger.warning("OpenRouter streaming request failed with status %s", resp.status_code)
        raise AIError("api_error")

    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if raw == "[DONE]":
            break
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            delta = obj["choices"][0]["delta"].get("content")
        except (KeyError, IndexError):
            continue
        if delta:
            yield delta


def stream_demo(tool, latest_message):
    """Simulate token-by-token streaming for demo mode (no API key configured)."""
    text = demo_response(tool, latest_message)
    for word in text.split(" "):
        yield word + " "
        time.sleep(0.02)


def demo_response(tool, latest_message):
    """
    Friendly, clearly-labeled fallback used when no AI API key is configured,
    so the app remains demonstrable out of the box. Never pretends to be a
    live model response.
    """
    label = TOOL_PROMPTS.get(tool, "").split(".")[0] if tool else ""
    intro = "**NexaMind demo mode** — no AI API key is configured yet, so here's a placeholder reply.\n\n"
    if tool == "code":
        body = (
            "```python\n"
            "def greet(name):\n"
            "    \"\"\"Return a friendly greeting.\"\"\"\n"
            "    return f\"Hello, {name}! This is a demo response.\"\n"
            "```\n\nOnce you add `GEMINI_API_KEY` or `OPENROUTER_API_KEY` to your `.env` file, "
            "NexaMind will generate real code for requests like yours."
        )
    else:
        body = (
            f"You asked: _{latest_message.strip()[:200]}_\n\n"
            "To get real, intelligent answers, add a valid API key to your `.env` file "
            "(`GEMINI_API_KEY` or `OPENROUTER_API_KEY`) and restart the server. "
            "Everything else — conversations, history, quick tools — is already fully working."
        )
    return intro + body


def get_ai_response(tool, history):
    """
    history: list of {"role": "user"|"assistant", "content": str}, oldest first.
    Returns (text, used_demo_mode: bool, error_message: str|None)
    """
    system_prompt = build_system_prompt(tool)
    provider = call_gemini if AI_PROVIDER == "gemini" else call_openrouter

    try:
        text = provider(system_prompt, history)
        return text, False, None
    except AIError as e:
        if str(e) == "missing_key":
            latest = history[-1]["content"] if history else ""
            return demo_response(tool, latest), True, None
        error_messages = {
            "timeout": "The AI service took too long to respond. Please try again.",
            "network": "We couldn't reach the AI service. Check your internet connection and try again.",
            "service_down": "The AI service is temporarily unavailable. Please try again shortly.",
            "rate_limit": "The AI service is temporarily rate-limited. Please try again in a moment.",
            "api_error": "The AI service returned an unexpected error. Please try again.",
        }
        return None, False, error_messages.get(str(e), "Something went wrong. Please try again.")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repair_mojibake(text):
    """Repair common UTF-8-as-Latin-1 corruption without changing valid text."""
    markers = "ÃÂâð�"
    if not isinstance(text, str) or not any(marker in text for marker in markers):
        return text
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired if sum(text.count(marker) for marker in markers) > sum(
        repaired.count(marker) for marker in markers
    ) else text


def make_title(text):
    """Derive a short, meaningful conversation title from the first message."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "New conversation"
    if len(text) <= 42:
        return text
    return text[:39].rsplit(" ", 1)[0] + "..."


def row_to_dict(row):
    if not row:
        return None
    result = dict(row)
    if "content" in result:
        result["content"] = repair_mojibake(result["content"])
    return result


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("username"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if current_user_id():
            return redirect(url_for("index"))
        return render_template("login.html")

    action = request.form.get("action", "login")
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    db = get_db()

    if action == "guest":
        guest_name = f"guest_{uuid.uuid4().hex[:8]}"
        db.execute(
            "INSERT INTO users (username, password_hash, is_guest, created_at) VALUES (?, ?, 1, ?)",
            (guest_name, generate_password_hash(uuid.uuid4().hex), now_iso()),
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE username = ?", (guest_name,)).fetchone()
        session["user_id"] = user["id"]
        session["username"] = "Guest"
        return redirect(url_for("index"))

    if not username or not password:
        return render_template("login.html", error="Please enter a username and password.")

    if action == "register":
        existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return render_template("login.html", error="That username is already taken.")
        db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), now_iso()),
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        session["user_id"] = user["id"]
        session["username"] = username
        return redirect(url_for("index"))

    # action == "login"
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Incorrect username or password.")
    session["user_id"] = user["id"]
    session["username"] = username
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# API routes - conversations
# ---------------------------------------------------------------------------

@app.route("/api/conversations", methods=["GET"])
@login_required
def list_conversations():
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        rows = db.execute(
            """
            SELECT DISTINCT c.* FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.user_id = ? AND (c.title LIKE ? OR m.content LIKE ?)
            ORDER BY c.updated_at DESC
            """,
            (current_user_id(), f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
            (current_user_id(),),
        ).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/conversations/<conversation_id>/messages", methods=["GET"])
@login_required
def get_messages(conversation_id):
    db = get_db()
    convo = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, current_user_id()),
    ).fetchone()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404
    rows = db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    ).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
@login_required
def delete_conversation(conversation_id):
    db = get_db()
    convo = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, current_user_id()),
    ).fetchone()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404
    db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
    db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    db.commit()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# API routes - chat
# ---------------------------------------------------------------------------

@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    conversation_id = data.get("conversation_id")
    tool = data.get("tool")

    if not message:
        return jsonify({"error": "Message cannot be empty."}), 400
    if len(message) > 8000:
        return jsonify({"error": "Message is too long (max 8000 characters)."}), 400

    db = get_db()

    try:
        if conversation_id:
            convo = db.execute(
                "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, current_user_id()),
            ).fetchone()
            if not convo:
                return jsonify({"error": "Conversation not found."}), 404
        else:
            conversation_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, current_user_id(), make_title(message), now_iso(), now_iso()),
            )
            db.commit()

        db.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (conversation_id, message, now_iso()),
        )
        db.commit()

        history_rows = db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

        reply, used_demo, error = get_ai_response(tool, history)

        if error:
            return jsonify({"error": error, "conversation_id": conversation_id}), 502
        reply = repair_mojibake(reply)

        db.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (conversation_id, reply, now_iso()),
        )
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id)
        )
        db.commit()

        convo = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()

        return jsonify(
            {
                "reply": reply,
                "conversation_id": conversation_id,
                "title": convo["title"],
                "demo_mode": used_demo,
            }
        )

    except sqlite3.Error:
        return jsonify({"error": "A database error occurred. Please try again."}), 500


def sse_pack(event, data):
    """Format a Server-Sent Events message."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.route("/api/chat/stream", methods=["POST"])
@login_required
def chat_stream():
    """
    Streaming counterpart to /api/chat. Sends the AI reply to the browser as
    it's generated (Server-Sent Events) instead of waiting for the full reply.
    """
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    conversation_id = data.get("conversation_id")
    tool = data.get("tool")

    if not message:
        return jsonify({"error": "Message cannot be empty."}), 400
    if len(message) > 8000:
        return jsonify({"error": "Message is too long (max 8000 characters)."}), 400

    db = get_db()
    try:
        if conversation_id:
            convo = db.execute(
                "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, current_user_id()),
            ).fetchone()
            if not convo:
                return jsonify({"error": "Conversation not found."}), 404
        else:
            conversation_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, current_user_id(), make_title(message), now_iso(), now_iso()),
            )
            db.commit()

        db.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (conversation_id, message, now_iso()),
        )
        db.commit()

        history_rows = db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

        convo = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        title = convo["title"]
    except sqlite3.Error:
        return jsonify({"error": "A database error occurred. Please try again."}), 500

    system_prompt = build_system_prompt(tool)
    error_messages = {
        "timeout": "The AI service took too long to respond. Please try again.",
        "network": "We couldn't reach the AI service. Check your internet connection and try again.",
        "service_down": "The AI service is temporarily unavailable. Please try again shortly.",
        "rate_limit": "The AI service is temporarily rate-limited. Please try again in a moment.",
        "api_error": "The AI service returned an unexpected error. Please try again.",
    }

    def generate():
        chunks = []
        used_demo = False

        try:
            yield sse_pack("meta", {"conversation_id": conversation_id, "title": title})

            try:
                provider_gen = stream_gemini(system_prompt, history) if AI_PROVIDER == "gemini" \
                    else stream_openrouter(system_prompt, history)
                for chunk in provider_gen:
                    chunk = repair_mojibake(chunk)
                    chunks.append(chunk)
                    yield sse_pack("chunk", {"text": chunk})

            except AIError as e:
                if str(e) == "missing_key":
                    used_demo = True
                    for chunk in stream_demo(tool, message):
                        chunk = repair_mojibake(chunk)
                        chunks.append(chunk)
                        yield sse_pack("chunk", {"text": chunk})
                else:
                    yield sse_pack(
                        "error",
                        {"error": error_messages.get(str(e), "Something went wrong. Please try again.")},
                    )
                    return

            reply = "".join(chunks)
            try:
                db2 = get_db()
                db2.execute(
                    "INSERT INTO messages (conversation_id, role, content, created_at) "
                    "VALUES (?, 'assistant', ?, ?)",
                    (conversation_id, reply, now_iso()),
                )
                db2.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id)
                )
                db2.commit()
            except sqlite3.Error:
                pass  # the reply already reached the browser; don't fail the stream over a save error

            yield sse_pack("done", {"demo_mode": used_demo})

        except GeneratorExit:
            raise
        except Exception:
            yield sse_pack("error", {"error": "An unexpected error occurred while generating a response."})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/regenerate", methods=["POST"])
@login_required
def regenerate():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    tool = data.get("tool")

    if not conversation_id:
        return jsonify({"error": "conversation_id is required."}), 400

    db = get_db()
    convo = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, current_user_id()),
    ).fetchone()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404

    last_assistant = db.execute(
        "SELECT id FROM messages WHERE conversation_id = ? AND role = 'assistant' "
        "ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if last_assistant:
        db.execute("DELETE FROM messages WHERE id = ?", (last_assistant["id"],))
        db.commit()

    history_rows = db.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in history_rows]
    if not history:
        return jsonify({"error": "Nothing to regenerate yet."}), 400

    reply, used_demo, error = get_ai_response(tool, history)
    if error:
        return jsonify({"error": error}), 502
    reply = repair_mojibake(reply)

    db.execute(
        "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
        (conversation_id, reply, now_iso()),
    )
    db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id))
    db.commit()

    return jsonify({"reply": reply, "demo_mode": used_demo})


@app.route("/api/regenerate/stream", methods=["POST"])
@login_required
def regenerate_stream():
    """Streaming counterpart to /api/regenerate."""
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    tool = data.get("tool")

    if not conversation_id:
        return jsonify({"error": "conversation_id is required."}), 400

    db = get_db()
    convo = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, current_user_id()),
    ).fetchone()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404

    last_assistant = db.execute(
        "SELECT id FROM messages WHERE conversation_id = ? AND role = 'assistant' "
        "ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if last_assistant:
        db.execute("DELETE FROM messages WHERE id = ?", (last_assistant["id"],))
        db.commit()

    history_rows = db.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in history_rows]
    if not history:
        return jsonify({"error": "Nothing to regenerate yet."}), 400

    latest_user_message = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    system_prompt = build_system_prompt(tool)
    error_messages = {
        "timeout": "The AI service took too long to respond. Please try again.",
        "network": "We couldn't reach the AI service. Check your internet connection and try again.",
        "service_down": "The AI service is temporarily unavailable. Please try again shortly.",
        "rate_limit": "The AI service is temporarily rate-limited. Please try again in a moment.",
        "api_error": "The AI service returned an unexpected error. Please try again.",
    }

    def generate():
        chunks = []
        used_demo = False
        try:
            try:
                provider_gen = stream_gemini(system_prompt, history) if AI_PROVIDER == "gemini" \
                    else stream_openrouter(system_prompt, history)
                for chunk in provider_gen:
                    chunk = repair_mojibake(chunk)
                    chunks.append(chunk)
                    yield sse_pack("chunk", {"text": chunk})
            except AIError as e:
                if str(e) == "missing_key":
                    used_demo = True
                    for chunk in stream_demo(tool, latest_user_message):
                        chunk = repair_mojibake(chunk)
                        chunks.append(chunk)
                        yield sse_pack("chunk", {"text": chunk})
                else:
                    yield sse_pack(
                        "error",
                        {"error": error_messages.get(str(e), "Something went wrong. Please try again.")},
                    )
                    return

            reply = "".join(chunks)
            try:
                db2 = get_db()
                db2.execute(
                    "INSERT INTO messages (conversation_id, role, content, created_at) "
                    "VALUES (?, 'assistant', ?, ?)",
                    (conversation_id, reply, now_iso()),
                )
                db2.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id)
                )
                db2.commit()
            except sqlite3.Error:
                pass

            yield sse_pack("done", {"demo_mode": used_demo})

        except GeneratorExit:
            raise
        except Exception:
            yield sse_pack("error", {"error": "An unexpected error occurred while generating a response."})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "ai_provider": AI_PROVIDER,
            "ai_configured": bool(GEMINI_API_KEY or OPENROUTER_API_KEY),
        }
    )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return render_template("login.html", error="Page not found."), 404


@app.errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "An unexpected server error occurred."}), 500
    return "Something went wrong on our end. Please try again.", 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
else:
    # Imported by a production server (e.g. `gunicorn app:app`)
    init_db()
    _default_secrets = {"nexamind-dev-secret-change-me", "change-this-to-a-long-random-string"}
    if app.secret_key in _default_secrets:
        print(
            "WARNING: NexaMind is using a placeholder SECRET_KEY. "
            "Set a real, random SECRET_KEY in your environment before deploying publicly."
        )
