from flask_cors import CORS


PRODUCTION_FRONTEND_ORIGIN = "https://team-x-frontend.vercel.app"
DEFAULT_DEVELOPMENT_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
)


def configure_cors(app, configured_origins=None):
    origins = [PRODUCTION_FRONTEND_ORIGIN]
    configured = configured_origins.split(",") if configured_origins else DEFAULT_DEVELOPMENT_ORIGINS
    origins.extend(origin.strip().rstrip("/") for origin in configured if origin.strip())
    origins = list(dict.fromkeys(origins))

    CORS(
        app,
        origins=origins,
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    return origins
