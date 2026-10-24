import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from utils import auth_middleware


class FirebaseInitializationTests(unittest.TestCase):
    def setUp(self):
        auth_middleware._firebase_ready = False

    def test_missing_credentials_attempt_adc_and_reports_safe_failure(self):
        cred_mod = types.SimpleNamespace(ApplicationDefault=MagicMock(side_effect=RuntimeError("no ADC")))
        firebase_mod = types.ModuleType("firebase_admin")
        firebase_mod._apps = {}
        firebase_mod.initialize_app = MagicMock()
        firebase_mod.get_app = MagicMock()
        cred_module = types.ModuleType("firebase_admin.credentials")
        cred_module.ApplicationDefault = cred_mod.ApplicationDefault
        google_auth = types.ModuleType("google.auth")
        google_auth.default = MagicMock(return_value=(object(), "teamcollab-9b857"))
        google_mod = types.ModuleType("google")
        google_mod.auth = google_auth
        with patch.dict(sys.modules, {"firebase_admin": firebase_mod, "firebase_admin.credentials": cred_module,
                                       "google": google_mod, "google.auth": google_auth}), \
             patch.dict("os.environ", {"FIREBASE_PROJECT_ID": "teamcollab-9b857"}, clear=True):
            self.assertFalse(auth_middleware.init_firebase())
        self.assertFalse(auth_middleware.firebase_status()["ready"])
        self.assertEqual(auth_middleware.firebase_status()["project_id"], "teamcollab-9b857")
        self.assertEqual(auth_middleware.firebase_status()["reason"], "RuntimeError")

    def test_initialization_failure_does_not_mark_admin_ready(self):
        firebase_mod = types.ModuleType("firebase_admin")
        firebase_mod._apps = {}
        firebase_mod.initialize_app = MagicMock(side_effect=RuntimeError("credential rejected"))
        credentials_mod = types.ModuleType("firebase_admin.credentials")
        credentials_mod.Certificate = MagicMock(return_value=types.SimpleNamespace(project_id="teamcollab-9b857"))
        credentials_mod.ApplicationDefault = MagicMock()
        with patch.dict(sys.modules, {"firebase_admin": firebase_mod, "firebase_admin.credentials": credentials_mod}), \
             patch.dict("os.environ", {
                 "FIREBASE_PROJECT_ID": "teamcollab-9b857",
                 "FIREBASE_CLIENT_EMAIL": "firebase-adminsdk@teamcollab-9b857.iam.gserviceaccount.com",
                 "FIREBASE_PRIVATE_KEY": "redacted-test-key",
             }, clear=True):
            self.assertFalse(auth_middleware.init_firebase())
        self.assertFalse(auth_middleware.firebase_status()["ready"])
        self.assertEqual(auth_middleware.firebase_status()["reason"], "RuntimeError")

    def test_rejects_wrong_project_configuration_before_initializing(self):
        firebase_mod = types.ModuleType("firebase_admin")
        firebase_mod._apps = {}
        firebase_mod.initialize_app = MagicMock()
        credentials_mod = types.ModuleType("firebase_admin.credentials")
        with patch.dict(sys.modules, {"firebase_admin": firebase_mod, "firebase_admin.credentials": credentials_mod}), \
             patch.dict("os.environ", {"FIREBASE_PROJECT_ID": "another-project"}, clear=True):
            self.assertFalse(auth_middleware.init_firebase())
        firebase_mod.initialize_app.assert_not_called()
        self.assertEqual(auth_middleware.firebase_status()["reason"], "project_mismatch")

    def test_rejects_service_account_from_another_project(self):
        firebase_mod = types.ModuleType("firebase_admin")
        firebase_mod._apps = {}
        firebase_mod.initialize_app = MagicMock()
        credentials_mod = types.ModuleType("firebase_admin.credentials")
        credentials_mod.Certificate = MagicMock()
        with patch.dict(sys.modules, {"firebase_admin": firebase_mod, "firebase_admin.credentials": credentials_mod}), \
             patch.dict("os.environ", {
                 "FIREBASE_PROJECT_ID": "teamcollab-9b857",
                 "FIREBASE_CLIENT_EMAIL": "admin@different-project.iam.gserviceaccount.com",
                 "FIREBASE_PRIVATE_KEY": "redacted-test-key",
             }, clear=True):
            self.assertFalse(auth_middleware.init_firebase())
        credentials_mod.Certificate.assert_not_called()
        firebase_mod.initialize_app.assert_not_called()
        self.assertEqual(auth_middleware.firebase_status()["reason"], "project_mismatch")


if __name__ == "__main__":
    unittest.main()
