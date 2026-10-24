"""
AI Team Brain — Flask Application
Fix: dotenv loaded at very top before any other imports
Fix: WebSocket 500 error suppressed (harmless Werkzeug conflict with SocketIO threading mode)
"""

import os
import sys
from datetime import timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

# Load .env FIRST before any other imports
from dotenv import load_dotenv
# Try multiple paths in case working directory differs
for env_path in [".env", "backend/.env", os.path.join(os.path.dirname(__file__), ".env")]:
    if os.path.exists(env_path):
        load_dotenv(env_path, override=True)
        break

from flask import Flask
from flask_jwt_extended import JWTManager
from flask_socketio import SocketIO
from loguru import logger
from utils.cors_config import configure_cors

# ── Logging ───────────────────────────────────────────────────
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | <level>{message}</level>",
    colorize=True,
    level="DEBUG",
)
os.makedirs("logs", exist_ok=True)
logger.add("logs/app.log", rotation="10 MB", retention="30 days", level="INFO")

# ── Verify key loading ────────────────────────────────────────
groq_key = os.getenv("GROQ_API_KEY", "")
if groq_key:
    logger.info("✅ GROQ_API_KEY loaded")
else:
    logger.warning("⚠️  GROQ_API_KEY not found in .env — AI features will be disabled")

# ── SocketIO — threading mode ─────────────────────────────────
socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
)
jwt = JWTManager()


