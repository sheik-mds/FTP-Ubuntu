"""
GTS XFTP - Colorful Professional UI with Fixed Next Button (ENHANCED)
--------------------------------------------------------------------
✅ Keeps your current UI + all existing functionality
✅ Dashboard: adds a new "Stats" column/card showing:
   - Overall servers / Production servers / Staging servers
   - Total transferred size to Production / Staging (based on completed jobs)
✅ File Transfer: adds a "Compress" button with loader
   - Works only when a folder (DIR) is selected
   - Creates a ZIP inside the CURRENT path (same level as that folder)
   - Does NOT change your SEND logic (SEND still zips to /tmp and uploads)
✅ Added environment icons for all environments

Dependencies:
  pip install flask flask-login paramiko werkzeug
"""

import os
import sqlite3
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import paramiko
from flask import (
    Flask, request, redirect, url_for, render_template_string,
    flash, session, jsonify
)
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# =========================
# CONFIG
# =========================
APP_PORT = 5009
DB_PATH = "/home/sheik/FTP-Site/FTP-Site/app.db"     # ✅ file path
HOST_UPLOADS = "/home/sheik/FTP-Site/FTP-Site"       # ✅ browse here on host
REMOTE_SEND_DEST = "/tmp"                      # ✅ send into remote
MAX_LIST_ITEMS = 5000

# Ensure directories exist
os.makedirs(HOST_UPLOADS, exist_ok=True)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# =========================
# Flask setup
# =========================
app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "change-me-please-very-long-secret")

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

TRANSFER_STATUS = {}
TRANSFER_LOCK = threading.Lock()

# =========================
# DB helpers
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'admin',
        last_login TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        host TEXT NOT NULL,
        ssh_user TEXT NOT NULL,
        pem_path TEXT NOT NULL,
        environment TEXT NOT NULL,
        online INTEGER NOT NULL DEFAULT 1,
        tag TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # Auto-migrate for older DBs
    cur.execute("PRAGMA table_info(servers)")
    cols = {r[1] for r in cur.fetchall()}
    if "tag" not in cols:
        cur.execute("ALTER TABLE servers ADD COLUMN tag TEXT")
        conn.commit()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS site_shortcuts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_name TEXT NOT NULL,
        domain_ip TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # ✅ NEW: transfer history table (for dashboard size metrics)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        server_host TEXT,
        item_rel TEXT,
        zipped_bytes INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    conn.commit()

    # Create default user if DB empty (credentials not shown in UI)
    cur.execute("SELECT COUNT(*) AS c FROM users")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            ("superadmin01", generate_password_hash("Admin@123"), "superadmin")
        )
        conn.commit()

    conn.close()

init_db()

def migrate_db():
    """Migrate existing database to add missing columns safely."""
    try:
        conn = db()
        cur = conn.cursor()

        # users.last_login
        cur.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cur.fetchall()]
        if "last_login" not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
            conn.commit()

        # servers.tag already handled in init_db(), keep safe here as well
        cur.execute("PRAGMA table_info(servers)")
        columns = [col[1] for col in cur.fetchall()]
        if "tag" not in columns:
            cur.execute("ALTER TABLE servers ADD COLUMN tag TEXT")
            conn.commit()

        conn.close()
    except Exception as e:
        print(f"Migration error: {e}")

migrate_db()

# =========================
# Auth
# =========================
class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.role = row["role"]

