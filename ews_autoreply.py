import os
import re
import time
import json
import queue
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from exchangelib import Account, Configuration, Credentials, DELEGATE
from exchangelib.errors import ErrorNonExistentMailbox

# =========================
# Config defaults
# =========================

DEFAULT_CONFIG = {
    "email": "tu_casilla@chaco.gob.ar",
    "server": "mail.chaco.gob.ar",        # Ajustar
    "auth_type": "NTLM",                  # NTLM típico en on-prem
    "internal_domains": ["@chaco.gob.ar"],

    "start_date": "2026-02-01",           # yyyy-mm-dd
    "poll_seconds": 20,
    "lookback_days": 30,                  # safety: por si start_date muy viejo

    "exclude_emails": [
        # "alguien@dominio.com"
    ],
    "exclude_domains": [
        # "@miempresa.com", "estudio.com"
    ],

    "recent_window_minutes": 720,         # 12h anti-loop por remitente
    "max_replies_per_hour": 60,

    "subject_reply": "Recepción de mensaje",
    "generic_reply_body": (
        "Gracias por su comunicación.\n\n"
        "Informamos que esta casilla no será monitoreada temporalmente.\n"
        "Para consultas, comunicarse con mesaentradas@chaco.gob.ar.\n\n"
        "Atentamente,\n"
        "Administración Tributaria Provincial del Chaco\n"
    ),
}

AUTO_SENDER_TOKENS = (
    "mailer-daemon",
    "postmaster",
    "no-reply",
    "noreply",
    "do-not-reply",
)

AUTO_SUBJECT_TOKENS = (
    "automatic reply",
    "auto reply",
    "out of office",
    "fuera de la oficina",
    "respuesta automática",
    "undeliverable",
    "delivery failure",
)

# =========================
# SQLite
# =========================

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  msg_key TEXT UNIQUE,
  message_id TEXT,
  received_at TEXT,
  sender_email TEXT,
  sender_name TEXT,
  subject TEXT,
  status TEXT,           -- SEEN / REPLIED / SKIPPED / ERROR
  reason TEXT,
  replied_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_email);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);

CREATE TABLE IF NOT EXISTS hourly_counters (
  hour_key TEXT PRIMARY KEY,
  count INTEGER
);

CREATE TABLE IF NOT EXISTS config_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  saved_at TEXT,
  config_json TEXT
);
"""

def db_connect(db_path: str):
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.executescript(DB_SCHEMA)
    return con

def db_upsert_message(con, row: dict):
    # Insert or ignore; then update fields
    cur = con.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO messages(msg_key, message_id, received_at, sender_email, sender_name, subject, status, reason, replied_at)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (
        row.get("msg_key"),
        row.get("message_id"),
        row.get("received_at"),
        row.get("sender_email"),
        row.get("sender_name"),
        row.get("subject"),
        row.get("status"),
        row.get("reason"),
        row.get("replied_at"),
    ))
    cur.execute("""
        UPDATE messages
        SET message_id=?, received_at=?, sender_email=?, sender_name=?, subject=?, status=?, reason=?, replied_at=?
        WHERE msg_key=?
    """, (
        row.get("message_id"),
        row.get("received_at"),
        row.get("sender_email"),
        row.get("sender_name"),
        row.get("subject"),
        row.get("status"),
        row.get("reason"),
        row.get("replied_at"),
        row.get("msg_key"),
    ))
    con.commit()

def db_already_replied(con, msg_key: str) -> bool:
    cur = con.cursor()
    cur.execute("SELECT 1 FROM messages WHERE msg_key=? AND status='REPLIED' LIMIT 1", (msg_key,))
    return cur.fetchone() is not None

def db_last_interaction_with_sender(con, sender_email: str):
    cur = con.cursor()
    cur.execute("""
        SELECT MAX(COALESCE(replied_at, received_at))
        FROM messages
        WHERE sender_email=?
          AND status IN ('REPLIED','SEEN')
    """, (sender_email,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None

def hour_bucket(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:00")

def db_hour_count(con, dt: datetime) -> int:
    hk = hour_bucket(dt)
    cur = con.cursor()
    cur.execute("SELECT count FROM hourly_counters WHERE hour_key=?", (hk,))
    row = cur.fetchone()
    return int(row[0]) if row else 0

def db_inc_hour(con, dt: datetime):
    hk = hour_bucket(dt)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO hourly_counters(hour_key, count) VALUES(?, 1)
        ON CONFLICT(hour_key) DO UPDATE SET count=count+1
    """, (hk,))
    con.commit()