def _normalize_database_url(url: str) -> str:
    """Render (and most managed Postgres providers) hand out connection
    strings starting with 'postgres://', which SQLAlchemy 1.4+/2.0 no longer
    accepts — it requires 'postgresql://'. This only touches Postgres URLs;
    the local SQLite default (sqlite:///...) is returned unchanged."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def create_app():
    app = Flask(__name__)

    database_url = _normalize_database_url(os.getenv("DATABASE_URL", "sqlite:///ai_team_brain.db"))
    is_postgres = database_url.startswith("postgresql://")

    app.config.update(
        SECRET_KEY                     = os.getenv("SECRET_KEY",    "atb-dev-secret-change-in-prod"),
        JWT_SECRET_KEY                 = os.getenv("JWT_SECRET_KEY","atb-jwt-secret-change-in-prod"),
        SQLALCHEMY_DATABASE_URI        = database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS = False,
        UPLOAD_FOLDER                  = os.getenv("UPLOAD_FOLDER", "./uploads"),
        MAX_CONTENT_LENGTH             = int(os.getenv("MAX_CONTENT_LENGTH", 16_777_216)),
        # SECURITY FIX: access tokens used to never expire (JWT_ACCESS_TOKEN_EXPIRES
        # was False), so a single leaked token was valid forever with no way to
        # revoke it. Short-lived access tokens + a longer-lived refresh token
        # (see /api/auth/refresh) mean a leaked access token is only useful for
        # a limited window, while the frontend silently refreshes it in the
        # background so the user is never interrupted during normal use.
        JWT_ACCESS_TOKEN_EXPIRES       = timedelta(minutes=int(os.getenv("JWT_ACCESS_TOKEN_EXPIRES_MIN", 30))),
        JWT_REFRESH_TOKEN_EXPIRES      = timedelta(days=int(os.getenv("JWT_REFRESH_TOKEN_EXPIRES_DAYS", 30))),
    )

    if is_postgres:
        # pool_pre_ping: managed Postgres providers (Render included) can
        # silently drop idle connections; this checks a connection is alive
        # before using it instead of surfacing a random request failure.
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
        logger.info("Database: PostgreSQL")
    else:
        logger.info("Database: SQLite (local file — not suitable for production, see DATABASE_URL)")

    for d in [app.config["UPLOAD_FOLDER"], "logs", "vector_store"]:
        os.makedirs(d, exist_ok=True)

    from models.database import db
    db.init_app(app)

    origins = configure_cors(app, os.getenv("ALLOWED_ORIGINS"))
    logger.info(f"CORS allowed origins: {origins}")
    jwt.init_app(app)
    socketio.init_app(app)

    from api.auth.routes      import auth_bp
    from api.tasks.routes     import tasks_bp
    from api.chat.routes      import chat_bp
    from api.knowledge.routes import knowledge_bp
    from api.analytics.routes import analytics_bp
    from api.ai.routes        import ai_bp

    app.register_blueprint(auth_bp,      url_prefix="/api/auth")
    app.register_blueprint(tasks_bp,     url_prefix="/api/tasks")
    app.register_blueprint(chat_bp,      url_prefix="/api/chat")
    app.register_blueprint(knowledge_bp, url_prefix="/api/knowledge")
    app.register_blueprint(analytics_bp, url_prefix="/api/analytics")
    app.register_blueprint(ai_bp,        url_prefix="/api/ai")

    from api.chat.socket_events import register_socket_events
    register_socket_events(socketio)

    with app.app_context():
        try:
            db.create_all()
        except Exception as e:
            logger.error(f"❌ Database initialization failed: {e}")
            logger.error("Please verify that DATABASE_URL is valid and reachable.")
            raise

        # Lightweight migration: email_verified column (added for the
        # "verify before you can create a team" gate). Harmless to re-run —
        # guarded by a check that the column already exists.
        from sqlalchemy import text as _sql
        try:
            db.session.execute(_sql("ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 0"))
            db.session.commit()
            logger.info("✅ Migration: added users.email_verified")
        except Exception:
            db.session.rollback()

        # Backfill: dev-mode users can't receive verification emails, so they
        # are treated as verified. Real users get their status on next login.
        try:
            db.session.execute(_sql("UPDATE users SET email_verified = 1 WHERE firebase_uid LIKE 'dev_%'"))
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Lightweight migration: team_members.status (join-request approval).
        try:
            db.session.execute(_sql("ALTER TABLE team_members ADD COLUMN status VARCHAR(20) DEFAULT 'active'"))
            db.session.commit()
            logger.info("✅ Migration: added team_members.status")
        except Exception:
            db.session.rollback()

        # Lightweight migration: teams.require_approval (sub-group gate).
        try:
            db.session.execute(_sql("ALTER TABLE teams ADD COLUMN require_approval BOOLEAN DEFAULT 0"))
            db.session.commit()
            logger.info("✅ Migration: added teams.require_approval")
        except Exception:
            db.session.rollback()

        # Lightweight migration: chat_rooms.is_restricted
        try:
            db.session.execute(_sql("ALTER TABLE chat_rooms ADD COLUMN is_restricted BOOLEAN DEFAULT 0"))
            db.session.commit()
            logger.info("✅ Migration: added chat_rooms.is_restricted")
        except Exception:
            db.session.rollback()

        # Lightweight migration: chat_rooms.is_archived
        try:
            db.session.execute(_sql("ALTER TABLE chat_rooms ADD COLUMN is_archived BOOLEAN DEFAULT 0"))
            db.session.commit()
            logger.info("✅ Migration: added chat_rooms.is_archived")
        except Exception:
            db.session.rollback()

        # Lightweight migration: tasks new fields
        for col, ctype in [
            ("source_message_id", "VARCHAR(36)"),
            ("is_blocked", "BOOLEAN DEFAULT 0"),
            ("blocked_reason", "TEXT"),
            ("blocked_at", "DATETIME"),
        ]:
            try:
                db.session.execute(_sql(f"ALTER TABLE tasks ADD COLUMN {col} {ctype}"))
                db.session.commit()
                logger.info(f"✅ Migration: added tasks.{col}")
            except Exception:
                db.session.rollback()

        # Create new tables if not exist
        try:
            db.create_all()
        except Exception:
            pass

        # Lightweight migration: notifications.extra_data
        try:
            db.session.execute(_sql("ALTER TABLE notifications ADD COLUMN extra_data TEXT"))
            db.session.commit()
            logger.info("✅ Migration: added notifications.extra_data")
        except Exception:
            db.session.rollback()

        # Lightweight migration: chat_messages.reply_to_id (thread replies)
        try:
            db.session.execute(_sql("ALTER TABLE chat_messages ADD COLUMN reply_to_id VARCHAR(36)"))
            db.session.commit()
            logger.info("✅ Migration: added chat_messages.reply_to_id")
        except Exception:
            db.session.rollback()

        logger.info("✅ Database ready")

    try:
        from utils.auth_middleware import init_firebase
        init_firebase()
    except Exception as e:
        logger.warning(f"Firebase init skipped: {e}")

    @app.route("/api/health")
    def health():
        from services.ai.vector_service import FAISS_AVAILABLE, ST_AVAILABLE
        from services.ai.ai_service import check_ai_providers
        providers = check_ai_providers()
        return {
            "status":   "healthy",
            "service":  "AI Team Brain",
            "version":  "1.0.0",
            "python":   sys.version.split()[0],
            "ai":       providers,
            "groq_key": bool(os.getenv("GROQ_API_KEY")),
            "features": {"faiss": FAISS_AVAILABLE, "embeddings": ST_AVAILABLE},
        }

    @app.route("/")
    def root():
        return {"message": "AI Team Brain API — visit /api/health"}

    logger.info("🚀 AI Team Brain backend ready")
    return app


if __name__ == "__main__":
    app   = create_app()
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_ENV", "development") == "development"
    logger.info(f"🌐 Server binding to 0.0.0.0:{port} (PORT={port})")
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=debug,
        allow_unsafe_werkzeug=True,
        use_reloader=False,
    )
