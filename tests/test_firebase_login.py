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
    def test_login_uses_verified_claims_not_client_identity(self, _verify, _sync):
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

    @patch("api.auth.routes.verify_firebase_token", side_effect=ValueError("invalid token"))
    def test_production_rejects_unverifiable_token(self, _verify):
        with patch.dict("os.environ", {"FLASK_ENV": "production"}):
            response = self.client.post(
                "/api/auth/firebase-login",
                json={"idToken": "invalid", "email": "attacker@example.com"},
            )

        self.assertEqual(response.status_code, 401)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)

    @patch("api.auth.routes.verify_firebase_token", side_effect=ValueError("invalid token"))
    def test_development_mock_auth_requires_explicit_opt_in(self, _verify):
        with patch.dict("os.environ", {"FLASK_ENV": "development"}, clear=True):
            response = self.client.post(
                "/api/auth/firebase-login",
                json={"idToken": "invalid", "email": "attacker@example.com"},
            )

        self.assertEqual(response.status_code, 401)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)

    @patch("api.auth.routes.verify_firebase_token", side_effect=ValueError("invalid token"))
    @patch("api.auth.routes.sync_user_teams")
    def test_local_mock_auth_requires_explicit_opt_in(self, _sync, _verify):
        with patch.dict(
            "os.environ",
            {"FLASK_ENV": "development", "ALLOW_DEV_AUTH": "true"},
            clear=True,
        ):
            response = self.client.post(
                "/api/auth/firebase-login",
                json={"idToken": "invalid", "email": "local-dev@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            user = User.query.filter_by(email="local-dev@example.com").one()
            self.assertTrue(user.email_verified)


if __name__ == "__main__":
    unittest.main()