# =========================
# Helpers
# =========================

def normalize_email(s: str) -> str:
    return (s or "").strip().lower()

def parse_start_date(s: str) -> datetime:
    # local naive date -> treat as local midnight, convert to UTC approx
    # If your server is local tz (-03), this is OK for filtering by date.
    dt = datetime.strptime(s.strip(), "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc)

def safe_iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.isoformat(timespec="seconds")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

def message_fingerprint(sender: str, subject: str, received_dt: datetime) -> str:
    # fallback key when message_id not available
    base = f"{normalize_email(sender)}|{(subject or '').strip()}|{safe_iso(received_dt) or ''}"
    return re.sub(r"\s+", " ", base).strip()

def sender_is_blocked(sender_email: str, cfg: dict) -> tuple[bool, str]:
    se = normalize_email(sender_email)
    if not se:
        return True, "sender_empty"

    if any(tok in se for tok in AUTO_SENDER_TOKENS):
        return True, "sender_auto_token"

    for ex in cfg.get("exclude_emails", []):
        if se == normalize_email(ex):
            return True, "sender_excluded_email"

    for dom in cfg.get("exclude_domains", []):
        dom = normalize_email(dom)
        if not dom:
            continue
        # allow "@domain.com" or "domain.com"
        if dom.startswith("@"):
            if se.endswith(dom):
                return True, "sender_excluded_domain"
        else:
            if se.endswith("@" + dom) or se.endswith(dom):
                return True, "sender_excluded_domain"

    return False, ""

def subject_looks_auto(subject: str) -> bool:
    s = (subject or "").strip().lower()
    return any(tok in s for tok in AUTO_SUBJECT_TOKENS)

def is_internal(sender_email: str, cfg: dict) -> bool:
    se = normalize_email(sender_email)
    for dom in cfg.get("internal_domains", []):
        dom = normalize_email(dom)
        if dom and se.endswith(dom):
            return True
    return False

def headers_indicate_autoreply(msg) -> bool:
    """
    exchangelib may or may not expose headers reliably depending on server.
    We'll try a few patterns; if unavailable, returns False.
    """
    try:
        hdrs = getattr(msg, "headers", None)
        if not hdrs:
            return False
        # hdrs can be a dict-like or list; we try string search
        text = str(hdrs).lower()
        # Common indicators
        if "auto-submitted" in text and "auto-generated" in text:
            return True
        if "x-auto-response-suppress" in text:
            return True
        if "precedence: bulk" in text or "precedence: junk" in text:
            return True
        return False
    except Exception:
        return False

# =========================
# Engine thread
# =========================

@dataclass
class EngineStatus:
    running: bool = False
    last_tick: str = ""
    last_error: str = ""
    replied_count_session: int = 0

class AutoReplyEngine(threading.Thread):
    def __init__(self, cfg: dict, db_path: str, logq: queue.Queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.db_path = db_path
        self.logq = logq
        self._stop = threading.Event()
        self.status = EngineStatus()

        self.con = db_connect(db_path)
        self.account = None

    def stop(self):
        self._stop.set()

    def log(self, level: str, msg: str):
        ts = datetime.now().isoformat(timespec="seconds")
        self.logq.put((ts, level, msg))

    def connect_ews(self):
        email = self.cfg["email"]
        server = self.cfg["server"]
        auth_type = self.cfg.get("auth_type", "NTLM")
        password = os.getenv("ATP_MAIL_PASSWORD")

        if not password:
            raise RuntimeError("Falta ATP_MAIL_PASSWORD (variable de entorno).")

        creds = Credentials(username=email, password=password)
        config = Configuration(server=server, credentials=creds, auth_type=auth_type)

        self.account = Account(
            primary_smtp_address=email,
            config=config,
            autodiscover=False,
            access_type=DELEGATE
        )

    def can_send_now(self) -> bool:
        now = datetime.now()
        maxh = int(self.cfg.get("max_replies_per_hour", 60))
        return db_hour_count(self.con, now) < maxh

    def run(self):
        self.status.running = True
        try:
            self.connect_ews()
            self.log("INFO", f"Conectado EWS: {self.cfg['email']} @ {self.cfg['server']}")
        except Exception as e:
            self.status.last_error = str(e)
            self.log("ERROR", f"No pudo conectar a EWS: {e}")
            self.status.running = False
            return

        poll = int(self.cfg.get("poll_seconds", 20))
        start_dt = parse_start_date(self.cfg.get("start_date", "2026-02-01"))

        # Safety: do not scan infinite history
        lookback_days = int(self.cfg.get("lookback_days", 30))
        min_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        if start_dt < min_dt:
            start_dt = min_dt

        recent_window = int(self.cfg.get("recent_window_minutes", 720))
        subject_reply = self.cfg.get("subject_reply", "Recepción de mensaje")
        body_reply = self.cfg.get("generic_reply_body", "")

        while not self._stop.is_set():
            try:
                self.status.last_tick = datetime.now().isoformat(timespec="seconds")

                qs = self.account.inbox.filter(
                    is_read=False,
                    datetime_received__gt=start_dt
                ).only("subject", "sender", "datetime_received", "message_id", "categories", "headers")

                for msg in qs:
                    sender_email = ""
                    sender_name = ""
                    if msg.sender:
                        sender_email = msg.sender.email_address or ""
                        sender_name = getattr(msg.sender, "name", "") or ""

                    subject = msg.subject or ""
                    received_dt = msg.datetime_received
                    if received_dt is None:
                        received_dt = datetime.now(timezone.utc)

                    msg_id = getattr(msg, "message_id", None) or ""
                    msg_key = msg_id.strip() or message_fingerprint(sender_email, subject, received_dt)

                    # Basic row
                    row = {
                        "msg_key": msg_key,
                        "message_id": msg_id,
                        "received_at": safe_iso(received_dt),
                        "sender_email": sender_email,
                        "sender_name": sender_name,
                        "subject": subject,
                        "status": "SEEN",
                        "reason": "",
                        "replied_at": None
                    }

                    # Always record that we saw it
                    db_upsert_message(self.con, row)

                    # Skip internal if desired (common case)
                    if is_internal(sender_email, self.cfg):
                        row["status"] = "SKIPPED"
                        row["reason"] = "internal_sender"
                        db_upsert_message(self.con, row)
                        msg.is_read = True
                        msg.save(update_fields=["is_read"])
                        continue

                    blocked, why = sender_is_blocked(sender_email, self.cfg)
                    if blocked:
                        row["status"] = "SKIPPED"
                        row["reason"] = why
                        db_upsert_message(self.con, row)
                        msg.is_read = True
                        msg.save(update_fields=["is_read"])
                        continue

                    if subject_looks_auto(subject) or headers_indicate_autoreply(msg):
                        row["status"] = "SKIPPED"
                        row["reason"] = "auto_reply_detected"
                        db_upsert_message(self.con, row)
                        msg.is_read = True
                        msg.save(update_fields=["is_read"])
                        continue

                    if db_already_replied(self.con, msg_key):
                        row["status"] = "SKIPPED"
                        row["reason"] = "already_replied_same_msg"
                        db_upsert_message(self.con, row)
                        msg.is_read = True
                        msg.save(update_fields=["is_read"])
                        continue

                    # Anti-loop: if we interacted recently with same sender, skip
                    last = db_last_interaction_with_sender(self.con, normalize_email(sender_email))
                    if last:
                        now_utc = datetime.now(timezone.utc)
                        # last might be naive; normalize
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        if (now_utc - last) < timedelta(minutes=recent_window):
                            row["status"] = "SKIPPED"
                            row["reason"] = f"recent_sender_window<{recent_window}m"
                            db_upsert_message(self.con, row)
                            msg.is_read = True
                            msg.save(update_fields=["is_read"])
                            continue

                    if not self.can_send_now():
                        # Don't mark as read, so it can be tried later
                        self.log("WARN", "Límite por hora alcanzado; pausa hasta la próxima hora.")
                        break

                    # Reply
                    try:
                        reply = msg.reply(subject=subject_reply, body=body_reply)
                        reply.send()

                        # Tag + read
                        cats = set(msg.categories or [])
                        cats.add("AUTO-REPLIED")
                        msg.categories = list(cats)
                        msg.is_read = True
                        msg.save(update_fields=["categories", "is_read"])

                        row["status"] = "REPLIED"
                        row["reason"] = "ok"
                        row["replied_at"] = safe_iso(datetime.now(timezone.utc))
                        db_upsert_message(self.con, row)
                        db_inc_hour(self.con, datetime.now())

                        self.status.replied_count_session += 1
                        self.log("INFO", f"Respondido a {sender_email} | {subject[:80]}")

                    except Exception as e:
                        row["status"] = "ERROR"
                        row["reason"] = f"reply_failed: {e}"
                        db_upsert_message(self.con, row)
                        self.log("ERROR", f"Fallo al responder a {sender_email}: {e}")
                        # Mark read to avoid stuck loop on poison message
                        try:
                            msg.is_read = True
                            msg.save(update_fields=["is_read"])
                        except Exception:
                            pass

            except Exception as e:
                self.status.last_error = str(e)
                self.log("ERROR", f"Loop error: {e}")

            time.sleep(poll)

        self.status.running = False
        self.log("INFO", "Motor detenido.")

# =========================
# UI (tkinter)
# =========================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("EWS Auto-Reply (ATP)")
        self.geometry("1100x720")

        self.cfg = DEFAULT_CONFIG.copy()
        self.engine = None
        self.logq = queue.Queue()
        self.db_path = os.path.abspath("autoreply.sqlite")
        self.con = db_connect(self.db_path)

        self._build_ui()
        self._load_config_if_exists()
        self.after(250, self._drain_logs)
        self.after(1000, self._refresh_table)

    def _build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        # Top config
        cfg_box = ttk.LabelFrame(frm, text="Configuración", padding=10)
        cfg_box.pack(fill="x")

        self.var_email = tk.StringVar(value=self.cfg["email"])
        self.var_server = tk.StringVar(value=self.cfg["server"])
        self.var_auth = tk.StringVar(value=self.cfg["auth_type"])
        self.var_start_date = tk.StringVar(value=self.cfg["start_date"])
        self.var_poll = tk.IntVar(value=self.cfg["poll_seconds"])
        self.var_recent = tk.IntVar(value=self.cfg["recent_window_minutes"])
        self.var_maxh = tk.IntVar(value=self.cfg["max_replies_per_hour"])

        r = 0
        ttk.Label(cfg_box, text="Email:").grid(row=r, column=0, sticky="w")
        ttk.Entry(cfg_box, textvariable=self.var_email, width=35).grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(cfg_box, text="Server EWS:").grid(row=r, column=2, sticky="w")
        ttk.Entry(cfg_box, textvariable=self.var_server, width=25).grid(row=r, column=3, sticky="w", padx=6)
        ttk.Label(cfg_box, text="Auth:").grid(row=r, column=4, sticky="w")
        ttk.Combobox(cfg_box, textvariable=self.var_auth, values=["NTLM", "BASIC"], width=10, state="readonly").grid(row=r, column=5, sticky="w", padx=6)

        r += 1
        ttk.Label(cfg_box, text="Desde (YYYY-MM-DD):").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Entry(cfg_box, textvariable=self.var_start_date, width=15).grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(cfg_box, text="Polling (seg):").grid(row=r, column=2, sticky="w")
        ttk.Spinbox(cfg_box, from_=5, to=300, textvariable=self.var_poll, width=6).grid(row=r, column=3, sticky="w", padx=6)
        ttk.Label(cfg_box, text="Ventana anti-loop (min):").grid(row=r, column=4, sticky="w")
        ttk.Spinbox(cfg_box, from_=10, to=10080, textvariable=self.var_recent, width=8).grid(row=r, column=5, sticky="w", padx=6)

        r += 1
        ttk.Label(cfg_box, text="Máx respuestas / hora:").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(cfg_box, from_=1, to=500, textvariable=self.var_maxh, width=8).grid(row=r, column=1, sticky="w", padx=6)

        # Exclusions and message
        mid = ttk.Frame(frm)
        mid.pack(fill="both", expand=False, pady=10)

        left = ttk.LabelFrame(mid, text="Exclusiones", padding=10)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        ttk.Label(left, text="Emails excluidos (uno por línea):").pack(anchor="w")
        self.txt_ex_emails = tk.Text(left, height=6)
        self.txt_ex_emails.pack(fill="both", expand=True, pady=6)

        ttk.Label(left, text="Dominios excluidos (uno por línea, ej: @chaco.gob.ar / estudio.com):").pack(anchor="w")
        self.txt_ex_domains = tk.Text(left, height=6)
        self.txt_ex_domains.pack(fill="both", expand=True, pady=6)

        right = ttk.LabelFrame(mid, text="Respuesta", padding=10)
        right.pack(side="left", fill="both", expand=True)

        self.var_subject = tk.StringVar(value=self.cfg["subject_reply"])
        ttk.Label(right, text="Asunto:").pack(anchor="w")
        ttk.Entry(right, textvariable=self.var_subject).pack(fill="x", pady=(0, 6))

        ttk.Label(right, text="Cuerpo del mensaje genérico:").pack(anchor="w")
        self.txt_body = tk.Text(right, height=14)
        self.txt_body.pack(fill="both", expand=True, pady=6)

        # Controls
        ctrl = ttk.Frame(frm)
        ctrl.pack(fill="x", pady=6)

        self.btn_start = ttk.Button(ctrl, text="Iniciar", command=self.start_engine)
        self.btn_stop = ttk.Button(ctrl, text="Detener", command=self.stop_engine, state="disabled")
        self.btn_save = ttk.Button(ctrl, text="Guardar config", command=self.save_config)
        self.btn_load = ttk.Button(ctrl, text="Cargar config", command=self.load_config)

        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_save.pack(side="left", padx=6)
        self.btn_load.pack(side="left", padx=6)

        ttk.Label(ctrl, text=f"DB: {self.db_path}").pack(side="right")

        # Logs
        log_box = ttk.LabelFrame(frm, text="Logs", padding=10)
        log_box.pack(fill="both", expand=True, pady=(10, 0))

        self.txt_logs = tk.Text(log_box, height=8, state="disabled")
        self.txt_logs.pack(fill="both", expand=False)

        # Table
        table_box = ttk.LabelFrame(frm, text="Últimos eventos (SQLite)", padding=10)
        table_box.pack(fill="both", expand=True, pady=(10, 0))

        cols = ("received_at", "sender_email", "subject", "status", "reason", "replied_at")
        self.tree = ttk.Treeview(table_box, columns=cols, show="headings", height=10)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=160 if c != "subject" else 380, anchor="w")
        self.tree.pack(fill="both", expand=True)

        self._apply_cfg_to_text()

    def _apply_cfg_to_text(self):
        # Fill text areas from cfg
        self.txt_ex_emails.delete("1.0", "end")
        self.txt_ex_emails.insert("1.0", "\n".join(self.cfg.get("exclude_emails", [])))

        self.txt_ex_domains.delete("1.0", "end")
        self.txt_ex_domains.insert("1.0", "\n".join(self.cfg.get("exclude_domains", [])))

        self.txt_body.delete("1.0", "end")
        self.txt_body.insert("1.0", self.cfg.get("generic_reply_body", ""))

    def _collect_cfg_from_ui(self) -> dict:
        cfg = self.cfg.copy()
        cfg["email"] = self.var_email.get().strip()
        cfg["server"] = self.var_server.get().strip()
        cfg["auth_type"] = self.var_auth.get().strip()
        cfg["start_date"] = self.var_start_date.get().strip()
        cfg["poll_seconds"] = int(self.var_poll.get())
        cfg["recent_window_minutes"] = int(self.var_recent.get())
        cfg["max_replies_per_hour"] = int(self.var_maxh.get())

        cfg["subject_reply"] = self.var_subject.get().strip()

        cfg["exclude_emails"] = [ln.strip() for ln in self.txt_ex_emails.get("1.0", "end").splitlines() if ln.strip()]
        cfg["exclude_domains"] = [ln.strip() for ln in self.txt_ex_domains.get("1.0", "end").splitlines() if ln.strip()]
        cfg["generic_reply_body"] = self.txt_body.get("1.0", "end").rstrip("\n")

        return cfg

    def start_engine(self):
        if self.engine and self.engine.status.running:
            messagebox.showinfo("Info", "Ya está corriendo.")
            return

        cfg = self._collect_cfg_from_ui()

        # quick validation
        try:
            datetime.strptime(cfg["start_date"], "%Y-%m-%d")
        except Exception:
            messagebox.showerror("Error", "start_date inválida. Use YYYY-MM-DD.")
            return

        if not os.getenv("ATP_MAIL_PASSWORD"):
            messagebox.showerror("Error", "Falta ATP_MAIL_PASSWORD en variables de entorno.")
            return

        self.cfg = cfg
        self.engine = AutoReplyEngine(cfg=self.cfg, db_path=self.db_path, logq=self.logq)
        self.engine.start()

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._append_log("INFO", "Motor iniciado.")

    def stop_engine(self):
        if self.engine:
            self.engine.stop()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._append_log("INFO", "Se solicitó detener el motor.")

    def _append_log(self, level: str, msg: str):
        ts = datetime.now().isoformat(timespec="seconds")
        self.txt_logs.config(state="normal")
        self.txt_logs.insert("end", f"[{ts}] {level}: {msg}\n")
        self.txt_logs.see("end")
        self.txt_logs.config(state="disabled")

    def _drain_logs(self):
        try:
            while True:
                ts, level, msg = self.logq.get_nowait()
                self.txt_logs.config(state="normal")
                self.txt_logs.insert("end", f"[{ts}] {level}: {msg}\n")
                self.txt_logs.see("end")
                self.txt_logs.config(state="disabled")
        except queue.Empty:
            pass
        self.after(250, self._drain_logs)

    def _refresh_table(self):
        cur = self.con.cursor()
        cur.execute("""
            SELECT received_at, sender_email, subject, status, reason, replied_at
            FROM messages
            ORDER BY id DESC
            LIMIT 200
        """)
        rows = cur.fetchall()

        # refresh tree
        for it in self.tree.get_children():
            self.tree.delete(it)
        for r in rows:
            self.tree.insert("", "end", values=r)

        self.after(2000, self._refresh_table)

    def save_config(self):
        cfg = self._collect_cfg_from_ui()
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        # audit in DB
        cur = self.con.cursor()
        cur.execute("INSERT INTO config_audit(saved_at, config_json) VALUES(?,?)",
                    (datetime.now().isoformat(timespec="seconds"), json.dumps(cfg, ensure_ascii=False)))
        self.con.commit()

        self._append_log("INFO", f"Config guardada: {path}")

    def load_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        self.cfg = cfg
        self.var_email.set(cfg.get("email", ""))
        self.var_server.set(cfg.get("server", ""))
        self.var_auth.set(cfg.get("auth_type", "NTLM"))
        self.var_start_date.set(cfg.get("start_date", ""))
        self.var_poll.set(int(cfg.get("poll_seconds", 20)))
        self.var_recent.set(int(cfg.get("recent_window_minutes", 720)))
        self.var_maxh.set(int(cfg.get("max_replies_per_hour", 60)))
        self.var_subject.set(cfg.get("subject_reply", "Recepción de mensaje"))

        self._apply_cfg_to_text()
        self._append_log("INFO", f"Config cargada: {path}")

    def _load_config_if_exists(self):
        # Optional: load ./config.json if present
        path = os.path.abspath("config.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.cfg = cfg
                self.var_email.set(cfg.get("email", ""))
                self.var_server.set(cfg.get("server", ""))
                self.var_auth.set(cfg.get("auth_type", "NTLM"))
                self.var_start_date.set(cfg.get("start_date", ""))
                self.var_poll.set(int(cfg.get("poll_seconds", 20)))
                self.var_recent.set(int(cfg.get("recent_window_minutes", 720)))
                self.var_maxh.set(int(cfg.get("max_replies_per_hour", 60)))
                self.var_subject.set(cfg.get("subject_reply", "Recepción de mensaje"))
                self._apply_cfg_to_text()
                self._append_log("INFO", "Config.json cargado automáticamente.")
            except Exception as e:
                self._append_log("WARN", f"No se pudo cargar config.json: {e}")

if __name__ == "__main__":
    app = App()
    app.mainloop()
