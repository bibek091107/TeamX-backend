"""Firebase token verification and JWT decorators."""

import os
from functools import wraps

from flask import jsonify, g
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
from loguru import logger

EXPECTED_FIREBASE_PROJECT_ID = "teamcollab-9b857"
_firebase_ready = False
_firebase_init_error = "not_initialized"


def firebase_status():
    """Return safe Firebase Admin startup diagnostics (never credentials)."""
    return {"ready": _firebase_ready, "project_id": EXPECTED_FIREBASE_PROJECT_ID,
            "reason": _firebase_init_error}


def init_firebase():
    """Initialize Firebase Admin from a service account, env fields, or ADC.

    FIREBASE_PROJECT_ID is always pinned to the existing production project.
    GOOGLE_APPLICATION_CREDENTIALS can point ADC at a service-account file;
    on Google-hosted runtimes the attached service identity is used directly.
    """
    global _firebase_ready, _firebase_init_error
    _firebase_ready = False
    _firebase_init_error = "not_configured"
    try:
        import firebase_admin
        from firebase_admin import credentials

        expected_project = os.getenv("FIREBASE_PROJECT_ID", EXPECTED_FIREBASE_PROJECT_ID).strip()
        if expected_project != EXPECTED_FIREBASE_PROJECT_ID:
            raise ValueError("FIREBASE_PROJECT_ID does not match the configured application project")

        if firebase_admin._apps:
            app = firebase_admin.get_app()
            actual_project = app.project_id or app.options.get("projectId")
            if actual_project != expected_project:
                raise ValueError("Existing Firebase Admin app targets a different project")
            _firebase_ready = True
            _firebase_init_error = ""
            logger.info("Firebase Admin ready: project_id={} source=existing_app", expected_project)
            return True

        configured_service_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH")
        service_path = configured_service_path or "./firebase-service-account.json"
        if configured_service_path and not os.path.isfile(service_path):
            raise FileNotFoundError("FIREBASE_SERVICE_ACCOUNT_PATH does not exist")
        if os.path.isfile(service_path):
            cred = credentials.Certificate(service_path)
            source = "service_account_file"
        elif all(os.getenv(name) for name in ("FIREBASE_PROJECT_ID", "FIREBASE_PRIVATE_KEY", "FIREBASE_CLIENT_EMAIL")):
            private_key = os.environ["FIREBASE_PRIVATE_KEY"].replace("\\n", "\n")
            client_email = os.environ["FIREBASE_CLIENT_EMAIL"].strip()
            if not client_email.endswith(f"@{expected_project}.iam.gserviceaccount.com"):
                raise ValueError("Firebase service account email does not match the configured project")
            sa = {
                "type": "service_account", "project_id": expected_project,
                "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID", ""),
                "private_key": private_key, "client_email": client_email,
                "client_id": os.getenv("FIREBASE_CLIENT_ID", ""),
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            cred = credentials.Certificate(sa)
            source = "environment_variables"
        else:
            import google.auth
            _, adc_project = google.auth.default()
            if adc_project != expected_project:
                raise ValueError("Application Default Credentials project does not match FIREBASE_PROJECT_ID")
            cred = credentials.ApplicationDefault()
            source = "application_default_credentials"

        credential_project = getattr(cred, "project_id", None)
        if credential_project and credential_project != expected_project:
            raise ValueError("Firebase credential project does not match FIREBASE_PROJECT_ID")

        firebase_admin.initialize_app(cred, {"projectId": expected_project})
        _firebase_ready = True
        _firebase_init_error = ""
        logger.info("Firebase Admin ready: project_id={} source={}", expected_project, source)
        return True
    except Exception as exc:
        # Log a bounded reason and exception class only; SDK exception text can
        # contain request details and must not be copied into client responses.
        reason = str(exc).lower()
        _firebase_init_error = "project_mismatch" if "project" in reason else type(exc).__name__
        logger.error("Firebase Admin initialization failed: reason={} exception_type={}",
                     _firebase_init_error, type(exc).__name__)
        return False


def verify_firebase_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return its signed claims."""
    if not _firebase_ready:
        raise RuntimeError("Firebase Admin is not initialized")
    try:
        from firebase_admin import auth
        decoded = auth.verify_id_token(id_token)
        if decoded.get("aud") != EXPECTED_FIREBASE_PROJECT_ID or decoded.get("iss") != f"https://securetoken.google.com/{EXPECTED_FIREBASE_PROJECT_ID}":
            raise ValueError("Firebase token project mismatch")
        return decoded
    except Exception as exc:
        # Keep details in server logs as a safe Firebase error code/class.
        code = getattr(exc, "code", None)
        logger.warning("Firebase ID token rejected: code={} exception_type={}", code or "unknown", type(exc).__name__)
        raise ValueError("Invalid Firebase ID token") from exc


def require_auth(f):
    """Decorator: require valid JWT."""
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            from models.database import User
            user = User.query.get(user_id)
            if not user or not user.is_active:
                return jsonify({"error": "User not found or inactive"}), 401
            g.current_user = user
            return f(*args, **kwargs)
        except Exception:
            return jsonify({"error": "Unauthorized"}), 401
    return decorated


def require_team_member(f):
    """Decorator: require valid JWT + team membership."""
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            from models.database import User, TeamMember
            user = User.query.get(user_id)
            if not user:
                return jsonify({"error": "Unauthorized"}), 401
            g.current_user = user
            team_id = kwargs.get("team_id")
            if team_id:
                member = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
                if not member:
                    return jsonify({"error": "Not a team member"}), 403
                g.team_member = member
            return f(*args, **kwargs)
        except Exception:
            return jsonify({"error": "Unauthorized"}), 401
    return decorated
