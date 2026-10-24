import unittest

from flask import Flask

from utils.cors_config import configure_cors


class CorsConfigTests(unittest.TestCase):
    def test_login_preflight_allows_production_origin_and_required_headers(self):
        app = Flask(__name__)
        configure_cors(app, "https://preview.example.com")
        app.add_url_rule(
            "/api/auth/firebase-login",
            view_func=lambda: ("", 204),
            methods=["POST"],
        )

        response = app.test_client().options(
            "/api/auth/firebase-login",
            headers={
                "Origin": "https://team-x-frontend.vercel.app",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"),
            "https://team-x-frontend.vercel.app",
        )
        self.assertIn("POST", response.headers.get("Access-Control-Allow-Methods", ""))
        allowed_headers = response.headers.get("Access-Control-Allow-Headers", "").lower()
        self.assertIn("authorization", allowed_headers)
        self.assertIn("content-type", allowed_headers)

    def test_configured_origins_do_not_remove_production_origin(self):
        app = Flask(__name__)

        origins = configure_cors(app, "https://preview.example.com/")

        self.assertIn("https://team-x-frontend.vercel.app", origins)
        self.assertIn("https://preview.example.com", origins)


if __name__ == "__main__":
    unittest.main()
