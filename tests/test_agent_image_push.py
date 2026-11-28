"""The push credential route: a short-lived ECR token for the one agent image repository.

STS and ECR answer through botocore `Stubber`, on clients that the backend's own factory builds.
`Stubber` answers before botocore signs a request or parses an answer, so the tests of the signature
and of the log answer at the HTTP level instead (`AwsOverHttp`).
"""

import base64
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone

from botocore.awsrequest import AWSResponse
from botocore.exceptions import NoCredentialsError
from botocore.stub import Stubber

from evalve_backend import aws
from evalve_backend.__main__ import configure_logging
from helpers import ACCOUNT, TOKEN, FakeKitaru, client
from test_sign_in import DASHBOARD_ACCOUNT, SESSION, kitaru_with_sessions

PUSH = "/evalve/v1/agent-images/push-credential"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
ROLE_ARN = "arn:aws:iam::123456789012:role/evalve-agent-image-push"
FARGATE = {
    "executor": "fargate", "aws_region": "eu-central-1",
    "agent_image_repository": "123456789012.dkr.ecr.eu-central-1.amazonaws.com/evalve/agents",
    "agent_image_push_role_arn": ROLE_ARN,
}
PASSWORD = "eyJwYXlsb2FkIjoiZWNyLXB1c2gtdG9rZW4tdmFsdWUifQ=="
# botocore gives `expiresAt` in the zone of the host, which here is one hour east of UTC.
EXPIRES_AT = datetime(2026, 11, 29, 11, 0, tzinfo=timezone(timedelta(hours=1)))
ENCODED_TOKEN = base64.b64encode(f"AWS:{PASSWORD}".encode()).decode()
REPLY = {
    "registry": "123456789012.dkr.ecr.eu-central-1.amazonaws.com",
    "repository": "evalve/agents",
    "username": "AWS",
    "password": PASSWORD,
    "expires_at": "2026-11-29T10:00:00Z",
}
# Stands in for the instance role that the real factory reads from IMDS.
INSTANCE_ROLE = aws.AwsKeys("ASIAINSTANCEROLE0000", "instance-role-secret", "instance-role-token")
SESSION_KEYS = aws.AwsKeys("ASIAPUSHSESSION00000", "push-session-secret", "push-session-token")


def assumed_role() -> dict:
    return {
        "Credentials": {
            "AccessKeyId": SESSION_KEYS.access_key_id, "SecretAccessKey": SESSION_KEYS.secret_access_key,
            "SessionToken": SESSION_KEYS.session_token, "Expiration": datetime(2026, 11, 28, 22, 15, tzinfo=timezone.utc),
        },
        "AssumedRoleUser": {
            "AssumedRoleId": "AROAEXAMPLEROLEID0000:evalve-push", "Arn": "arn:aws:sts::123456789012:assumed-role/x/y",
        },
    }


def authorization_token(password: str = PASSWORD) -> dict:
    return {"authorizationData": [{
        "authorizationToken": base64.b64encode(f"AWS:{password}".encode()).decode(),
        "expiresAt": EXPIRES_AT, "proxyEndpoint": "https://123456789012.dkr.ecr.eu-central-1.amazonaws.com",
    }]}


def assume_role_params(account_id: str) -> dict:
    return {"RoleArn": ROLE_ARN, "RoleSessionName": f"evalve-push-{account_id}", "DurationSeconds": 900}


class FakeAws:
    """The AWS client factory of the backend, with one stubbed answer for each client that it builds.

    It records how each client was built, so a test can see which credentials a call used.
    """

    def __init__(self):
        self.answers: dict[str, list[dict]] = {"sts": [], "ecr": []}
        self.built: list[tuple[str, str, aws.AwsKeys | None]] = []
        self._stubbers: list[Stubber] = []

    def answer(self, service: str, method: str, response: dict | None = None, *,
               expected_params: dict | None = None, error_code: str | None = None) -> None:
        self.answers[service].append(
            {"method": method, "response": response, "expected_params": expected_params, "error_code": error_code})

    def __call__(self, service: str, region: str, credentials: aws.AwsKeys | None = None):
        self.built.append((service, region, credentials))
        built = aws.client(service, region, credentials or INSTANCE_ROLE)
        stubber = Stubber(built)
        answer = self.answers[service].pop(0)
        if answer["error_code"] is not None:
            stubber.add_client_error(answer["method"], service_error_code=answer["error_code"], http_status_code=403)
        else:
            stubber.add_response(answer["method"], answer["response"], answer["expected_params"])
        stubber.activate()
        self._stubbers.append(stubber)
        return built

    def assert_all_answered(self) -> None:
        for stubber in self._stubbers:
            stubber.assert_no_pending_responses()
        assert not any(self.answers.values()), self.answers


