import unittest

from evalve_backend.settings import Settings, SettingsError

ENV = {
    "EVALVE_KITARU_URL": "http://127.0.0.1:8000/",
    "EVALVE_DB_HOST": "db.internal",
    "EVALVE_DB_USER": "evalve_admin",
    "EVALVE_DB_PWD": "database-password-value",
}


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        settings = Settings.from_environment(ENV)
        self.assertEqual(settings.kitaru_url, "http://127.0.0.1:8000")
        self.assertEqual((settings.db_name, settings.db_port, settings.db_ssl_mode), ("evalve", 5432, "verify-full"))
        self.assertEqual((settings.host, settings.port), ("127.0.0.1", 8100))
        self.assertIsNone(settings.image)
        image = "ghcr.io/evalve-app/evalve-backend@sha256:" + "ab" * 32
        self.assertEqual(Settings.from_environment(ENV | {"EVALVE_IMAGE": image}).image, image)

    def test_missing_settings_are_named(self):
        with self.assertRaisesRegex(SettingsError, "EVALVE_KITARU_URL, EVALVE_DB_PWD"):
            Settings.from_environment({"EVALVE_DB_HOST": "db.internal", "EVALVE_DB_USER": "evalve_admin"})

    def test_the_kitaru_url_must_be_a_plain_http_url(self):
        for url in ("ftp://kitaru", "kitaru:8000", "https://user:pass@kitaru", "https://kitaru/?a=b", "https://kitaru/#x"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(SettingsError, "EVALVE_KITARU_URL"):
                    Settings.from_environment(ENV | {"EVALVE_KITARU_URL": url})

    def test_the_database_name_must_be_a_plain_identifier(self):
        for name in ("Evalve", 'evalve"; DROP DATABASE kitaru; --', "", "1evalve"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(SettingsError, "EVALVE_DB_NAME"):
                    Settings.from_environment(ENV | {"EVALVE_DB_NAME": name})

    def test_ports_must_be_numbers(self):
        with self.assertRaisesRegex(SettingsError, "EVALVE_PORT"):
            Settings.from_environment(ENV | {"EVALVE_PORT": "eighty"})

    def test_the_password_stays_out_of_the_repr_and_error_text(self):
        settings = Settings.from_environment(ENV)
        self.assertNotIn("database-password-value", repr(settings))
        try:
            Settings.from_environment(ENV | {"EVALVE_KITARU_URL": "nope"})
        except SettingsError as error:
            self.assertNotIn("database-password-value", str(error))


if __name__ == "__main__":
    unittest.main()
