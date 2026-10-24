"""Firebase token verification and JWT decorators."""

import os
from functools import wraps

from flask import jsonify, g
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
from loguru import logger

EXPECTED_FIREBASE_PROJECT_ID = "teamcollab-9b857"
FIREBASE_ENV_CREDENTIALS = (
    "FIREBASE_PROJECT_ID",
    "FIREBASE_CLIENT_EMAIL",
    "FIREBASE_PRIVATE_KEY",
)
_firebase_ready = False
_firebase_init_error = "not_initialized"
_firebase_source = "none"
_firebase_exception_type = ""
_firebase_env_presence = {name: False for name in FIREBASE_ENV_CREDENTIALS}
_firebase_missing_env = list(FIREBASE_ENV_CREDENTIALS)


def firebase_status():
    """Return safe Firebase Admin startup diagnostics (never credentials)."""
    return {
        "ready": _firebase_ready,
        "project_id": EXPECTED_FIREBASE_PROJECT_ID,
        "source": _firebase_source,
        "reason": _firebase_init_error,
        "exception_type": _firebase_exception_type,
        "env_vars_present": dict(_firebase_env_presence),
        "missing_env_vars": list(_firebase_missing_env),
    }


def init_firebase():
    """Initialize Firebase Admin from a service account, env fields, or ADC.

    FIREBASE_PROJECT_ID is always pinned to the existing production project.
    GOOGLE_APPLICATION_CREDENTIALS can point ADC at a service-account file;
    on Google-hosted runtimes the attached service identity is used directly.
    """
    global _firebase_ready, _firebase_init_error, _firebase_source, _firebase_exception_type
    global _firebase_env_presence, _firebase_missing_env
    _firebase_ready = False
    _firebase_init_error = "not_configured"
    _firebase_source = "none"
    _firebase_exception_type = ""
    _firebase_env_presence = {
        name: bool(os.getenv(name, "").strip())
        for name in FIREBASE_ENV_CREDENTIALS
    }
    _firebase_missing_env = [name for name, present in _firebase_env_presence.items() if not present]
    source = "configuration"
    failure_reason = None
    try:
        import firebase_admin
        from firebase_admin import credentials

        expected_project = os.getenv("FIREBASE_PROJECT_ID", EXPECTED_FIREBASE_PROJECT_ID).strip()
        if expected_project != EXPECTED_FIREBASE_PROJECT_ID:
            failure_reason = "project_mismatch"
            raise ValueError("FIREBASE_PROJECT_ID does not match the configured application project")

        if firebase_admin._apps:
            try:
                app = firebase_admin.get_app()
            except ValueError:
                # A named app may exist without the default app. Keep searching
                # for the default app instead of treating that as initialized.
                app = None
            if app is not None:
                actual_project = app.project_id or app.options.get("projectId")
                existing_credential_project = getattr(getattr(app, "credential", None), "project_id", None)
                if actual_project != expected_project or (
                    existing_credential_project and existing_credential_project != expected_project
                ):
                    failure_reason = "project_mismatch"
                    raise ValueError("Existing Firebase Admin app targets a different project")
                _firebase_ready = True
                _firebase_init_error = ""
                _firebase_source = "existing_app"
                logger.info("Firebase Admin ready: project_id={} source=existing_app", expected_project)
                return True

        configured_service_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH")
        service_path = configured_service_path or "./firebase-service-account.json"
        if configured_service_path and not os.path.isfile(service_path):
            source = "service_account_file"
            failure_reason = "service_account_file_not_found"
            raise FileNotFoundError("FIREBASE_SERVICE_ACCOUNT_PATH does not exist")
        if os.path.isfile(service_path):
            source = "service_account_file"
            cred = credentials.Certificate(service_path)
        elif any(_firebase_env_presence.values()) and not all(_firebase_env_presence.values()):
            source = "environment_variables"
            failure_reason = "incomplete_environment_credentials"
            raise ValueError("Firebase Admin environment credential set is incomplete")
        elif all(_firebase_env_presence.values()):
            source = "environment_variables"
            private_key = os.environ["FIREBASE_PRIVATE_KEY"].replace("\\n", "\n").strip()
            client_email = os.environ["FIREBASE_CLIENT_EMAIL"].strip()
            if not private_key.startswith("-----BEGIN PRIVATE KEY-----") or not private_key.endswith("-----END PRIVATE KEY-----"):
                failure_reason = "malformed_private_key"
                raise ValueError("Firebase private key is not valid PEM")
            if not client_email.endswith(f"@{expected_project}.iam.gserviceaccount.com"):
                failure_reason = "project_mismatch"
                raise ValueError("Firebase service account email does not match the configured project")
            sa = {
                "type": "service_account", "project_id": expected_project,
                "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID", ""),
                "private_key": private_key, "client_email": client_email,
                "client_id": os.getenv("FIREBASE_CLIENT_ID", ""),
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            cred = credentials.Certificate(sa)
        else:
            source = "application_default_credentials"
            import google.auth
            _, adc_project = google.auth.default()
            if adc_project != expected_project:
                failure_reason = "project_mismatch"
                raise ValueError("Application Default Credentials project does not match FIREBASE_PROJECT_ID")
            cred = credentials.ApplicationDefault()

        credential_project = getattr(cred, "project_id", None)
        if credential_project and credential_project != expected_project:
            failure_reason = "project_mismatch"
            raise ValueError("Firebase credential project does not match FIREBASE_PROJECT_ID")

        firebase_admin.initialize_app(cred, {"projectId": expected_project})
        _firebase_ready = True
        _firebase_init_error = ""
        _firebase_source = source
        _firebase_exception_type = ""
        logger.info("Firebase Admin ready: project_id={} source={}", expected_project, source)
        return True
    except Exception as exc:
        # Never log SDK exception text: it can contain credential or request
        # details. Variable names/presence and exception class are sufficient.
        if failure_reason:
            reason = failure_reason
        elif source in ("environment_variables", "service_account_file") and isinstance(exc, ValueError):
            reason = "invalid_service_account_credentials"
        elif source == "application_default_credentials":
            reason = "adc_unavailable"
        elif source in ("environment_variables", "service_account_file"):
            reason = "service_account_initialization_failed"
        else:
            reason = "initialization_failed"
        _firebase_init_error = reason
        _firebase_source = source
        _firebase_exception_type = type(exc).__name__
        missing = ",".join(_firebase_missing_env) or "none"
        logger.error(
            "Firebase Admin initialization failed: source={} reason={} exception_type={} missing_env_vars={}",
            source, reason, type(exc).__name__, missing,
        )
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