class RawBody:
    """The body of an HTTP answer, in the form that botocore reads."""

    def __init__(self, body: str):
        self._body = body.encode()

    def stream(self, **_):
        yield self._body


ASSUME_ROLE_ANSWER = f"""<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleResult>
    <Credentials>
      <AccessKeyId>{SESSION_KEYS.access_key_id}</AccessKeyId>
      <SecretAccessKey>{SESSION_KEYS.secret_access_key}</SecretAccessKey>
      <SessionToken>{SESSION_KEYS.session_token}</SessionToken>
      <Expiration>2026-11-28T22:15:00Z</Expiration>
    </Credentials>
    <AssumedRoleUser>
      <AssumedRoleId>AROAEXAMPLEROLEID0000:evalve-push</AssumedRoleId>
      <Arn>arn:aws:sts::123456789012:assumed-role/evalve-agent-image-push/evalve-push</Arn>
    </AssumedRoleUser>
  </AssumeRoleResult>
  <ResponseMetadata><RequestId>4d2b5c38-0000-4000-8000-000000000001</RequestId></ResponseMetadata>
</AssumeRoleResponse>"""


class AwsOverHttp:
    """The AWS client factory of the backend, with STS and ECR answers at the HTTP level.

    botocore signs each request and parses each answer as it does with AWS. The fake records the
    access key ID in the SigV4 signature of each request.
    """

    def __init__(self):
        self.signed_with: dict[str, str] = {}

    def __call__(self, service: str, region: str, credentials: aws.AwsKeys | None = None):
        built = aws.client(service, region, credentials or INSTANCE_ROLE)
        built.meta.events.register("before-send", lambda request, **_: self._answer(service, request))
        return built

    def _answer(self, service: str, request) -> AWSResponse:
        signature = request.headers["Authorization"]
        signature = signature.decode() if isinstance(signature, bytes) else signature
        # The form is `AWS4-HMAC-SHA256 Credential=<access key ID>/<date>/<region>/<service>/aws4_request, ...`.
        self.signed_with[service] = signature.split("Credential=")[1].split("/")[0]
        if service == "sts":
            return AWSResponse(request.url, 200, {}, RawBody(ASSUME_ROLE_ANSWER))
        body = {"authorizationData": [{
            "authorizationToken": ENCODED_TOKEN, "expiresAt": EXPIRES_AT.timestamp(),
            "proxyEndpoint": "https://123456789012.dkr.ecr.eu-central-1.amazonaws.com",
        }]}
        return AWSResponse(request.url, 200, {}, RawBody(json.dumps(body)))


def granting(account_id: str = ACCOUNT["id"]) -> FakeAws:
    fake = FakeAws()
    fake.answer("sts", "assume_role", assumed_role(), expected_params=assume_role_params(account_id))
    fake.answer("ecr", "get_authorization_token", authorization_token(), expected_params={})
    return fake


