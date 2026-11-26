"""A small GitHub client on the Python standard library: REST, GraphQL and run artifacts."""

import fnmatch
import io
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile

TIMEOUT = 60
_tokens = {}


class GitHubError(RuntimeError):
    """A refused GitHub request: `status` is the HTTP status, or None for GraphQL errors."""

    def __init__(self, message, status=None):
        super().__init__(message if status is None else f"GitHub {status}: {message}")
        self.status = status
        self.message = message


def api_url():
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def git_destination(repository):
    """Use the harness's local repository when comparing publication commands."""
    if "LIT_PARITY_LAND_TARGET" in os.environ:
        target = Path(os.environ["LIT_PARITY_LAND_TARGET"])
        if os.environ.get("GIT_ALLOW_PROTOCOL") != "file" or not target.is_absolute() or not target.is_dir():
            raise ValueError("Expected an isolated local landing target")
        return str(target)
    return "https://github.com/" + repository + ".git"


def graphql_url():
    return os.environ.get("GITHUB_GRAPHQL_URL") or api_url() + "/graphql"


def web_host(url=None):
    """The host git knows credentials by: github.com for api.github.com."""
    host = urllib.parse.urlsplit(url or api_url()).hostname or "github.com"
    return host[4:] if host.startswith("api.") else host


def token(url=None):
    """GH_TOKEN, else GITHUB_TOKEN, else git's credential helper, else None. Never logged."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    host = web_host(url)
    if host not in _tokens:
        _tokens[host] = _git_credential(host)
    return _tokens[host]


def _git_credential(host):
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n",
            capture_output=True,
            text=True,
            timeout=15,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("password=") and len(line) > 9:
            return line[9:]
    return None


def headers(url=None, extra=None):
    found = dict(
        Accept="application/vnd.github+json",
        **{"X-GitHub-Api-Version": "2022-11-28", "User-Agent": "lit"},
    )
    secret = token(url)
    if secret:
        found["Authorization"] = "Bearer " + secret
    found.update(extra or {})
    return found


def build_url(path, query=None):
    base = path if path.startswith(("http://", "https://")) else api_url() + "/" + path.lstrip("/")
    if query:
        base += ("&" if "?" in base else "?") + urllib.parse.urlencode(
            {k: str(v) for k, v in query.items() if v is not None}
        )
    return base


def _failure(error):
    try:
        detail = json.loads(error.read().decode("utf8", "replace"))
        message = detail.get("message") if isinstance(detail, dict) else None
    except (ValueError, OSError):
        message = None
    return GitHubError(message or error.reason or "request failed", error.code)


def _send(request):
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise _failure(error) from None
    except urllib.error.URLError as error:
        raise GitHubError(f"could not reach GitHub: {error.reason}") from None


def rest(method, path, query=None, body=None):
    """Call a REST endpoint and return its parsed JSON (None when the reply is empty)."""
    url = build_url(path, query)
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers=headers(url, {"Content-Type": "application/json"} if data else None),
    )
    raw = _send(request)
    return json.loads(raw) if raw.strip() else None


def rest_pages(path, query=None):
    """Yield every row of a list endpoint, 100 per page."""
    page = 1
    while True:
        rows = rest("GET", path, dict(query or {}, per_page=100, page=page))
        yield from rows
        if len(rows) < 100:
            return
        page += 1


def graphql(query, variables=None, raise_on_errors=True):
    """Run a GraphQL document and return the whole response ({"data": ..., "errors": ...}).

    Like `gh api graphql`, a response carrying errors is a failure unless the caller reads them."""
    url = graphql_url()
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(query=query, variables=variables or {})).encode(),
        method="POST",
        headers=headers(url, {"Content-Type": "application/json"}),
    )
    result = json.loads(_send(request))
    if raise_on_errors and result.get("errors"):
        raise GitHubError(
            "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in result["errors"])
        )
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_artifacts(repository, run_id, directory, patterns):
    """Extract a run's unexpired artifacts whose names match any glob into directory/<name>/."""
    wanted = []
    for row in _artifact_rows(repository, run_id):
        if not row.get("expired") and any(fnmatch.fnmatchcase(row["name"], p) for p in patterns):
            wanted.append(row)
    if not wanted:
        raise GitHubError("no artifact matches the given patterns")
    root = Path(directory).resolve()
    for artifact in wanted:
        url = build_url(artifact["archive_download_url"])
        request = urllib.request.Request(url, headers=headers(url))
        try:
            blob = urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT).read()
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location") if error.code in (301, 302, 303, 307, 308) else None
            if not location:
                raise _failure(error) from None
            # The signed storage URL must not receive our token.
            blob = _send(urllib.request.Request(location, headers={"User-Agent": "lit"}))
        target = root / artifact["name"]
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for member in archive.namelist():
                if not (target / member).resolve().is_relative_to(target):
                    raise GitHubError("artifact holds a path outside its directory")
            archive.extractall(target)


def _artifact_rows(repository, run_id):
    page = 1
    while True:
        reply = rest(
            "GET",
            f"repos/{repository}/actions/runs/{run_id}/artifacts",
            dict(per_page=100, page=page),
        )
        rows = reply.get("artifacts", [])
        yield from rows
        if len(rows) < 100:
            return
        page += 1
