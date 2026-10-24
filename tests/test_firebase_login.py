import unittest
from unittest.mock import patch

from flask import Flask
from flask_jwt_extended import JWTManager

from api.auth.routes import auth_bp
from models.database import User, db


class FirebaseLoginTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            JWT_SECRET_KEY="test-only-secret-long-enough-for-hmac-sha256",
        )
        db.init_app(self.app)
        JWTManager(self.app)
        self.app.register_blueprint(auth_bp, url_prefix="/api/auth")
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    @patch("api.auth.routes.firebase_status", return_value={"ready": True})
    @patch("api.auth.routes.sync_user_teams")
    @patch(
        "api.auth.routes.verify_firebase_token",
        return_value={
            "uid": "verified-firebase-uid",
            "email": "verified@example.com",
            "name": "Verified User",
            "email_verified": False,
        },
    )
    def test_login_uses_verified_claims_not_client_identity(self, _verify, _sync, _status):
        with patch.dict("os.environ", {"FLASK_ENV": "production"}):
            response = self.client.post(
                "/api/auth/firebase-login",
                json={
                    "idToken": "opaque-test-token",
                    "email": "attacker@example.com",
                    "displayName": "Unverified Name",
                    "emailVerified": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["user"]["email"], "verified@example.com")
        self.assertEqual(response.json["user"]["displayName"], "Verified User")
        self.assertFalse(response.json["user"]["emailVerified"])
        with self.app.app_context():
            user = User.query.filter_by(firebase_uid="verified-firebase-uid").one()
            self.assertEqual(user.email, "verified@example.com")

    @patch("api.auth.routes.firebase_status", return_value={"ready": True})
    @patch("api.auth.routes.verify_firebase_token", side_effect=ValueError("invalid token"))
    def test_production_rejects_unverifiable_token(self, _verify, _status):
        with patch.dict("os.environ", {"FLASK_ENV": "production"}):
            response = self.client.post(
                "/api/auth/firebase-login",
                json={"idToken": "invalid", "email": "attacker@example.com"},
            )

        self.assertEqual(response.status_code, 401)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)

    @patch("api.auth.routes.firebase_status", return_value={"ready": True})
    @patch("api.auth.routes.verify_firebase_token", side_effect=ValueError("invalid token"))
    def test_development_rejects_unverifiable_token(self, _verify, _status):
        with patch.dict("os.environ", {"FLASK_ENV": "development"}, clear=True):
            response = self.client.post(
                "/api/auth/firebase-login",
                json={"idToken": "invalid", "email": "attacker@example.com"},
            )

        self.assertEqual(response.status_code, 401)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)

    def test_missing_token_returns_400(self):
        response = self.client.post("/api/auth/firebase-login", json={"email": "ignored@example.com"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "missing_id_token")

    @patch("api.auth.routes.firebase_status", return_value={"ready": False})
    @patch("api.auth.routes.verify_firebase_token", side_effect=RuntimeError("not initialized"))
    def test_missing_credentials_return_503_without_creating_user(self, _verify, _status):
        response = self.client.post("/api/auth/firebase-login", json={"idToken": "opaque"})
        self.assertEqual(response.status_code, 503)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
