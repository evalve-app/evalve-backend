import unittest

from evalve_backend.settings import Settings, SettingsError

ENV = {
    "EVALVE_KITARU_URL": "http://127.0.0.1:8000/",
    "EVALVE_DB_HOST": "db.internal",
    "EVALVE_DB_USER": "evalve_admin",
    "EVALVE_DB_PWD": "database-password-value",
}
FARGATE = {
    "EVALVE_EXECUTOR": "fargate",
    "EVALVE_AWS_REGION": "eu-central-1",
    "EVALVE_AGENT_IMAGE_REPOSITORY": "123456789012.dkr.ecr.eu-central-1.amazonaws.com/evalve/agents",
    "EVALVE_AGENT_IMAGE_PUSH_ROLE_ARN": "arn:aws:iam::123456789012:role/evalve-agent-image-push",
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

    def test_the_session_cookie_name_has_a_default_and_can_be_turned_off(self):
        self.assertEqual(Settings.from_environment(ENV).session_cookie_name, "evalve_session")
        named = Settings.from_environment(ENV | {"EVALVE_SESSION_COOKIE_NAME": "kitaru_session"})
        self.assertEqual(named.session_cookie_name, "kitaru_session")
        self.assertEqual(Settings.from_environment(ENV | {"EVALVE_SESSION_COOKIE_NAME": ""}).session_cookie_name, "")

    def test_the_session_cookie_name_must_be_a_plain_cookie_name(self):
        for name in ("evalve session", "a=b", "a;b", "sess\r\nX-Injected: 1", "é", "x" * 65):
            with self.subTest(name=name):
                with self.assertRaisesRegex(SettingsError, "EVALVE_SESSION_COOKIE_NAME"):
                    Settings.from_environment(ENV | {"EVALVE_SESSION_COOKIE_NAME": name})

    def test_ports_must_be_numbers(self):
        with self.assertRaisesRegex(SettingsError, "EVALVE_PORT"):
            Settings.from_environment(ENV | {"EVALVE_PORT": "eighty"})

    def test_the_executor_is_kitaru_by_default_and_reads_no_aws_setting(self):
        settings = Settings.from_environment(ENV | {"EVALVE_AGENT_IMAGE_REPOSITORY": "not a repository"})
        self.assertEqual(settings.executor, "kitaru")
        self.assertIsNone(settings.agent_image_repository)

    def test_the_executor_must_be_kitaru_or_fargate(self):
        with self.assertRaisesRegex(SettingsError, "^EVALVE_EXECUTOR must be kitaru or fargate.$"):
            Settings.from_environment(ENV | {"EVALVE_EXECUTOR": "lambda"})

    def test_fargate_reads_the_aws_settings_of_the_agent_image_push(self):
        settings = Settings.from_environment(ENV | FARGATE)
        self.assertEqual(settings.executor, "fargate")
        self.assertEqual(settings.aws_region, "eu-central-1")
        self.assertEqual(settings.agent_image_repository, "123456789012.dkr.ecr.eu-central-1.amazonaws.com/evalve/agents")
        self.assertEqual(settings.agent_image_registry, "123456789012.dkr.ecr.eu-central-1.amazonaws.com")
        self.assertEqual(settings.agent_image_repository_name, "evalve/agents")
        self.assertEqual(settings.agent_image_push_role_arn, "arn:aws:iam::123456789012:role/evalve-agent-image-push")

    def test_fargate_without_the_push_role_does_not_start(self):
        env = ENV | FARGATE
        del env["EVALVE_AGENT_IMAGE_PUSH_ROLE_ARN"]
        with self.assertRaisesRegex(SettingsError, "^With EVALVE_EXECUTOR=fargate, set EVALVE_AGENT_IMAGE_PUSH_ROLE_ARN.$"):
            Settings.from_environment(env)

    def test_fargate_names_each_missing_aws_setting(self):
        with self.assertRaisesRegex(SettingsError, (
            "^With EVALVE_EXECUTOR=fargate, set EVALVE_AWS_REGION, EVALVE_AGENT_IMAGE_REPOSITORY, "
            "EVALVE_AGENT_IMAGE_PUSH_ROLE_ARN.$"
        )):
            Settings.from_environment(ENV | {"EVALVE_EXECUTOR": "fargate"})

    def test_the_aws_settings_must_have_their_format_and_the_error_holds_no_value(self):
        for name, value in (
            ("EVALVE_AWS_REGION", "Frankfurt"),
            ("EVALVE_AGENT_IMAGE_REPOSITORY", "ghcr.io/evalve-app/agents"),
            ("EVALVE_AGENT_IMAGE_REPOSITORY", "123456789012.dkr.ecr.eu-central-1.amazonaws.com"),
            ("EVALVE_AGENT_IMAGE_REPOSITORY", "123456789012.dkr.ecr.eu-central-1.amazonaws.com/Agents"),
            ("EVALVE_AGENT_IMAGE_PUSH_ROLE_ARN", "arn:aws:iam::123456789012:user/someone"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(SettingsError, f"^{name} must be ") as raised:
                    Settings.from_environment(ENV | FARGATE | {name: value})
                self.assertNotIn(value, str(raised.exception))

    def test_the_password_stays_out_of_the_repr_and_error_text(self):
        settings = Settings.from_environment(ENV)
        self.assertNotIn("database-password-value", repr(settings))
        try:
            Settings.from_environment(ENV | {"EVALVE_KITARU_URL": "nope"})
        except SettingsError as error:
            self.assertNotIn("database-password-value", str(error))


if __name__ == "__main__":
    unittest.main()