class PushCredentialTest(unittest.TestCase):
    def test_a_bearer_credential_gets_a_push_credential_from_a_session_of_the_push_role(self):
        fake = granting()
        with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=fake) as api:
            response = api.post(PUSH, headers=BEARER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), REPLY)
        fake.assert_all_answered()
        # STS gets the instance role, and ECR gets the keys of the new session.
        self.assertEqual(fake.built, [("sts", "eu-central-1", None), ("ecr", "eu-central-1", SESSION_KEYS)])

    def test_a_dashboard_session_gets_the_same_push_credential(self):
        fake = granting(DASHBOARD_ACCOUNT["id"])
        with client(kitaru_with_sessions(), app_settings=FARGATE, make_aws_client=fake) as api:
            response = api.post(PUSH, headers=SESSION)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), REPLY)
        fake.assert_all_answered()

    def test_a_call_with_no_credential_is_refused_and_makes_no_aws_call(self):
        fake = FakeAws()
        with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=fake) as api:
            response = api.post(PUSH)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(fake.built, [])

    def test_a_deployment_on_kitaru_has_no_agent_image_repository_and_makes_no_aws_call(self):
        fake = FakeAws()
        with client(FakeKitaru(), make_aws_client=fake) as api:
            response = api.post(PUSH, headers=BEARER)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "This deployment has no agent image repository."})
        self.assertEqual(fake.built, [])

    def test_an_access_denied_of_the_push_role_gives_503_with_no_ecr_call(self):
        fake = FakeAws()
        fake.answer("sts", "assume_role", error_code="AccessDenied")
        with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=fake) as api:
            response = api.post(PUSH, headers=BEARER)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "AWS could not give a push credential. Try again."})
        self.assertEqual([service for service, _, _ in fake.built], ["sts"])

    def test_an_error_of_ecr_gives_503(self):
        fake = FakeAws()
        fake.answer("sts", "assume_role", assumed_role())
        fake.answer("ecr", "get_authorization_token", error_code="AccessDeniedException")
        with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=fake) as api:
            response = api.post(PUSH, headers=BEARER)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "AWS could not give a push credential. Try again."})

    def test_an_instance_role_that_cannot_be_read_gives_503(self):
        def no_instance_role(service, region, credentials=None):
            raise NoCredentialsError

        with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=no_instance_role) as api:
            response = api.post(PUSH, headers=BEARER)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "AWS could not give a push credential. Try again."})

    def test_each_call_assumes_the_role_again(self):
        fake = granting()
        fake.answer("sts", "assume_role", assumed_role(), expected_params=assume_role_params(ACCOUNT["id"]))
        fake.answer("ecr", "get_authorization_token", authorization_token("a-second-token"), expected_params={})
        with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=fake) as api:
            first = api.post(PUSH, headers=BEARER)
            second = api.post(PUSH, headers=BEARER)
        self.assertEqual((first.json()["password"], second.json()["password"]), (PASSWORD, "a-second-token"))
        self.assertEqual([service for service, _, _ in fake.built], ["sts", "ecr", "sts", "ecr"])
        fake.assert_all_answered()

    def test_no_cache_keeps_the_reply(self):
        with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=granting()) as api:
            response = api.post(PUSH, headers=BEARER)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def capture_logs(self, *fakes: FakeAws) -> list[str]:
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, logging.WARNING)
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("test").debug("capture started")
            for fake in fakes:
                with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=fake) as api:
                    api.post(PUSH, headers=BEARER)
        return [record.getMessage() + str(record.exc_info or "") for record in captured.records]

    def test_no_log_line_holds_the_password(self):
        refused = FakeAws()
        refused.answer("sts", "assume_role", assumed_role())
        refused.answer("ecr", "get_authorization_token", error_code="AccessDeniedException")
        logged = self.capture_logs(granting(), refused)
        for secret in (PASSWORD, ENCODED_TOKEN, SESSION_KEYS.secret_access_key, SESSION_KEYS.session_token):
            self.assertFalse([line for line in logged if secret in line], secret)

    def test_one_log_line_names_the_account_of_the_credential(self):
        logged = self.capture_logs(granting())
        self.assertEqual([line for line in logged if ACCOUNT["id"] in line], [
            f"Gave a push credential for the agent images to the account {ACCOUNT['id']}. "
            "It expires at 2026-11-29T10:00:00+00:00.",
        ])

    def test_the_aws_clients_fail_fast(self):
        config = aws.client("sts", "eu-central-1", INSTANCE_ROLE).meta.config
        self.assertEqual((config.connect_timeout, config.read_timeout), (3, 10))

    def test_the_sdk_does_not_repeat_an_aws_call(self):
        config = aws.client("sts", "eu-central-1", INSTANCE_ROLE).meta.config
        self.assertEqual(config.retries["total_max_attempts"], 1)


class PushCredentialOverHttpTest(unittest.TestCase):
    def test_the_instance_role_signs_the_sts_call_and_the_push_session_signs_the_ecr_call(self):
        fake = AwsOverHttp()
        with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=fake) as api:
            response = api.post(PUSH, headers=BEARER)
        self.assertEqual(response.json(), REPLY)
        self.assertEqual(fake.signed_with, {"sts": "ASIAINSTANCEROLE0000", "ecr": "ASIAPUSHSESSION00000"})

    def test_with_the_logging_of_the_backend_no_log_line_holds_a_credential_of_an_aws_answer(self):
        # botocore logs each answer body at DEBUG. The backend sets its loggers higher.
        root, quiet = logging.getLogger(), ("httpx", "boto3", "botocore", "urllib3")
        levels = {name: logging.getLogger(name).level for name in quiet}
        self.addCleanup(lambda: [logging.getLogger(name).setLevel(level) for name, level in levels.items()])
        self.addCleanup(setattr, root, "handlers", root.handlers[:])
        self.addCleanup(root.setLevel, root.level)
        configure_logging()
        with self.assertLogs(level=logging.DEBUG) as captured:
            with client(FakeKitaru(), app_settings=FARGATE, make_aws_client=AwsOverHttp()) as api:
                self.assertEqual(api.post(PUSH, headers=BEARER).status_code, 200)
        logged = "\n".join(record.getMessage() for record in captured.records)
        for secret in (PASSWORD, ENCODED_TOKEN, SESSION_KEYS.secret_access_key, SESSION_KEYS.session_token):
            self.assertNotIn(secret, logged)

if __name__ == "__main__":
    unittest.main()
