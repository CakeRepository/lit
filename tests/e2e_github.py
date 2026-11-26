"""A local stand-in for GitHub, for end-to-end tests of collect, run and land.

Git URLs under https://github.com/ are rewritten (git's url.insteadOf) to bare
repositories in a scratch folder, and GITHUB_API_URL points lit at a small HTTP
server that answers the REST calls collect and land make, from those
repositories' refs. Nothing leaves the machine.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent

IDENTITY = dict(
    GIT_AUTHOR_NAME="Agent", GIT_AUTHOR_EMAIL="agent@example.com",
    GIT_COMMITTER_NAME="Agent", GIT_COMMITTER_EMAIL="agent@example.com",
)


def git(cwd, *args, data=None):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, input=data, capture_output=True,
        env=dict(os.environ, **IDENTITY), check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.decode()}")
    return proc.stdout.decode().strip()


class FakeGitHub:
    """One repository, OWNER/NAME, served from <root>/remotes/OWNER/NAME.git."""

    def __init__(self, root, repository="example/app"):
        self.root = Path(root)
        self.repository = repository
        self.remote = self.root / "remotes" / (repository + ".git")
        self.remote.parent.mkdir(parents=True)
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.pulls = {}  # number -> dict(author, body, draft, labels, association)
        self.calls = []
        self.graphql = None
        self.drafts = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                fake.calls.append(("POST", self.path))
                if self.path == f"/repos/{fake.repository}/pulls":
                    row = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    row["html_url"] = f"https://github.com/{fake.repository}/pull/100"
                    fake.drafts[row["head"]] = row
                    self._send(201, row)
                    return
                if self.path == "/graphql" and fake.graphql:
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    self._send(200, fake.graphql(request["query"], request["variables"]))
                    return
                self._send(403, dict(message="GraphQL is not served here"))

            def do_GET(self):
                fake.calls.append(("GET", self.path))
                code, body = fake.answer(self.path)
                self._send(code, body)

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def ref(self, name):
        try:
            return git(self.remote, "rev-parse", "--verify", "--quiet", name)
        except RuntimeError:
            return None

    def answer(self, raw):
        url = urlsplit(raw)
        query = parse_qs(url.query)
        prefix = f"/repos/{self.repository}/"
        if not url.path.startswith(prefix):
            return 404, dict(message="Not Found")
        rest = url.path[len(prefix):]
        if rest.startswith("git/ref/heads/"):
            sha = self.ref("refs/heads/" + rest[len("git/ref/heads/"):])
            if not sha:
                return 404, dict(message="Not Found")
            return 200, dict(object=dict(type="commit", sha=sha))
        if rest == "pulls":
            if "head" in query:
                head = query["head"][0].split(":", 1)[1]
                draft = self.drafts.get(head)
                if not draft:
                    return 200, []
                return 200, [dict(number=100, state="open", html_url=draft["html_url"],
                                  head=dict(ref=head, sha=self.ref("refs/heads/" + head),
                                            repo=dict(full_name=self.repository)))]
            if int(query.get("page", ["1"])[0]) > 1:
                return 200, []
            base = query.get("base", ["main"])[0]
            rows = []
            for n, pr in sorted(self.pulls.items()):
                head = self.ref(f"refs/pull/{n}/head")
                if pr.get("closed") or not head:
                    continue
                rows.append(dict(
                    number=n, draft=pr.get("draft", False), body=pr.get("body", ""),
                    head=dict(sha=head), base=dict(ref=base),
                    user=dict(login=pr["author"]),
                    author_association=pr.get("association", "MEMBER"),
                    labels=[dict(name=l) for l in pr.get("labels", ())],
                ))
            return 200, rows
        if rest.startswith("pulls/") and rest.endswith("/reviews"):
            return 200, []
        return 404, dict(message="Not Found")

    def env(self):
        """Environment for a lit process that should talk to this fake."""
        return dict(
            GITHUB_API_URL=f"http://127.0.0.1:{self.server.server_port}",
            GITHUB_GRAPHQL_URL=f"http://127.0.0.1:{self.server.server_port}/graphql",
            GH_TOKEN="test-token",
            LIT_GITHUB_API="rest",
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0=f"url.{(self.root / 'remotes').as_uri()}/.insteadOf",
            GIT_CONFIG_VALUE_0="https://github.com/",
            **IDENTITY,
        )

    def open_pull(self, number, author, workdir, branch, **extra):
        """Push branch from workdir as PR number's head."""
        git(workdir, "push", "-q", "--force", str(self.remote), f"{branch}:refs/pull/{number}/head")
        self.pulls[number] = dict(author=author, **extra)


def lit_command():
    """How to run lit: LIT_BIN (the lit binary) or this checkout's Python lit."""
    if os.environ.get("LIT_BIN"):
        return [os.environ["LIT_BIN"]]
    return [sys.executable, "-B", "-c",
            "import sys; sys.path.insert(0, sys.argv.pop(1)); "
            "from concurrent_merge.cli import main; sys.exit(main())", str(ROOT)]


def lit(cwd, env, *args):
    return subprocess.run([*lit_command(), *args], cwd=cwd, env=dict(os.environ, **env),
                          capture_output=True, text=True)