@login_manager.user_loader
def load_user(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return User(row) if row else None

# =========================
# Helpers
# =========================
def normalize_env(env: str) -> str:
    env = (env or "").strip().lower()
    if env in ("production", "staging", "development"):
        return env
    return "staging"

def is_allowed_pem_path(p: str) -> bool:
    return bool(p) and p.startswith("/") and p.endswith(".pem") and os.path.exists(p)

def set_job(job_id, **kwargs):
    with TRANSFER_LOCK:
        TRANSFER_STATUS.setdefault(job_id, {})
        TRANSFER_STATUS[job_id].update(kwargs)

def get_servers(env=None):
    conn = db()
    cur = conn.cursor()
    if env:
        cur.execute("SELECT * FROM servers WHERE environment=? ORDER BY id DESC", (env,))
    else:
        cur.execute("SELECT * FROM servers ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def safe_join(base_abs: str, rel: str) -> str:
    base = Path(base_abs).resolve()
    rel = (rel or "").lstrip("/")
    target = (base / rel).resolve()
    if str(target) == str(base) or str(target).startswith(str(base) + os.sep):
        return str(target)
    raise ValueError("Unsafe path traversal")

def list_host_dir(rel_dir: str):
    rel_dir = (rel_dir or "").strip().lstrip("/")
    current_abs = safe_join(HOST_UPLOADS, rel_dir)
    current_rel = rel_dir

    if not os.path.isdir(current_abs):
        current_abs = HOST_UPLOADS
        current_rel = ""

    items = []
    base = Path(current_abs)
    try:
        for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            items.append({
                "name": p.name,
                "rel": (current_rel + "/" + p.name).strip("/"),
                "is_dir": p.is_dir(),
            })
            if len(items) >= MAX_LIST_ITEMS:
                break
    except Exception:
        pass

    return items, current_rel

def format_bytes(n: int) -> str:
    try:
        n = int(n or 0)
    except Exception:
        n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            if u == "B":
                return f"{int(f)} {u}"
            return f"{f:.2f} {u}"
        f /= 1024.0
    return f"{n} B"

# =========================
# SSH/SFTP + Transfer (SEND only)
# =========================
def make_ssh_client(host: str, user: str, pem_path: str) -> paramiko.SSHClient:
    if not is_allowed_pem_path(pem_path):
        raise RuntimeError(f"PEM path invalid or missing on host: {pem_path}")

    key = paramiko.RSAKey.from_private_key_file(pem_path)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, username=user, pkey=key, timeout=25)
    return ssh

def exec_remote(ssh: paramiko.SSHClient, cmd: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode(errors="ignore")
    err = stderr.read().decode(errors="ignore")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err

def zip_local_path(src_abs: str, zip_abs: str):
    src_abs = os.path.abspath(src_abs)
    with zipfile.ZipFile(zip_abs, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if os.path.isdir(src_abs):
            base_dir = os.path.dirname(src_abs)
            for root, _, files in os.walk(src_abs):
                for f in files:
                    full = os.path.join(root, f)
                    arc = os.path.relpath(full, base_dir)
                    z.write(full, arcname=arc)
        else:
            z.write(src_abs, arcname=os.path.basename(src_abs))

def log_transfer(job_id: str, env: str, server_host: str, item_rel: str, zipped_bytes: int, status: str):
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO transfers (job_id, environment, server_host, item_rel, zipped_bytes, status, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (job_id, env, server_host, item_rel, int(zipped_bytes or 0), status, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        # Do not break the transfer if logging fails
        pass

def run_send_job(job_id, server, host_rel_path, env: str):
    set_job(job_id, status="running", start_time=datetime.utcnow().isoformat(), message="Starting SEND...")

    ssh = None
    sftp = None
    tmp_zip_local = None
    zip_bytes = 0

    try:
        src_abs = safe_join(HOST_UPLOADS, host_rel_path)
        if not os.path.exists(src_abs):
            raise RuntimeError(f"Host path not found: {src_abs}")

        base_name = os.path.basename(src_abs.rstrip("/"))
        tmp_zip_local = f"/tmp/send_{job_id}.zip"
        tmp_zip_remote = f"{REMOTE_SEND_DEST}/send_{job_id}.zip"

        set_job(job_id, message="Zipping on host...")
        zip_local_path(src_abs, tmp_zip_local)

        try:
            zip_bytes = os.path.getsize(tmp_zip_local)
        except Exception:
            zip_bytes = 0

        set_job(job_id, message="Connecting to remote...")
        ssh = make_ssh_client(server["host"], server["ssh_user"], server["pem_path"])
        sftp = ssh.open_sftp()

        set_job(job_id, message="Uploading zip to remote...")
        sftp.put(tmp_zip_local, tmp_zip_remote)

        set_job(job_id, message="Extracting on remote (/tmp)...")
        rc, out, err = exec_remote(
            ssh,
            f"mkdir -p '{REMOTE_SEND_DEST}' && "
            f"unzip -o '{tmp_zip_remote}' -d '{REMOTE_SEND_DEST}' >/dev/null 2>&1 && "
            f"rm -f '{tmp_zip_remote}'"
        )
        if rc != 0:
            raise RuntimeError(f"Remote extract failed: {err or out}")

        set_job(
            job_id,
            status="completed",
            end_time=datetime.utcnow().isoformat(),
            message=f"✅ Successfully transferred {base_name} to {server['host']}:{REMOTE_SEND_DEST}"
        )

        # ✅ dashboard stats
        log_transfer(job_id, env, server["host"], host_rel_path, zip_bytes, "completed")

    except Exception as e:
        set_job(job_id, status="failed", end_time=datetime.utcnow().isoformat(), message=str(e))
        log_transfer(job_id, env, server.get("host", ""), host_rel_path, zip_bytes, "failed")

    finally:
        try:
            if sftp:
                sftp.close()
        except Exception:
            pass
        try:
            if ssh:
                ssh.close()
        except Exception:
            pass
        try:
            if tmp_zip_local and os.path.exists(tmp_zip_local):
                os.remove(tmp_zip_local)
        except Exception:
            pass

# =========================
# UI Templates (your current UI preserved; only added new cards/buttons)
# =========================
BASE_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FTP • {{ title }} </title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            /* Colorful Gradient Theme */
            --color-primary: #6d28d9;
            --color-primary-light: #8b5cf6;
            --color-primary-dark: #5b21b6;
            --color-secondary: #0ea5e9;
            --color-accent: #f59e0b;
            --color-accent-2: #10b981;
            --color-accent-3: #ef4444;
            --color-accent-4: #8b5cf6;
            
            /* Environment Colors */
            --color-production: #ef4444;
            --color-staging: #f59e0b;
            --color-development: #0ea5e9;
            
            /* Background Colors */
            --color-bg-gradient: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --color-bg-light: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            --color-bg-card: rgba(255, 255, 255, 0.95);
            --color-bg-sidebar: linear-gradient(180deg, #1e1b4b 0%, #312e81 100%);
            
            /* Text Colors */
            --color-text-dark: #1f2937;
            --color-text-medium: #4b5563;
            --color-text-light: #6b7280;
            --color-text-white: #ffffff;
            
            /* Status Colors */
            --color-success: #10b981;
            --color-warning: #f59e0b;
            --color-danger: #ef4444;
            --color-info: #0ea5e9;
            
            /* UI Colors */
            --color-border: #e5e7eb;
            --color-border-light: #f3f4f6;
            --color-shadow: rgba(0, 0, 0, 0.1);
            
            /* Shadows */
            --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
            --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
            --shadow-xl: 0 20px 25px -5px rgba(0, 0, 0, 0.1);
            --shadow-2xl: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            
            /* Border Radius */
            --radius-sm: 0.5rem;
            --radius-md: 0.75rem;
            --radius-lg: 1rem;
            --radius-xl: 1.5rem;
            --radius-full: 9999px;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: var(--color-bg-gradient);
            min-height: 100vh;
            color: var(--color-text-dark);
            line-height: 1.6;
            overflow: hidden;
        }

        /* Sidebar */
        .sidebar {
            width: 280px;
            background: var(--color-bg-sidebar);
            color: var(--color-text-white);
            display: flex;
            flex-direction: column;
            position: fixed;
            height: 100vh;
            z-index: 1000;
            left: -280px;
            top: 0;
            transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 4px 0 15px rgba(0, 0, 0, 0.2);
            overflow-y: auto;
        }

        .sidebar.active {
            left: 0;
        }

        .sidebar-header {
            padding: 2rem 1.5rem 1.5rem;
            background: rgba(255, 255, 255, 0.05);
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }

        .logo {
            display: flex;
            align-items: center;
            gap: 1rem;
        }

        .logo-icon {
            width: 48px;
            height: 48px;
            border-radius: var(--radius-md);
            background: linear-gradient(135deg, var(--color-primary-light), var(--color-accent));
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.5rem;
            box-shadow: 0 4px 12px rgba(107, 70, 193, 0.4);
        }

        .logo-text {
            font-family: 'Poppins', sans-serif;
            font-weight: 700;
            font-size: 1.5rem;
            background: linear-gradient(135deg, #fff, #c7d2fe);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: 0.5px;
        }

        .nav {
            flex: 1;
            padding: 1.5rem 1rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        .nav-item {
            display: flex;
            align-items: center;
            gap: 1rem;
            padding: 1rem 1.25rem;
            color: rgba(255, 255, 255, 0.8);
            text-decoration: none;
            border-radius: var(--radius-md);
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }

        .nav-item::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            height: 100%;
            width: 4px;
            background: linear-gradient(to bottom, var(--color-primary-light), var(--color-accent));
            transform: translateX(-100%);
            transition: transform 0.3s ease;
        }

        .nav-item:hover {
            background: rgba(255, 255, 255, 0.1);
            color: var(--color-text-white);
            transform: translateX(5px);
        }

        .nav-item:hover::before {
            transform: translateX(0);
        }

        .nav-item.active {
            background: rgba(255, 255, 255, 0.15);
            color: var(--color-text-white);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        }

        .nav-item.active::before {
            transform: translateX(0);
        }

        .nav-item i {
            font-size: 1.25rem;
            width: 24px;
            text-align: center;
        }

        .nav-item span {
            font-weight: 500;
            font-size: 0.95rem;
        }

        .sidebar-footer {
            padding: 1.5rem;
            border-top: 1px solid rgba(255, 255, 255, 0.1);
            background: rgba(255, 255, 255, 0.05);
        }

        .user-info {
            display: flex;
            align-items: center;
            gap: 1rem;
        }

        .user-avatar {
            width: 48px;
            height: 48px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--color-primary-light), var(--color-accent));
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            font-size: 1.25rem;
            color: white;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2);
        }

        .user-details {
            flex: 1;
        }

        .user-name {
            font-weight: 600;
            font-size: 0.95rem;
            color: var(--color-text-white);
        }

        .user-role {
            font-size: 0.8rem;
            color: rgba(255, 255, 255, 0.7);
            margin-top: 0.25rem;
        }

        .logout-btn {
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: rgba(255, 255, 255, 0.8);
            width: 40px;
            height: 40px;
            border-radius: var(--radius-sm);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.3s ease;
        }

        .logout-btn:hover {
            background: rgba(255, 255, 255, 0.2);
            color: var(--color-text-white);
            transform: rotate(15deg);
        }

        /* Main Content */
        .main-content {
            flex: 1;
            min-height: 100vh;
            transition: margin-left 0.3s ease, padding-top 0.3s ease;
            background: var(--color-bg-light);
            position: relative;
            z-index: 1;
            width: 100%;
            padding: 1rem;
        }

        @media (min-width: 769px) {
            .sidebar {
                position: fixed;
                left: 0;
            }
            
            .main-content {
                margin-left: 280px;
                padding: 2rem;
                width: 93%;
            }
            
            .mobile-menu-toggle {
                display: none;
            }
            
            .sidebar.active {
                left: 0;
            }
        }

        /* Mobile Menu Toggle */
        .mobile-menu-toggle {
            position: fixed;
            top: 1.5rem;
            left: 1.5rem;
            z-index: 1001;
            background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
            border: none;
            width: 48px;
            height: 48px;
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            box-shadow: var(--shadow-lg);
            transition: all 0.3s ease;
        }

        .mobile-menu-toggle:hover {
            transform: scale(1.1);
            box-shadow: var(--shadow-xl);
        }

        .mobile-menu-toggle i {
            color: white;
            font-size: 1.25rem;
        }

        /* Overlay for mobile */
        .sidebar-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.5);
            z-index: 999;
            display: none;
        }

        .sidebar-overlay.active {
            display: block;
        }

        /* Content Header */
        .content-header {
            padding: 2rem 1.5rem 1rem;
            background: transparent;
        }

        @media (max-width: 768px) {
            .content-header {
                padding: 5rem 1rem 1rem;
            }
        }

        .page-title {
            font-family: 'Poppins', sans-serif;
            font-size: 2rem;
            font-weight: 700;
            background: linear-gradient(135deg, var(--color-primary), var(--color-accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
            letter-spacing: -0.5px;
        }

        .page-subtitle {
            font-size: 1rem;
            color: var(--color-text-medium);
            font-weight: 400;
            max-width: 800px;
        }

        /* Cards */
        .card {
            background: var(--color-bg-card);
            border-radius: var(--radius-xl);
            border: 1px solid rgba(255, 255, 255, 0.3);
            box-shadow: var(--shadow-xl);
            overflow: hidden;
            transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            backdrop-filter: blur(10px);
            margin-bottom: 1.5rem;
        }

        .card:hover {
            transform: translateY(-5px);
            box-shadow: var(--shadow-2xl);
        }

        .card-header {
            padding: 1.5rem;
            border-bottom: 1px solid var(--color-border);
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.9), rgba(255, 255, 255, 0.7));
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 1rem;
        }

        .card-title {
            font-family: 'Poppins', sans-serif;
            font-size: 1.25rem;
            font-weight: 600;
            color: var(--color-primary-dark);
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .card-title i {
            background: linear-gradient(135deg, var(--color-primary), var(--color-accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-size: 1.25rem;
        }

        .card-body {
            padding: 1.5rem;
        }

        /* Forms */
        .form-group {
            margin-bottom: 1.5rem;
        }

        .form-label {
            display: block;
            font-size: 0.95rem;
            font-weight: 500;
            color: var(--color-text-medium);
            margin-bottom: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .form-control {
            width: 100%;
            padding: 1rem 1.25rem;
            border: 2px solid var(--color-border);
            border-radius: var(--radius-lg);
            font-size: 1rem;
            transition: all 0.3s ease;
            background: rgba(255, 255, 255, 0.9);
            font-family: 'Inter', sans-serif;
        }

        .form-control:focus {
            outline: none;
            border-color: var(--color-primary-light);
            box-shadow: 0 0 0 4px rgba(139, 92, 246, 0.2);
            background: white;
        }

        /* Radio Groups */
        .radio-group {
            border: 2px solid var(--color-border);
            border-radius: var(--radius-lg);
            padding: 1rem;
            background: white;
            margin-bottom: 1.5rem;
        }

        .radio-item {
            display: flex;
            align-items: flex-start;
            gap: 1rem;
            padding: 0.75rem;
            border-bottom: 1px solid var(--color-border-light);
            cursor: pointer;
            transition: all 0.3s ease;
        }

        .radio-item:last-child {
            border-bottom: none;
        }

        .radio-item:hover {
            background: rgba(139, 92, 246, 0.05);
            border-radius: var(--radius-md);
        }

        .radio-item input[type="radio"] {
            margin-top: 0.25rem;
        }

        .radio-item-content {
            flex: 1;
        }

        .radio-item b {
            font-size: 1rem;
            color: var(--color-text-dark);
            display: block;
            margin-bottom: 0.25rem;
        }

        .radio-item span {
            font-size: 0.875rem;
            color: var(--color-text-light);
            display: block;
        }

        /* Buttons */
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.75rem;
            padding: 0.875rem 1.5rem;
            border-radius: var(--radius-lg);
            font-family: 'Poppins', sans-serif;
            font-weight: 600;
            font-size: 0.95rem;
            cursor: pointer;
            transition: all 0.3s ease;
            border: none;
            outline: none;
            text-decoration: none;
            position: relative;
            overflow: hidden;
            white-space: nowrap;
        }

        .btn::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: 0;
            height: 0;
            border-radius: 50%;
            background: rgba(255, 255, 255, 0.3);
            transform: translate(-50%, -50%);
            transition: width 0.6s, height 0.6s;
        }

        .btn:hover::before {
            width: 300px;
            height: 300px;
        }

        .btn-primary {
            # background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
                background: linear-gradient(135deg, #39d928, #0d4312);
            color: white;
            box-shadow: 0 4px 15px rgba(109, 40, 217, 0.4);
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(109, 40, 217, 0.6);
        }

        .btn-secondary {
            background: linear-gradient(135deg, var(--color-secondary), #0284c7);
            color: white;
            box-shadow: 0 4px 15px rgba(14, 165, 233, 0.4);
        }

        .btn-success {
            background: linear-gradient(135deg, var(--color-accent-2), #059669);
            color: white;
            box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4);
        }

        .btn-danger {
            background: linear-gradient(135deg, var(--color-accent-3), #dc2626);
            color: white;
            box-shadow: 0 4px 15px rgba(239, 68, 68, 0.4);
        }

        .btn-warning {
            background: linear-gradient(135deg, var(--color-accent), #d97706);
            color: white;
            box-shadow: 0 4px 15px rgba(245, 158, 11, 0.4);
        }

        /* Status Dots */
        .status-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
            vertical-align: middle;
        }

        .status-dot.online {
            background-color: var(--color-success);
            box-shadow: 0 0 0 2px rgba(22, 163, 74, 0.2);
        }

        .status-dot.offline {
            background-color: var(--color-danger);
            box-shadow: 0 0 0 2px rgba(220, 38, 38, 0.2);
        }

        /* Alerts */
        .alert {
            padding: 1rem 1.25rem;
            border-radius: var(--radius-lg);
            margin-bottom: 1.5rem;
            border: 1px solid transparent;
            backdrop-filter: blur(10px);
            font-weight: 500;
            animation: slideIn 0.5s ease;
        }

        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateY(-10px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .alert-success {
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.15), rgba(16, 185, 129, 0.1));
            border-color: rgba(16, 185, 129, 0.3);
            color: #065f46;
        }

        .alert-error {
            background: linear-gradient(135deg, rgba(239, 68, 68, 0.15), rgba(239, 68, 68, 0.1));
            border-color: rgba(239, 68, 68, 0.3);
            color: #7f1d1d;
        }

        .alert-info {
            background: linear-gradient(135deg, rgba(14, 165, 233, 0.15), rgba(14, 165, 233, 0.1));
            border-color: rgba(14, 165, 233, 0.3);
            color: #0c4a6e;
        }

        /* Grid System */
        .grid {
            display: grid;
            gap: 1.5rem;
        }

        .grid-1 {
            grid-template-columns: 1fr;
            # grid-template-columns: repeat(4, 1fr);
        }

        .grid-2 {
            # grid-template-columns: repeat(2, 1fr);
             grid-template-columns: repeat(4, 1fr);
        }

        .grid-3 {
            grid-template-columns: repeat(3, 1fr);
        }

        .grid-4 {
            grid-template-columns: repeat(4, 1fr);
        }

        /* Tables */
        .table-container {
            overflow-x: auto;
            border-radius: var(--radius-lg);
            border: 1px solid var(--color-border);
            background: white;
            box-shadow: var(--shadow-md);
            margin-bottom: 1.5rem;
            -webkit-overflow-scrolling: touch;
        }

        .table {
            width: 100%;
            border-collapse: collapse;
            min-width: 600px;
        }

        .table th {
            # background: linear-gradient(135deg, var(--color-primary-light), var(--color-primary));
            background:#4f4b8b;
            color: white;
            padding: 0.875rem;
            text-align: left;
            font-weight: 600;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .table td {
            padding: 0.875rem;
            border-bottom: 1px solid var(--color-border-light);
            vertical-align: middle;
        }

        .table tr:hover {
            background: rgba(139, 92, 246, 0.05);
        }

        .table tr:last-child td {
            border-bottom: none;
        }

        /* Responsive Design */
        @media (max-width: 1200px) {
            .grid-3 {
                grid-template-columns: repeat(2, 1fr);
            }
            
            .grid-4 {
                grid-template-columns: repeat(3, 1fr);
            }
        }

        @media (max-width: 992px) {
            .grid-3 {
                grid-template-columns: 1fr;
            }
            
            .grid-4 {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        @media (max-width: 768px) {
            .content-header {
                padding: 5rem 1rem 1rem;
            }
            
            .page-title {
                font-size: 1.75rem;
            }
            
            .page-subtitle {
                font-size: 0.95rem;
            }
            
            .card-body {
                padding: 1.25rem;
            }
            
            .card-header {
                padding: 1.25rem;
            }
            
            .card-title {
                font-size: 1.125rem;
            }
            
            .grid-2,
            .grid-3,
            .grid-4 {
                grid-template-columns: 1fr;
            }
            
            .btn {
                padding: 0.75rem 1.25rem;
                font-size: 0.9rem;
            }
            
            .table {
                min-width: 500px;
            }
            
            .mobile-menu-toggle {
                top: 1rem;
                left: 1rem;
            }
        }

        @media (max-width: 576px) {
            .content-header {
                padding-top: 4rem;
            }
            
            .page-title {
                font-size: 1.5rem;
            }
            
            .card-body {
                padding: 1rem;
            }
            
            .form-control {
                padding: 0.875rem 1rem;
            }
            
            .btn {
                width: 100%;
                justify-content: center;
            }
        }

        /* Login Page Specific */
        .login-page {
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important;
        }

        .login-page .main-content {
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1.5rem;
            background: transparent;
            margin-left: 0 !important;
        }

        .login-page .card {
            max-width: 480px;
            width: 100%;
            animation: fadeIn 0.6s ease;
            margin: 0 auto;
        }

        @keyframes fadeIn {
            from {
                opacity: 0;
                transform: translateY(-20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .login-page .sidebar {
            display: none !important;
        }

        .login-page .mobile-menu-toggle {
            display: none !important;
        }

        .login-page .content-header {
            display: none;
        }
        
        /* Transfer page specific responsive fixes */
        .transfer-grid {
            display: grid;
            gap: 1.5rem;
        }
        
        @media (min-width: 769px) {
            .transfer-grid {
                grid-template-columns: repeat(3, 1fr);
            }
        }
        
        @media (max-width: 768px) {
            .transfer-grid {
                grid-template-columns: 1fr;
            }
            
            .transfer-grid .card {
                margin-bottom: 1.5rem;
            }
        }
    </style>
</head>
<body class="{% if not current_user.is_authenticated %}login-page{% endif %}">
    {% if current_user.is_authenticated %}
    
    <div class="sidebar-overlay" id="sidebarOverlay"></div>

    <aside class="sidebar {% if current_user.is_authenticated %}active{% endif %}" id="sidebar">
        <div class="sidebar-header">
            <div class="logo">
                <div class="logo-icon"><i class="fas fa-exchange-alt"></i></div>
                <div class="logo-text">ITops</div>
            </div>
        </div>

        <nav class="nav">
            <a href="{{ url_for('dashboard') }}" class="nav-item {% if active=='dashboard' %}active{% endif %}">
                <i class="fas fa-chart-line"></i><span>Dashboard</span>
            </a>
            <a href="{{ url_for('profile') }}" class="nav-item {% if active=='profile' %}active{% endif %}">
                <i class="fas fa-user"></i><span>Profile</span>
            </a>
            <a href="{{ url_for('manage_servers') }}" class="nav-item {% if active=='server' %}active{% endif %}">
                <i class="fas fa-server"></i><span>Server</span>
            </a>
            <a href="{{ url_for('transfer') }}" class="nav-item {% if active=='transfer' %}active{% endif %}">
                <i class="fas fa-file-export"></i><span>File Transfer</span>
            </a>
        </nav>

        <div class="sidebar-footer">
            <div class="user-info">
                <div class="user-avatar">{{ current_user.username[0].upper() }}</div>
                <div class="user-details">
                    <div class="user-name">{{ current_user.username }}</div>
                    <div class="user-role">{{ current_user.role }}</div>
                </div>
                <a href="{{ url_for('logout') }}" class="logout-btn" title="Logout">
                    <i class="fas fa-sign-out-alt"></i>
                </a>
            </div>
        </div>
    </aside>
    {% endif %}

    <main class="main-content" id="mainContent">
        {% if current_user.is_authenticated %}
        <div class="content-header">
            <h1 class="page-title">{{ page_title }}</h1>
            <p class="page-subtitle">{{ page_subtitle }}</p>
        </div>
        {% endif %}

        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="alert alert-info">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        {{ body|safe }}
    </main>

    <script>
        const mobileMenuToggle = document.getElementById('mobileMenuToggle');
        const sidebar = document.getElementById('sidebar');
        const sidebarOverlay = document.getElementById('sidebarOverlay');

        if (sidebarOverlay) {
            sidebarOverlay.addEventListener('click', () => {
                sidebar.classList.remove('active');
                sidebarOverlay.classList.remove('active');
            });
        }
        if (mobileMenuToggle && sidebar) {
            mobileMenuToggle.addEventListener('click', (e) => {
                e.stopPropagation();
                sidebar.classList.toggle('active');
                if (sidebarOverlay) sidebarOverlay.classList.toggle('active');
            });
        }

        function handleResize() {
            if (!sidebar) return;
            if (window.innerWidth > 768) {
                sidebar.classList.add('active');
                if (sidebarOverlay) sidebarOverlay.classList.remove('active');
            } else {
                sidebar.classList.remove('active');
                if (sidebarOverlay) sidebarOverlay.classList.remove('active');
            }
        }
        window.addEventListener('resize', handleResize);
        handleResize();

        setTimeout(() => {
            document.querySelectorAll('.alert').forEach(a => {
                a.style.transition = 'opacity 0.3s, transform 0.3s';
                a.style.opacity = '0';
                a.style.transform = 'translateY(-10px)';
                setTimeout(()=>a.remove(), 300);
            });
        }, 5000);
    </script>
</body>
</html>
"""

LOGIN_BODY = r"""
<div class="card" style="max-width: 480px; width: 100%;">
  <div class="card-header">
    <h2 class="card-title"><i class="fas fa-lock"></i> Secure Login</h2>
  </div>
  <div class="card-body">
    <form method="post">
      <div class="form-group">
        <label class="form-label">Username</label>
        <input type="text" name="username" class="form-control" placeholder="Enter your username" required>
      </div>
      <div class="form-group">
        <label class="form-label">Password</label>
        <input type="password" name="password" class="form-control" placeholder="Enter your password" required>
      </div>
      <button type="submit" class="btn btn-primary" style="width:100%;">
        <i class="fas fa-sign-in-alt"></i> Sign In
      </button>
    </form>
  </div>
</div>
"""

PROFILE_BODY = r"""
<div class="card">
  <div class="card-header">
    <h2 class="card-title"><i class="fas fa-user-circle"></i> User Profile</h2>
  </div>
  <div class="card-body">
    <div class="form-group">
      <label class="form-label">Username</label>
      <input type="text" value="{{ current_user.username }}" class="form-control" disabled
             style="background: linear-gradient(135deg, rgba(139, 92, 246, 0.1), rgba(139, 92, 246, 0.05));">
    </div>
    <div class="form-group">
      <label class="form-label">Role</label>
      <input type="text" value="{{ current_user.role }}" class="form-control" disabled
             style="background: linear-gradient(135deg, rgba(245, 158, 11, 0.1), rgba(245, 158, 11, 0.05));">
    </div>
  </div>
</div>
"""

# ✅ Dashboard: add a new "Stats" card/column without changing existing UI styles
DASHBOARD_BODY = r"""
<div class="grid grid-2">

  <!-- LEFT STACK (Quick Tips + Stats) -->
  <div style="display:flex; flex-direction:column; gap:1.5rem;">
    <div class="card">
      <div class="card-header">
        <h2 class="card-title"><i class="fas fa-lightbulb"></i> Quick Tips</h2>
      </div>
      <div class="card-body">
        <ul style="padding-left: 1.5rem; margin: 0;">
          <li style="margin-bottom: 0.75rem;">HOST uploads browsing is from:
            <code style="background: rgba(139, 92, 246, 0.1); padding: 0.25rem 0.5rem; border-radius: 0.375rem; font-family: monospace;">{{ host_base }}</code>
          </li>
          <li style="margin-bottom: 0.75rem;">SEND copies to remote:
            <code style="background: rgba(14, 165, 233, 0.1); padding: 0.25rem 0.5rem; border-radius: 0.375rem; font-family: monospace;">/tmp</code>
          </li>
          <li>PEM path must exist on HOST (this server).</li>
        </ul>
      </div>
    </div>

    <!-- ✅ NEW: Stats Card -->
    <div class="card" style="height: 63%;">
      <div class="card-header" >
        <h2 class="card-title"><i class="fas fa-chart-pie"></i> Server & Transfer Stats</h2>
      </div>
      <div class="card-body">

        <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap: 1rem;">
          <div style="padding: 1rem; border: 1px solid var(--color-border); border-radius: var(--radius-lg); background:#fff;">
            <div style="display:flex; align-items:center; gap:0.6rem; font-weight:700;">
              <i class="fas fa-server" style="color: var(--color-secondary);"></i> Overall
            </div>
            <div style="font-size: 1.8rem; font-weight: 800; margin-top:0.25rem;">{{ stats.total_servers }}</div>
          </div>

          <div style="padding: 1rem; border: 1px solid var(--color-border); border-radius: var(--radius-lg); background:#fff;">
            <div style="display:flex; align-items:center; gap:0.6rem; font-weight:700;">
              <i class="fas fa-fire" style="color: var(--color-production);"></i> Production
            </div>
            <div style="font-size: 1.8rem; font-weight: 800; margin-top:0.25rem;">{{ stats.prod_servers }}</div>
          </div>

          <div style="padding: 1rem; border: 1px solid var(--color-border); border-radius: var(--radius-lg); background:#fff;">
            <div style="display:flex; align-items:center; gap:0.6rem; font-weight:700;">
              <i class="fas fa-flask" style="color: var(--color-staging);"></i> Staging
            </div>
            <div style="font-size: 1.8rem; font-weight: 800; margin-top:0.25rem;">{{ stats.stg_servers }}</div>
          </div>
        </div>

        <hr style="margin: 1.25rem 0; border: 1px solid var(--color-border);">

        <div style="display:grid; grid-template-columns: repeat(2, 1fr); gap: 1rem;">
          <div style="padding: 1rem; border: 1px solid rgba(239,68,68,0.25); border-radius: var(--radius-lg); background: rgba(239,68,68,0.06);">
            <div style="display:flex; align-items:center; gap:0.6rem; font-weight:700;">
              <i class="fas fa-upload" style="color: var(--color-production);"></i>
              Total sent to Production
            </div>
            <div style="font-size: 1.4rem; font-weight: 800; margin-top:0.25rem;">{{ stats.prod_bytes_human }}</div>
            <div style="font-size: 0.85rem; color: var(--color-text-light); margin-top:0.25rem;">(completed jobs only)</div>
          </div>

          <div style="padding: 1rem; border: 1px solid rgba(245,158,11,0.25); border-radius: var(--radius-lg); background: rgba(245,158,11,0.08);">
            <div style="display:flex; align-items:center; gap:0.6rem; font-weight:700;">
              <i class="fas fa-upload" style="color: var(--color-staging);"></i>
              Total sent to Staging
            </div>
            <div style="font-size: 1.4rem; font-weight: 800; margin-top:0.25rem;">{{ stats.stg_bytes_human }}</div>
            <div style="font-size: 0.85rem; color: var(--color-text-light); margin-top:0.25rem;">(completed jobs only)</div>
          </div>
        </div>

      </div>
    </div>
  </div>

  <!-- RIGHT: Site Shortcuts (unchanged) -->
  <div class="card" style="width:250%">
    <div class="card-header">
      <h2 class="card-title"><i class="fas fa-link"></i> Site Shortcuts</h2>
    </div>
    <div class="card-body">
      <form method="post" action="{{ url_for('add_site_shortcut') }}">
        <div class="form-group">
          <label class="form-label">Site Name</label>
          <input type="text" name="site_name" class="form-control" placeholder="ITMonitor / TrackAWB / Any Site" required>
        </div>
        <div class="form-group">
          <label class="form-label">Domain / IP</label>
          <input type="text" name="domain_ip" class="form-control" placeholder="https://example.com OR 3.93.115.250:5005" required>
        </div>
        <button type="submit" class="btn btn-primary">
          <i class="fas fa-plus"></i> Add Shortcut
        </button>
      </form>

      <hr style="margin: 1.5rem 0; border: 1px solid var(--color-border);">

      {% if sites|length == 0 %}
        <div style="text-align:center; padding:2rem; color: var(--color-text-light);">
          <i class="fas fa-link" style="font-size:2rem; margin-bottom:1rem; display:block; opacity:0.5;"></i>
          No shortcuts added yet.
        </div>
      {% else %}
        <div style="max-height: 300px; overflow-y:auto;">
          {% for s in sites %}
          <div style="display:flex; align-items:center; justify-content:space-between; padding:1rem; border:1px solid var(--color-border);
                      border-radius: var(--radius-lg); margin-bottom:0.75rem; background:#fff;">
            <div>
              <div style="font-weight:600;">{{ s.site_name }}</div>
              <div style="font-size:0.875rem; color: var(--color-text-light);">
                <a href="{{ s.domain_ip if s.domain_ip.startswith('http') else 'http://' + s.domain_ip }}" target="_blank"
                   style="color: var(--color-primary); text-decoration:none;">
                  {{ s.domain_ip }}
                </a>
              </div>
            </div>
            <form method="post" action="{{ url_for('delete_site_shortcut', site_id=s.id) }}" style="margin:0;">
              <button type="submit" class="btn btn-danger" onclick="return confirm('Delete shortcut?')" style="padding:0.5rem 1rem;">
                <i class="fas fa-trash"></i>
              </button>
            </form>
          </div>
          {% endfor %}
        </div>
      {% endif %}
    </div>
  </div>

</div>
"""

MANAGE_SERVERS_BODY = r"""
<div class="grid grid-2">
  <div class="card" >
    <div class="card-header">
      <h2 class="card-title"><i class="fas fa-plus-circle"></i> Add New Server</h2>
    </div>
    <div class="card-body">
      <form method="post" action="{{ url_for('add_server') }}">
        <div class="form-group">
          <label class="form-label">Server Label</label>
          <input type="text" name="label" class="form-control" required>
        </div>

        <div class="form-group">
          <label class="form-label">Host/IP Address</label>
          <input type="text" name="host" class="form-control" required>
        </div>

        <div class="form-group">
          <label class="form-label">Username</label>
          <input type="text" name="ssh_user" class="form-control" required>
        </div>

        <div class="form-group">
          <label class="form-label">PEM Key (Path on HOST)</label>
          <input type="text" name="pem_path" class="form-control" required>
        </div>

        <div class="form-group">
          <label class="form-label">Environment</label>
          <div class="radio-group">
            <div class="radio-item">
              <input type="radio" name="environment" value="production" checked>
              <div class="radio-item-content">
                <b><i class="fas fa-fire" style="color: var(--color-production); margin-right: 8px;"></i> Production</b>
                <span>Live production environment</span>
              </div>
            </div>
            <div class="radio-item">
              <input type="radio" name="environment" value="staging">
              <div class="radio-item-content">
                <b><i class="fas fa-flask" style="color: var(--color-staging); margin-right: 8px;"></i> Staging</b>
                <span>Testing/staging environment</span>
              </div>
            </div>
            <div class="radio-item">
              <input type="radio" name="environment" value="development">
              <div class="radio-item-content">
                <b><i class="fas fa-file-upload" style="color: var(--color-development); margin-right: 8px;"></i> File Upload</b>
                <span>FTP environment</span>
              </div>
            </div>
          </div>
        </div>

        <div class="form-group">
          <label class="form-label">Online Status</label>
          <div class="radio-group">
            <div class="radio-item">
              <input type="radio" name="online" value="1" checked>
              <div class="radio-item-content"><b>Online</b><span>Server is online and accessible</span></div>
            </div>
            <div class="radio-item">
              <input type="radio" name="online" value="0">
              <div class="radio-item-content"><b>Offline</b><span>Server is offline or unreachable</span></div>
            </div>
          </div>
        </div>

        <div class="form-group">
          <label class="form-label">IT Tag</label>
          <input type="text" name="tag" class="form-control" placeholder="Optional">
        </div>

        <div style="display:flex; gap:1rem; justify-content:flex-end;">
          <button type="reset" class="btn btn-secondary"><i class="fas fa-undo"></i> Reset</button>
          <button type="submit" class="btn btn-primary"><i class="fas fa-plus"></i> Add Server</button>
        </div>
      </form>
    </div>
  </div>

  <div class="card" style="width: 275%;height: 98%;    max-height: 100%;background: bottom;">
    <div class="card-header">
      <h2 class="card-title"><i class="fas fa-server"></i> Server List</h2>
    </div>
    <div class="card-body" style="    overflow-y: scroll;
    max-height: 100%;
    background: bottom;
    height: 100%;">
      {% if servers|length == 0 %}
        <div style="text-align:center; padding:2rem; color: var(--color-text-light);">
          <i class="fas fa-server" style="font-size:2rem; margin-bottom:1rem; display:block; opacity:0.5;"></i>
          No servers added yet.
        </div>
      {% else %}
        <div style="overflow-y:auto;">
          <div style="display:flex; flex-wrap:wrap; gap:1rem;">
            {% for s in servers %}
            <div style="flex: 1 0 calc(50% - 0.5rem); min-width: 300px; border: 2px solid var(--color-border);
                        border-radius: var(--radius-lg); padding: 1.25rem; background: #fff; display:flex; flex-direction:column;">
              <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem;">
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <div class="status-dot {% if s.online %}online{% else %}offline{% endif %}"></div>
                  <div>
                    <div style="font-weight:700; font-size:1.6rem;">
                     
                      {{ s.label }}
                    </div>
                    <div style="font-size:0.875rem; color: var(--color-text-light);">{{ s.host }}</div>
                  </div>
                </div>
                <form method="post" action="{{ url_for('delete_server', server_id=s.id) }}">
                  <button type="submit" class="btn btn-danger" onclick="return confirm('Delete this server?')" style="padding:0.5rem 1rem;">
                    <i class="fas fa-trash"></i>
                  </button>
                </form>
              </div>

              <div style="display:flex; flex-grow:1; gap:1.5rem;">
                <div style="flex:1; font-size:0.875rem;">
                  <div style="margin-bottom:0.75rem;">
                    <span style="color: var(--color-text-light); display:block;">User:</span>
                    <span style="font-weight:600;">{{ s.ssh_user }}</span>
                  </div>
                  <div style="margin-bottom:0.75rem;">
                    <span style="color: var(--color-text-light); display:block;">Env:</span>
                    <span style="font-weight:600;">
                      {% if s.environment == 'production' %}
                        <i class="fas fa-fire" style="color: var(--color-production); margin-right: 6px;"></i>
                      {% elif s.environment == 'staging' %}
                        <i class="fas fa-flask" style="color: var(--color-staging); margin-right: 6px;"></i>
                      {% elif s.environment == 'development' %}
                        <i class="fas fa-file-upload" style="color: var(--color-development); margin-right: 6px;"></i>
                      {% endif %}
                      {{ s.environment }}
                    </span>
                  </div>
                  <div style="margin-bottom:0.75rem;">
                    <span style="color: var(--color-text-light); display:block;">Status:</span>
                    <span style="font-weight:600;">{% if s.online %}Online{% else %}Offline{% endif %}</span>
                  </div>
                </div>

                <div style="flex:1; font-size:0.875rem;">
                  <div style="margin-bottom:0.75rem;">
                    <span style="color: var(--color-text-light); display:block;">Added:</span>
                    <span style="font-weight:600;">{{ (s.created_at or '')[:16].replace('T',' ') }}</span>
                  </div>
                  <div style="margin-bottom:0.75rem;">
                    <span style="color: var(--color-text-light); display:block;">Tag:</span>
                    <span style="font-weight:600;">{{ s.tag if s.tag else '-' }}</span>
                  </div>
                  <div style="margin-bottom:0.75rem;">
                    <span style="color: var(--color-text-light); display:block;">Port:</span>
                    <span style="font-weight:600;">22</span>
                  </div>
                </div>
              </div>

            </div>
            {% endfor %}
          </div>
        </div>
      {% endif %}
    </div>
  </div>
</div>
"""

# ✅ File Transfer: add Compress button with loader
TRANSFER_BODY = r"""
<form method="post" action="{{ url_for('start_transfer') }}">
  <div class="transfer-grid">

    <!-- LEFT: File Browser -->
    <section class="card">
      <div class="card-header">
        <h2 class="card-title"><i class="fas fa-folder-open"></i> Select File to Transfer</h2>
        <div style="font-size: 0.875rem; color: var(--color-text-light);">
          Host: <code style="background: rgba(139, 92, 246, 0.1); padding: 0.25rem 0.5rem; border-radius: 0.375rem; font-family: monospace;">{{ host_base }}</code>
        </div>
      </div>

      <div class="card-body">
        <div style="font-size: 0.875rem; color: var(--color-text-light); margin-bottom: 1rem;">
          Current: <code style="background: rgba(139, 92, 246, 0.1); padding: 0.25rem 0.5rem; border-radius: 0.375rem; font-family: monospace;">{{ current_rel or "/" }}</code>
        </div>

        <div style="display:flex; gap:0.75rem; margin-bottom: 1.5rem; flex-wrap:wrap;">
          <a href="{{ url_for('nav_back') }}" class="btn btn-secondary" title="Back" style="flex:1; min-width:120px;">
            <i class="fas fa-arrow-left"></i> Back
          </a>

          <button type="button" class="btn btn-secondary" onclick="goNext()" title="Next" style="flex:1; min-width:120px;">
            <i class="fas fa-arrow-right"></i> Next
          </button>

          <!-- ✅ NEW: Compress button with loader -->
          <button type="button" class="btn btn-warning" onclick="compressSelected()" title="Compress selected folder" style="flex:1; min-width:140px;" id="compressBtn">
            <i class="fas fa-file-zipper"></i> <span id="compressText">Compress</span>
            <span id="compressLoader" style="display:none;"><i class="fas fa-spinner fa-spin"></i> Processing...</span>
          </button>
        </div>

        <hr style="margin: 1.5rem 0; border: 1px solid var(--color-border);">

        {% if host_items|length == 0 %}
          <div style="text-align: center; padding: 2rem; color: var(--color-text-light);">
            <i class="fas fa-folder-open" style="font-size: 2rem; margin-bottom: 1rem; display: block; opacity: 0.5;"></i>
            No files/folders found.
          </div>
        {% else %}
          <div class="table-container">
            <table class="table">
              <thead>
                <tr>
                  <th style="width: 40px;"></th>
                  <th>Name</th>
                  <th>Type</th>
                </tr>
              </thead>
              <tbody>
                {% for it in host_items %}
                <tr>
                  <td>
                    <input type="radio" name="selected_item" value="{{ it.rel }}" {% if it.rel == selected_item %}checked{% endif %}>
                  </td>
                  <td style="font-family: monospace;">
                    {% if it.is_dir %}
                      {{ it.name }}/
                    {% else %}
                      {{ it.name }}
                    {% endif %}
                  </td>
                  <td>{{ "DIR" if it.is_dir else "FILE" }}</td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        {% endif %}

        <hr style="margin: 1.5rem 0; border: 1px solid var(--color-border);">

        <div style="font-size: 0.875rem; color: var(--color-text-light);">
          Selected item:
          <code style="background: rgba(139, 92, 246, 0.1); padding: 0.25rem 0.5rem; border-radius: 0.375rem; font-family: monospace;"
                id="selText">{{ selected_item or "-" }}</code>
        </div>

        <script>
          function selectedRadioValue(){
            const picked = document.querySelector('input[name="selected_item"]:checked');
            return picked ? picked.value : "";
          }

          function goNext(){
            const target = selectedRadioValue();
            if(!target){
              alert("Please select a folder (DIR) to go Next.");
              return;
            }
            window.location = "/transfer/next?target=" + encodeURIComponent(target);
          }

          function compressSelected(){
            const target = selectedRadioValue();
            if(!target){
              alert("Please select a folder (DIR) to compress.");
              return;
            }
            
            // Show loader
            const btn = document.getElementById('compressBtn');
            const text = document.getElementById('compressText');
            const loader = document.getElementById('compressLoader');
            
            btn.disabled = true;
            text.style.display = 'none';
            loader.style.display = 'inline';
            
            // go to server-side compressor
            window.location = "/transfer/compress?target=" + encodeURIComponent(target);
          }

          document.querySelectorAll('input[name="selected_item"]').forEach(r=>{
            r.addEventListener("change", ()=>{
              document.getElementById("selText").innerText = r.value;
              const url = new URL(window.location.href);
              url.searchParams.set("item", r.value);
              window.history.replaceState({}, "", url.toString());
            });
          });

          // Reset button state on page load (in case of page refresh)
          window.addEventListener('load', function() {
            const btn = document.getElementById('compressBtn');
            if(btn) {
              btn.disabled = false;
              document.getElementById('compressText').style.display = 'inline';
              document.getElementById('compressLoader').style.display = 'none';
            }
          });
        </script>
      </div>
    </section>

    <!-- MIDDLE: Server Selection -->
    <section class="card">
      <div class="card-header">
        <h2 class="card-title"><i class="fas fa-globe"></i> Select Environment</h2>
      </div>

      <div class="card-body">
        <div class="form-group">
          <label class="form-label">Environment</label>
          <div class="radio-group">
            <div class="radio-item">
              <input type="radio" name="env" value="staging" {% if env=='staging' %}checked{% endif %}
                     onchange="window.location='{{ url_for('transfer') }}?env=staging'">
              <div class="radio-item-content">
                <b><i class="fas fa-flask" style="color: var(--color-staging); margin-right: 8px;"></i> Staging</b>
                <span>Staging environment</span>
              </div>
            </div>
            <div class="radio-item">
              <input type="radio" name="env" value="production" {% if env=='production' %}checked{% endif %}
                     onchange="window.location='{{ url_for('transfer') }}?env=production'">
              <div class="radio-item-content">
                <b><i class="fas fa-fire" style="color: var(--color-production); margin-right: 8px;"></i> Production</b>
                <span>Production environment</span>
              </div>
            </div>
            <div class="radio-item">
              <input type="radio" name="env" value="development" {% if env=='development' %}checked{% endif %}
                     onchange="window.location='{{ url_for('transfer') }}?env=development'">
              <div class="radio-item-content">
                <b><i class="fas fa-file-upload" style="color: var(--color-development); margin-right: 8px;"></i> File Upload</b>
                <span>Development environment</span>
              </div>
            </div>
          </div>
        </div>

        <h3 style="margin: 0 0 1rem 0; font-size: 1.125rem; font-weight: 600; color: var(--color-text-dark);">
          <i class="fas fa-server"></i> Server List
        </h3>

        {% if servers|length == 0 %}
          <div style="text-align:center; padding:2rem; color: var(--color-text-light);">
            <i class="fas fa-server" style="font-size:2rem; margin-bottom:1rem; display:block; opacity:0.5;"></i>
            No servers added for this environment.
          </div>
        {% else %}
          <div class="table-container">
            <table class="table">
              <thead>
                <tr>
                  <th style="width: 40px;"></th>
                  <th>Label</th>
                  <th>Host</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {% for s in servers %}
                <tr>
                  <td>
                    <input type="radio" name="selected_server_id" value="{{ s.id }}" {% if s.id == selected_server_id %}checked{% endif %}>
                  </td>
                  <td>
                    {% if s.environment == 'production' %}
                      <i class="fas fa-fire" style="color: var(--color-production); margin-right: 6px;"></i>
                    {% elif s.environment == 'staging' %}
                      <i class="fas fa-flask" style="color: var(--color-staging); margin-right: 6px;"></i>
                    {% elif s.environment == 'development' %}
                      <i class="fas fa-file-upload" style="color: var(--color-development); margin-right: 6px;"></i>
                    {% endif %}
                    {{ s.label }}
                  </td>
                  <td style="font-family: monospace;">{{ s.host }}</td>
                  <td>
                    {% if s.online %}
                      <div class="status-dot online"></div> Online
                    {% else %}
                      <div class="status-dot offline"></div> Offline
                    {% endif %}
                  </td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        {% endif %}

        <div class="form-group">
          <label class="form-label">Action</label>
          <select name="action" class="form-control" required>
            <option value="send" selected>SEND (Host → Remote /tmp)</option>
          </select>
        </div>
      </div>
    </section>

    <!-- RIGHT: Transfer Control -->
    <section class="card">
      <div class="card-header">
        <h2 class="card-title"><i class="fas fa-rocket"></i> Transfer Control</h2>
      </div>

      <div class="card-body">
        <div style="margin-bottom:1.5rem;">
          <div style="font-size:0.875rem; color: var(--color-text-light); margin-bottom:0.5rem;">User:</div>
          <div style="font-weight:600; color: var(--color-primary);">{{ current_user.username }}</div>
        </div>

        <div style="margin-bottom:1.5rem;">
          <div style="font-size:0.875rem; color: var(--color-text-light); margin-bottom:0.5rem;">Role:</div>
          <div style="font-weight:600; color: var(--color-accent);">{{ current_user.role }}</div>
        </div>

        <hr style="margin:1.5rem 0; border:1px solid var(--color-border);">

        <div style="margin-bottom:1.5rem;">
          <div style="font-size:0.875rem; color: var(--color-text-light); margin-bottom:0.5rem;">File/Folder:</div>
          <div style="font-weight:600; font-family: monospace;">{{ selected_item or '-' }}</div>
        </div>

        <div style="margin-bottom:1.5rem;">
          <div style="font-size:0.875rem; color: var(--color-text-light); margin-bottom:0.5rem;">Server:</div>
          <div style="font-weight:600;">{{ selected_server_label or '-' }}</div>
        </div>

        <div style="margin-bottom:1.5rem;">
          <div style="font-size:0.875rem; color: var(--color-text-light); margin-bottom:0.5rem;">Environment:</div>
          <div style="font-weight:600;">
            {% if env == 'production' %}
              <i class="fas fa-fire" style="color: var(--color-production); margin-right: 6px;"></i>
            {% elif env == 'staging' %}
              <i class="fas fa-flask" style="color: var(--color-staging); margin-right: 6px;"></i>
            {% elif env == 'development' %}
              <i class="fas fa-file-upload" style="color: var(--color-development); margin-right: 6px;"></i>
            {% endif %}
            {{ env }}
          </div>
        </div>

        <hr style="margin:1.5rem 0; border:1px solid var(--color-border);">

        <div id="jobBox" style="padding: 1rem; background: rgba(14, 165, 233, 0.1); border-radius: var(--radius-lg);
             border: 1px solid rgba(14, 165, 233, 0.3); margin-bottom: 1.5rem;">
          <div style="font-size: 0.875rem; color: var(--color-text-light);">No job running.</div>
        </div>

        <hr style="margin:1.5rem 0; border:1px solid var(--color-border);">

        <div>
          <button type="submit" class="btn btn-primary" style="width: 40%; padding: 1rem; margin-left: 1%">
            <i class="fas fa-paper-plane"></i> Click to Transfer File
          </button>

          <button type="button" class="btn btn-secondary" onclick="refreshStatus()" style="width: 40%; padding: 1rem; margin-left: 18%;">
            <i class="fas fa-sync-alt"></i> Refresh
          </button>
        </div>

        <div id="resultBox" style="margin-top: 1rem;"></div>

        <script>
          const jobId = "{{ job_id or '' }}";

          async function refreshStatus(){
            if(!jobId){
              document.getElementById("jobBox").innerHTML =
                '<div style="font-size:0.875rem; color: var(--color-text-light);">No job running.</div>';
              document.getElementById("resultBox").innerHTML = "";
              return;
            }
            const r = await fetch("/api/job/" + jobId);
            const j = await r.json();

            let statusColor = 'var(--color-info)';
            if(j.status === "completed") statusColor = 'var(--color-success)';
            else if(j.status === "failed") statusColor = 'var(--color-danger)';
            else if(j.status === "running") statusColor = 'var(--color-accent)';

            document.getElementById("jobBox").innerHTML = `
              <div style="margin-bottom:0.5rem;">
                <span style="font-size:0.875rem; color: var(--color-text-light);">Status:</span>
                <span style="font-weight:600; color:${statusColor}; margin-left:0.5rem;">${j.status.toUpperCase()}</span>
              </div>
              <div style="margin-bottom:0.5rem;">
                <span style="font-size:0.875rem; color: var(--color-text-light);">Message:</span>
                <span style="font-weight:600; margin-left:0.5rem;">${j.message || "-"}</span>
              </div>
              <div style="margin-bottom:0.5rem;">
                <span style="font-size:0.875rem; color: var(--color-text-light);">Start:</span>
                <span style="font-weight:600; margin-left:0.5rem;">${j.start_time || "-"}</span>
              </div>
              <div>
                <span style="font-size:0.875rem; color: var(--color-text-light);">End:</span>
                <span style="font-weight:600; margin-left:0.5rem;">${j.end_time || "-"}</span>
              </div>
            `;

            if(j.status === "completed"){
              document.getElementById("resultBox").innerHTML = `
                <div class="alert alert-success">
                  <i class="fas fa-check-circle"></i> ✅ Successfully Transfer Completed
                </div>`;
            } else if(j.status === "failed"){
              document.getElementById("resultBox").innerHTML = `
                <div class="alert alert-error">
                  <i class="fas fa-times-circle"></i> ❌ Transfer Failed: ${j.message || ''}
                </div>`;
            } else {
              document.getElementById("resultBox").innerHTML = "";
            }
          }

          if(jobId){
            refreshStatus();
            setInterval(refreshStatus, 2000);
          }
        </script>
      </div>
    </section>

  </div>
</form>
"""

def render_page(*, title, page_title, page_subtitle, active, body, **ctx):
    return render_template_string(
        BASE_HTML,
        title=title,
        page_title=page_title,
        page_subtitle=page_subtitle,
        active=active,
        body=render_template_string(body, **ctx),
        **ctx
    )

# =========================
# Routes
# =========================
@app.route("/")
def root():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""

        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=?", (u,))
        row = cur.fetchone()
        conn.close()

        if not row or not check_password_hash(row["password_hash"], p):
            flash("Invalid username or password.")
            return render_page(
                title="Login",
                page_title="Login",
                page_subtitle="Sign in to continue",
                active="",
                body=LOGIN_BODY,
            )

        user = User(row)
        login_user(user)

        # Update last login
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("UPDATE users SET last_login=? WHERE id=?",
                        (datetime.utcnow().isoformat(), user.id))
            conn.commit()
            conn.close()
        except Exception:
            pass

        return redirect(url_for("dashboard"))

    return render_page(
        title="Login",
        page_title="Login",
        page_subtitle="Sign in to continue",
        active="",
        body=LOGIN_BODY,
    )

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully", "success")
    return redirect(url_for("login"))

@app.route("/profile")
@login_required
def profile():
    return render_page(
        title="Profile",
        page_title="Profile",
        page_subtitle="Your account details",
        active="profile",
        body=PROFILE_BODY,
    )

@app.route("/dashboard")
@login_required
def dashboard():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM site_shortcuts ORDER BY id DESC")
    sites = cur.fetchall()

    # ✅ server counts
    cur.execute("SELECT COUNT(*) AS c FROM servers")
    total_servers = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM servers WHERE environment='production'")
    prod_servers = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM servers WHERE environment='staging'")
    stg_servers = cur.fetchone()["c"]

    # ✅ transferred bytes (completed only)
    cur.execute("""
        SELECT COALESCE(SUM(zipped_bytes),0) AS b
        FROM transfers
        WHERE environment='production' AND status='completed'
    """)
    prod_bytes = cur.fetchone()["b"]

    cur.execute("""
        SELECT COALESCE(SUM(zipped_bytes),0) AS b
        FROM transfers
        WHERE environment='staging' AND status='completed'
    """)
    stg_bytes = cur.fetchone()["b"]

    conn.close()

    stats = {
        "total_servers": total_servers,
        "prod_servers": prod_servers,
        "stg_servers": stg_servers,
        "prod_bytes": prod_bytes,
        "stg_bytes": stg_bytes,
        "prod_bytes_human": format_bytes(prod_bytes),
        "stg_bytes_human": format_bytes(stg_bytes),
    }

    return render_page(
        title="Dashboard",
        page_title="Dashboard",
        page_subtitle="Overview and shortcuts",
        active="dashboard",
        body=DASHBOARD_BODY,
        host_base=HOST_UPLOADS,
        sites=sites,
        stats=stats,
    )

@app.route("/dashboard/site/add", methods=["POST"])
@login_required
def add_site_shortcut():
    site_name = (request.form.get("site_name") or "").strip()
    domain_ip = (request.form.get("domain_ip") or "").strip()
    if not site_name or not domain_ip:
        flash("Please enter site-name and Domain/IP.")
        return redirect(url_for("dashboard"))

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO site_shortcuts (site_name, domain_ip, created_at) VALUES (?,?,?)",
        (site_name, domain_ip, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    flash("✅ Shortcut added.")
    return redirect(url_for("dashboard"))

@app.route("/dashboard/site/delete/<int:site_id>", methods=["POST"])
@login_required
def delete_site_shortcut(site_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM site_shortcuts WHERE id=?", (site_id,))
    conn.commit()
    conn.close()
    flash("Shortcut deleted.")
    return redirect(url_for("dashboard"))

@app.route("/manage-servers")
@login_required
def manage_servers():
    servers = get_servers()
    return render_page(
        title="Srv Management",
        page_title="Server Management",
        page_subtitle="Add and manage your servers",
        active="server",
        body=MANAGE_SERVERS_BODY,
        servers=servers,
    )

@app.route("/manage-servers/add", methods=["POST"])
@login_required
def add_server():
    try:
        label = (request.form.get("label") or "").strip()
        host = (request.form.get("host") or "").strip()
        ssh_user = (request.form.get("ssh_user") or "").strip()
        pem_path = (request.form.get("pem_path") or "").strip()
        environment = normalize_env(request.form.get("environment"))
        online = 1 if (request.form.get("online") == "1") else 0
        tag = (request.form.get("tag") or "").strip()

        if not label or not host or not ssh_user or not pem_path:
            flash("All fields are required.")
            return redirect(url_for("manage_servers"))

        if not is_allowed_pem_path(pem_path):
            flash(f"PEM not found on host: {pem_path}")
            return redirect(url_for("manage_servers"))

        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO servers (label, host, ssh_user, pem_path, environment, online, tag, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (label, host, ssh_user, pem_path, environment, online, tag, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()

        flash("✅ Server added.")
        return redirect(url_for("manage_servers"))
    except Exception as e:
        flash(f"❌ Failed to add server: {e}")
        return redirect(url_for("manage_servers"))

@app.route("/manage-servers/delete/<int:server_id>", methods=["POST"])
@login_required
def delete_server(server_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM servers WHERE id=?", (server_id,))
    conn.commit()
    conn.close()
    flash("Server deleted.")
    return redirect(url_for("manage_servers"))

# -------------------------
# Transfer navigation history
# -------------------------
def get_hist():
    h = session.get("dir_hist")
    if not isinstance(h, list):
        h = []
    return h

def push_hist(rel_dir):
    h = get_hist()
    if not h or h[-1] != rel_dir:
        h.append(rel_dir)
    session["dir_hist"] = h

@app.route("/transfer/back")
@login_required
def nav_back():
    env = normalize_env(session.get("env") or "staging")
    h = get_hist()
    if len(h) >= 2:
        h.pop()
        prev = h[-1]
        session["dir_hist"] = h
        session["dir"] = prev
    else:
        session["dir"] = ""
        session["dir_hist"] = [""]

    return redirect(url_for("transfer", env=env))

@app.route("/transfer/next")
@login_required
def nav_next():
    env = normalize_env(session.get("env") or "staging")
    target = (request.args.get("target") or "").strip().lstrip("/")
    if not target:
        flash("Please select a folder (DIR) to go Next.")
        return redirect(url_for("transfer", env=env))

    try:
        abs_target = safe_join(HOST_UPLOADS, target)
        if not os.path.isdir(abs_target):
            flash("Next works only for folders (DIR).")
            return redirect(url_for("transfer", env=env, item=target))
    except Exception:
        flash("Invalid folder selection.")
        return redirect(url_for("transfer", env=env))

    session["dir"] = target
    push_hist(target)
    return redirect(url_for("transfer", env=env, dir=target, item=""))

# ✅ NEW: Compress endpoint
@app.route("/transfer/compress")
@login_required
def compress():
    env = normalize_env(session.get("env") or "staging")
    target = (request.args.get("target") or "").strip().lstrip("/")
    if not target:
        flash("Please select a folder (DIR) to compress.")
        return redirect(url_for("transfer", env=env))

    try:
        abs_target = safe_join(HOST_UPLOADS, target)
        if not os.path.isdir(abs_target):
            flash("Compress works only for folders (DIR).")
            return redirect(url_for("transfer", env=env, item=target))

        # Create zip in the SAME parent directory (current path)
        parent_dir = os.path.dirname(abs_target.rstrip("/"))
        folder_name = os.path.basename(abs_target.rstrip("/"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_name = f"{folder_name}_{ts}.zip"
        zip_abs = os.path.join(parent_dir, zip_name)

        # zip contents (folder itself included)
        zip_local_path(abs_target, zip_abs)

        flash(f"✅ Compressed: {zip_name}")
    except Exception as e:
        flash(f"❌ Compress failed: {e}")

    # remain in same directory view
    rel_dir = session.get("dir", "")
    return redirect(url_for("transfer", env=env, dir=rel_dir, item=""))

@app.route("/transfer")
@login_required
def transfer():
    env = normalize_env(request.args.get("env") or session.get("env") or "staging")
    session["env"] = env

    rel_dir = (request.args.get("dir") or session.get("dir") or "").strip()
    session["dir"] = rel_dir

    if "dir_hist" not in session:
        session["dir_hist"] = [rel_dir or ""]

    push_hist(rel_dir or "")

    selected_item = (request.args.get("item") or session.get("selected_item") or "").strip()
    session["selected_item"] = selected_item

    sid = request.args.get("sid") or session.get("selected_server_id")
    selected_server_id = int(sid) if sid and str(sid).isdigit() else None
    if selected_server_id:
        session["selected_server_id"] = selected_server_id

    job_id = request.args.get("job_id")

    host_items, current_rel = list_host_dir(rel_dir)
    servers = get_servers(env=env)

    selected_server_label = None
    if selected_server_id:
        for s in servers:
            if s["id"] == selected_server_id:
                selected_server_label = f"{s['label']} ({s['host']})"
                break

    return render_page(
        title="File Transfer",
        page_title="File Transfer",
        page_subtitle="Select file/folder and send via SSH",
        active="transfer",
        body=TRANSFER_BODY,
        host_base=HOST_UPLOADS,
        host_items=host_items,
        current_rel=current_rel,
        selected_item=selected_item,
        env=env,
        servers=servers,
        selected_server_id=selected_server_id,
        selected_server_label=selected_server_label,
        job_id=job_id,
    )

@app.route("/transfer/start", methods=["POST"])
@login_required
def start_transfer():
    env = normalize_env(session.get("env") or "staging")

    selected_item = (request.form.get("selected_item") or "").strip()
    selected_server_id = (request.form.get("selected_server_id") or "").strip()

    if not selected_item:
        flash("❌ Please select a file/folder (dot selection).")
        return redirect(url_for("transfer", env=env))

    if not selected_server_id.isdigit():
        flash("❌ Please select a server (dot selection).")
        return redirect(url_for("transfer", env=env, item=selected_item))

    selected_server_id = int(selected_server_id)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM servers WHERE id=? AND environment=?", (selected_server_id, env))
    server = cur.fetchone()
    conn.close()

    if not server:
        flash("❌ Server not found for this environment.")
        return redirect(url_for("transfer", env=env, item=selected_item, sid=selected_server_id))

    if server["online"] != 1:
        flash("❌ Selected server is Offline.")
        return redirect(url_for("transfer", env=env, item=selected_item, sid=selected_server_id))

    job_id = f"{int(time.time())}_{os.getpid()}"
    set_job(job_id, status="queued", message="Queued...")

    # ✅ pass env into job (for dashboard transfer size)
    t = threading.Thread(target=run_send_job, args=(job_id, dict(server), selected_item, env), daemon=True)
    t.start()

    flash("✅ SEND started.")

    rel_dir = session.get("dir", "")
    return redirect(url_for(
        "transfer",
        env=env,
        dir=rel_dir,
        item=selected_item,
        sid=selected_server_id,
        job_id=job_id
    ))

@app.route("/api/job/<job_id>")
@login_required
def api_job(job_id):
    with TRANSFER_LOCK:
        j = TRANSFER_STATUS.get(job_id, {"status": "unknown", "message": "No such job"})
    return jsonify(j)

# =========================
# Main
# =========================
if __name__ == "__main__":
    print(f"Starting GTS XFTP on port {APP_PORT}")
    print(f"Host uploads directory: {HOST_UPLOADS}")
    print(f"Database path: {DB_PATH}")
    print(f"Remote destination: {REMOTE_SEND_DEST}")
    print("\n" + "="*50)
    print("🎨 Colorful Professional UI Enabled")
    print("✅ Fixed Next Button Functionality")
    print("📱 Fully Responsive Design")
    print("🔒 Secure Authentication System")
    print("📊 Dashboard with Site Shortcuts")
    print("🖥️ Server Management Interface")
    print("🚀 File Transfer with Progress Tracking")
    print("="*50 + "\n")
    
    app.run(
        host="0.0.0.0",
        port=APP_PORT,
        debug=False,
        threaded=True
    )
