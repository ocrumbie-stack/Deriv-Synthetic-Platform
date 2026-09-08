import unittest

from app.config import Settings


class DerivConfigTests(unittest.TestCase):
    def test_deriv_settings_are_present(self):
        settings = Settings()
        self.assertTrue(hasattr(settings, "deriv_app_id"))
        self.assertTrue(hasattr(settings, "deriv_api_token"))
        self.assertEqual(settings.execution_mode, "paper")


if __name__ == "__main__":
    unittest.main()
