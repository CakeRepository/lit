"""The standard-library GitHub client, with a fake urlopen: no network."""

import io
import json
import os
import unittest
import urllib.error
import zipfile
from unittest.mock import patch

from concurrent_merge import github

NO_TOKEN = dict(GH_TOKEN="", GITHUB_TOKEN="", GITHUB_API_URL="https://api.github.com")


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def reply(payload):
    return Reply(payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def http_error(code, message):
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "reason", {}, io.BytesIO(json.dumps(dict(message=message)).encode())
    )


class Env(unittest.TestCase):
    def setUp(self):
        github._tokens.clear()
        patcher = patch.dict("os.environ", NO_TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(github._tokens.clear)


class TokenTests(Env):
    def test_environment_comes_first_then_git_then_nothing(self):
        with patch.dict("os.environ", dict(GH_TOKEN="a", GITHUB_TOKEN="b")):
            self.assertEqual(github.token(), "a")
        with patch.dict("os.environ", dict(GITHUB_TOKEN="b")):
            self.assertEqual(github.token(), "b")
        done = type("Done", (), dict(stdout="protocol=https\nhost=github.com\nusername=u\npassword=secret\n"))
        with patch("concurrent_merge.github.subprocess.run", return_value=done) as run:
            self.assertEqual(github.token(), "secret")
            self.assertEqual(run.call_args.args[0], ["git", "credential", "fill"])
            self.assertEqual(run.call_args.kwargs["input"], "protocol=https\nhost=github.com\n\n")
            github._tokens.clear()
            with patch.dict("os.environ", dict(GITHUB_API_URL="https://ghe.example/api/v3")):
                github.token()
            self.assertIn("host=ghe.example\n", run.call_args.kwargs["input"])
        github._tokens.clear()
        empty = type("Done", (), dict(stdout="protocol=https\nhost=github.com\n"))
        with patch("concurrent_merge.github.subprocess.run", return_value=empty):
            self.assertIsNone(github.token())
        github._tokens.clear()
        with patch("concurrent_merge.github.subprocess.run", side_effect=OSError):
            self.assertIsNone(github.token())

    def test_no_token_sends_no_authorization(self):
        with patch("concurrent_merge.github.token", return_value=None):
            self.assertNotIn("Authorization", github.headers())
        with patch("concurrent_merge.github.token", return_value="t"):
            self.assertEqual(github.headers()["Authorization"], "Bearer t")


class RequestTests(Env):
    def test_rest_builds_url_query_headers_and_body(self):
        seen = []

        def fake(request, timeout=None):
            seen.append(request)
            return reply(dict(ok=True))

        with patch.dict("os.environ", dict(GH_TOKEN="tok")), patch("urllib.request.urlopen", fake):
            out = github.rest("POST", "/repos/o/r/pulls", dict(state="open", page=2), dict(draft=True))
        self.assertEqual(out, dict(ok=True))
        request = seen[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/o/r/pulls?state=open&page=2")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), dict(draft=True))
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertEqual(request.get_header("X-github-api-version"), "2022-11-28")

    def test_path_with_a_query_keeps_it(self):
        self.assertEqual(github.build_url("a/b?x=1", dict(y=2)), "https://api.github.com/a/b?x=1&y=2")

    def test_enterprise_urls(self):
        # GitHub Actions sets GITHUB_GRAPHQL_URL for every job, so clear it here.
        with patch.dict("os.environ", dict(GITHUB_API_URL="https://ghe.example/api/v3/")):
            os.environ.pop("GITHUB_GRAPHQL_URL", None)
            self.assertEqual(github.build_url("user"), "https://ghe.example/api/v3/user")
            self.assertEqual(github.graphql_url(), "https://ghe.example/api/v3/graphql")
            os.environ["GITHUB_GRAPHQL_URL"] = "https://ghe.example/api/graphql"
            self.assertEqual(github.graphql_url(), "https://ghe.example/api/graphql")

    def test_graphql_returns_the_response(self):
        with patch("urllib.request.urlopen", lambda r, timeout=None: reply(dict(data=dict(a=1)))):
            self.assertEqual(github.graphql("query{a}", dict(x=1)), dict(data=dict(a=1)))

    def test_graphql_errors_raise_unless_the_caller_reads_them(self):
        body = dict(data=None, errors=[dict(message="Bad things")])
        with patch("urllib.request.urlopen", lambda r, timeout=None: reply(body)):
            with self.assertRaisesRegex(github.GitHubError, "Bad things") as caught:
                github.graphql("query{a}")
            self.assertIsNone(caught.exception.status)
            self.assertEqual(github.graphql("query{a}", raise_on_errors=False), body)

    def test_http_error_carries_status_and_message(self):
        def fake(request, timeout=None):
            raise http_error(404, "Not Found")

        with patch("urllib.request.urlopen", fake):
            with self.assertRaises(github.GitHubError) as caught:
                github.rest("GET", "repos/o/missing")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.message, "Not Found")
        self.assertIsInstance(caught.exception, RuntimeError)

    def test_rest_pages_stop_on_a_short_page(self):
        pages = [list(range(100)), list(range(5))]
        urls = []

        def fake(request, timeout=None):
            urls.append(request.full_url)
            return reply(pages[len(urls) - 1])

        with patch("urllib.request.urlopen", fake):
            self.assertEqual(len(list(github.rest_pages("repos/o/r/pulls", dict(state="open")))), 105)
        self.assertIn("page=2", urls[1])
        self.assertIn("per_page=100", urls[0])


class ArtifactTests(Env):
    def test_matching_artifacts_are_extracted_and_the_token_stays_home(self):
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, "w") as z:
            z.writestr("t.json", "{}")
        listing = dict(
            artifacts=[
                dict(name="validation-timings-1", archive_download_url="https://api.github.com/a/1/zip"),
                dict(name="other", archive_download_url="https://api.github.com/a/2/zip"),
            ]
        )
        sent = []

        def fake(request, timeout=None):
            sent.append(request)
            return reply(listing if "/artifacts" in request.full_url else blob.getvalue())

        class Opener:
            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(
                    request.full_url, 302, "Found", {"Location": "https://blob.example/z?sig=1"}, None
                )

        import tempfile

        with tempfile.TemporaryDirectory() as d, patch.dict("os.environ", dict(GH_TOKEN="tok")):
            with patch("urllib.request.urlopen", fake), patch("urllib.request.build_opener", return_value=Opener()):
                github.download_artifacts("o/r", 5, d, ["validation-timings-*"])
            self.assertTrue((__import__("pathlib").Path(d) / "validation-timings-1" / "t.json").exists())
        self.assertEqual(sent[-1].full_url, "https://blob.example/z?sig=1")
        self.assertIsNone(sent[-1].get_header("Authorization"))

    def test_no_match_is_an_error(self):
        with patch("urllib.request.urlopen", lambda r, timeout=None: reply(dict(artifacts=[]))):
            with self.assertRaises(github.GitHubError):
                github.download_artifacts("o/r", 5, ".", ["x-*"])


if __name__ == "__main__":
    unittest.main()
