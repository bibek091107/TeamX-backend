import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from utils import auth_middleware


PEM = "-----BEGIN PRIVATE KEY-----\\nunit-test-key-material\\n-----END PRIVATE KEY-----"
SERVICE_EMAIL = "firebase-adminsdk@teamcollab-9b857.iam.gserviceaccount.com"


class FirebaseInitializationTests(unittest.TestCase):
    def setUp(self):
        auth_middleware._firebase_ready = False

    def firebase_modules(self, certificate=None, initialize=None):
        admin = types.ModuleType("firebase_admin")
        admin._apps = {}
        admin.get_app = MagicMock(side_effect=ValueError("default app missing"))
        admin.initialize_app = initialize or MagicMock()
        credentials = types.ModuleType("firebase_admin.credentials")
        credentials.Certificate = certificate or MagicMock(
            return_value=types.SimpleNamespace(project_id="teamcollab-9b857")
        )
        credentials.ApplicationDefault = MagicMock()
        return admin, credentials

    def test_missing_variables_reports_names_and_adc_failure_without_values(self):
        admin, credentials = self.firebase_modules()

        class DefaultCredentialsError(Exception):
            pass

        google_auth = types.ModuleType("google.auth")
        google_auth.default = MagicMock(side_effect=DefaultCredentialsError("no default credentials"))
        google_module = types.ModuleType("google")
        google_module.auth = google_auth
        with patch.dict(sys.modules, {
            "firebase_admin": admin,
            "firebase_admin.credentials": credentials,
            "google": google_module,
            "google.auth": google_auth,
        }), patch.dict("os.environ", {}, clear=True), patch("os.path.isfile", return_value=False):
            self.assertFalse(auth_middleware.init_firebase())

        status = auth_middleware.firebase_status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "adc_unavailable")
        self.assertEqual(status["exception_type"], "DefaultCredentialsError")
        self.assertEqual(set(status["missing_env_vars"]), set(auth_middleware.FIREBASE_ENV_CREDENTIALS))
        self.assertNotIn("unit-test-key-material", str(status))

    def test_partial_environment_configuration_does_not_fall_back_to_adc(self):
        admin, credentials = self.firebase_modules()
        with patch.dict(sys.modules, {
            "firebase_admin": admin,
            "firebase_admin.credentials": credentials,
        }), patch.dict("os.environ", {"FIREBASE_PROJECT_ID": "teamcollab-9b857"}, clear=True), \
             patch("os.path.isfile", return_value=False):
            self.assertFalse(auth_middleware.init_firebase())

        status = auth_middleware.firebase_status()
        self.assertEqual(status["reason"], "incomplete_environment_credentials")
        self.assertEqual(status["source"], "environment_variables")
        self.assertEqual(set(status["missing_env_vars"]), {"FIREBASE_CLIENT_EMAIL", "FIREBASE_PRIVATE_KEY"})
        credentials.ApplicationDefault.assert_not_called()

    def test_malformed_service_account_is_reported_without_logging_secret(self):
        admin, credentials = self.firebase_modules(
            certificate=MagicMock(side_effect=ValueError("bad key payload")),
        )
        with patch.dict(sys.modules, {
            "firebase_admin": admin,
            "firebase_admin.credentials": credentials,
        }), patch.dict("os.environ", {
            "FIREBASE_PROJECT_ID": "teamcollab-9b857",
            "FIREBASE_CLIENT_EMAIL": SERVICE_EMAIL,
            "FIREBASE_PRIVATE_KEY": PEM,
        }, clear=True), patch("os.path.isfile", return_value=False):
            self.assertFalse(auth_middleware.init_firebase())

        status = auth_middleware.firebase_status()
        self.assertEqual(status["reason"], "invalid_service_account_credentials")
        self.assertEqual(status["exception_type"], "ValueError")
        self.assertNotIn("unit-test-key-material", str(status))

    def test_environment_credentials_construct_service_account_and_normalize_escaped_newlines(self):
        captured = {}
        cert = MagicMock(side_effect=lambda value: captured.update(value) or
                         types.SimpleNamespace(project_id=value["project_id"]))
        admin, credentials = self.firebase_modules(certificate=cert)

        def initialize(cred, options):
            admin._apps["[DEFAULT]"] = types.SimpleNamespace(
                project_id=options["projectId"],
                options=options,
                credential=cred,
            )
            return admin._apps["[DEFAULT]"]

        admin.initialize_app = MagicMock(side_effect=initialize)
        with patch.dict(sys.modules, {
            "firebase_admin": admin,
            "firebase_admin.credentials": credentials,
        }), patch.dict("os.environ", {
            "FIREBASE_PROJECT_ID": "teamcollab-9b857",
            "FIREBASE_CLIENT_EMAIL": SERVICE_EMAIL,
            "FIREBASE_PRIVATE_KEY": PEM,
            "FIREBASE_PRIVATE_KEY_ID": "test-key-id",
            "FIREBASE_CLIENT_ID": "test-client-id",
        }, clear=True), patch("os.path.isfile", return_value=False):
            self.assertTrue(auth_middleware.init_firebase())

        self.assertEqual(captured["project_id"], "teamcollab-9b857")
        self.assertEqual(captured["client_email"], SERVICE_EMAIL)
        self.assertEqual(captured["private_key"], PEM.replace("\\n", "\n"))
        self.assertEqual(captured["private_key_id"], "test-key-id")
        self.assertEqual(auth_middleware.firebase_status()["source"], "environment_variables")
        self.assertTrue(all(auth_middleware.firebase_status()["env_vars_present"].values()))
        self.assertTrue(auth_middleware.firebase_status()["ready"])

    def test_repeated_initialization_reuses_default_app(self):
        admin, credentials = self.firebase_modules()
        app = types.SimpleNamespace(
            project_id="teamcollab-9b857",
            options={"projectId": "teamcollab-9b857"},
        )
        admin._apps["[DEFAULT]"] = app
        admin.get_app = MagicMock(return_value=app)
        with patch.dict(sys.modules, {
            "firebase_admin": admin,
            "firebase_admin.credentials": credentials,
        }), patch.dict("os.environ", {"FIREBASE_PROJECT_ID": "teamcollab-9b857"}, clear=True):
            self.assertTrue(auth_middleware.init_firebase())
            self.assertTrue(auth_middleware.init_firebase())

        admin.initialize_app.assert_not_called()
        credentials.Certificate.assert_not_called()
        self.assertEqual(auth_middleware.firebase_status()["source"], "existing_app")


if __name__ == "__main__":
    unittest.main()
