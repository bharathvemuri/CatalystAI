"""GitHub implementation of the tracker adapter.

Talks to the REST API over HTTPS with a bearer token, using the standard library
rather than a client package: the surface used here is a handful of endpoints,
and every dependency added to the harness is a dependency an agent has to
install inside a container on every run.

Credential resolution accepts the ``gh`` CLI's own token as a last resort, so a
machine already logged in with ``gh auth login`` needs no ``.env`` at all — but
``gh`` is a fallback, never a requirement, because the adapter has to keep
working where only a token exists (CI, a container, a Jira port of this file).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from .. import HARNESS_VERSION
from ..env import describe_sources, lookup
from ..paths import Project
from .base import (Issue, IssueSpec, RepoRef, Tracker, TrackerItemError,
                   TrackerSystemicError)

API_ROOT = "https://api.github.com"
TOKEN_VAR = "GITHUB_TOKEN"
TIMEOUT = 30

NO_CREDENTIALS = (
    "no GitHub credentials found.\n"
    "  Put a token in a gitignored .env:   GITHUB_TOKEN=ghp_...\n"
    "  or log in with the GitHub CLI:      gh auth login\n"
    "The token needs the 'repo' scope to create issues, labels and milestones."
)

_REMOTE_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<name>.+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<name>.+?)(?:\.git)?$"),
    re.compile(r"^https?://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<name>.+?)(?:\.git)?$"),
)


def parse_remote(url: str) -> RepoRef | None:
    """Extract owner/name from any of the URL forms git writes for GitHub."""
    url = url.strip()
    for pattern in _REMOTE_PATTERNS:
        match = pattern.match(url)
        if match:
            return RepoRef(match.group("owner"), match.group("name"))
    return None


def parse_repo_arg(value: str) -> RepoRef | None:
    """Parse an ``owner/name`` (or full URL) command-line argument."""
    from_url = parse_remote(value)
    if from_url:
        return from_url
    parts = value.strip().strip("/").split("/")
    if len(parts) == 2 and all(parts):
        return RepoRef(parts[0], parts[1])
    return None


def gh_cli_token() -> str | None:
    """The token an existing `gh auth login` already holds, if gh is installed."""
    if not shutil.which("gh"):
        return None
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def resolve_token(project: Project | None = None) -> tuple[str, str]:
    """Find a token, or raise the same clean failure ``llm`` raises for the API."""
    found = lookup(TOKEN_VAR, project)
    if found:
        return found

    token = gh_cli_token()
    if token:
        return token, "gh auth token"

    raise TrackerSystemicError(
        f"{NO_CREDENTIALS}\nSearched: {describe_sources(project)}, then `gh auth token`."
    )


class GitHubTracker(Tracker):
    provider = "github"

    def __init__(self, token: str, api_root: str = API_ROOT):
        self._token = token
        self._api = api_root.rstrip("/")
        self._account: str | None = None
        self._label_cache: dict[str, set[str]] = {}
        self._milestone_cache: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------- transport

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, allow_404: bool = False) -> tuple[object, str | None]:
        url = path if path.startswith("http") else f"{self._api}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", f"ai-harness/{HARNESS_VERSION}")
        if data is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read().decode("utf-8")
                return (json.loads(raw) if raw.strip() else None), response.headers.get("Link")
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_404:
                return None, None
            raise self._classify(exc, method, url) from exc
        except urllib.error.URLError as exc:
            raise TrackerSystemicError(f"could not reach {self._api}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TrackerSystemicError(f"{self._api} timed out after {TIMEOUT}s") from exc

    @staticmethod
    def _classify(exc: urllib.error.HTTPError, method: str, url: str):
        """Decide whether this failure dooms the rest of the run or just this item."""
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except (ValueError, OSError):
            payload = {}
        message = payload.get("message") or exc.reason or "unknown error"
        details = payload.get("errors") or []
        if details:
            rendered = "; ".join(
                d.get("message") or f"{d.get('field', '?')}: {d.get('code', '?')}"
                for d in details if isinstance(d, dict))
            if rendered:
                message = f"{message} ({rendered})"

        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        rate_limited = exc.code == 429 or (
            exc.code == 403 and (remaining == "0" or "rate limit" in message.lower()))

        if rate_limited:
            return TrackerSystemicError(f"GitHub rate limit reached: {message}")
        if exc.code == 401:
            return TrackerSystemicError(
                f"GitHub rejected the credential: {message}\n"
                "The token is missing, expired, or revoked.")
        if exc.code == 403:
            return TrackerSystemicError(
                f"GitHub refused the request: {message}\n"
                "The token most likely lacks the 'repo' scope, or the account "
                "cannot write to this repository.")
        if exc.code == 404:
            return TrackerSystemicError(
                f"not found: {method} {url}\n"
                "Either the repository does not exist or the token cannot see it.")
        if exc.code >= 500:
            return TrackerSystemicError(f"GitHub server error {exc.code}: {message}")
        return TrackerItemError(f"GitHub rejected the request ({exc.code}): {message}")

    def _paginate(self, path: str) -> list[dict]:
        items: list[dict] = []
        url = path
        while url:
            page, link = self._request("GET", url)
            items.extend(page or [])
            url = _next_link(link)
        return items

    # ------------------------------------------------------------ operations

    def whoami(self) -> str:
        if self._account is None:
            user, _ = self._request("GET", "/user")
            self._account = (user or {}).get("login", "")
        return self._account

    def create_repo(self, name: str, *, owner: str | None = None,
                    private: bool = True) -> tuple[RepoRef, str]:
        body = {"name": name, "private": private, "auto_init": False}
        if owner and owner != self.whoami():
            repo, _ = self._request("POST", f"/orgs/{owner}/repos", body)
        else:
            repo, _ = self._request("POST", "/user/repos", body)
        repo = repo or {}
        full = repo.get("full_name", f"{owner or ''}/{name}")
        return RepoRef(*full.split("/", 1)), repo.get("html_url", f"https://github.com/{full}")

    def repo_url(self, repo: RepoRef) -> str | None:
        found, _ = self._request("GET", f"/repos/{repo.full}", allow_404=True)
        return (found or {}).get("html_url") if found else None

    def labels(self, repo: RepoRef, refresh: bool = False) -> set[str]:
        """Cached: creating twelve labels should not cost twelve list calls."""
        if refresh or repo.full not in self._label_cache:
            self._label_cache[repo.full] = {
                label["name"] for label in
                self._paginate(f"/repos/{repo.full}/labels?per_page=100")
            }
        return self._label_cache[repo.full]

    def ensure_label(self, repo: RepoRef, name: str, color: str,
                     description: str) -> bool:
        known = self.labels(repo)
        if name in known:
            return False
        self._request("POST", f"/repos/{repo.full}/labels",
                      {"name": name, "color": color, "description": description})
        known.add(name)
        return True

    def milestones(self, repo: RepoRef, refresh: bool = False) -> dict[str, int]:
        if refresh or repo.full not in self._milestone_cache:
            self._milestone_cache[repo.full] = {
                m["title"]: m["number"] for m in
                self._paginate(f"/repos/{repo.full}/milestones?state=all&per_page=100")
            }
        return self._milestone_cache[repo.full]

    def ensure_milestone(self, repo: RepoRef, title: str,
                         description: str) -> tuple[int, bool]:
        known = self.milestones(repo)
        if title in known:
            return known[title], False
        created, _ = self._request("POST", f"/repos/{repo.full}/milestones",
                                   {"title": title, "description": description})
        number = (created or {})["number"]
        known[title] = number
        return number, True

    def create_issue(self, repo: RepoRef, spec: IssueSpec) -> Issue:
        created, _ = self._request("POST", f"/repos/{repo.full}/issues",
                                   self._issue_body(repo, spec))
        return _to_issue(created or {})

    def get_issue(self, repo: RepoRef, number: int) -> Issue | None:
        found, _ = self._request("GET", f"/repos/{repo.full}/issues/{number}",
                                 allow_404=True)
        return _to_issue(found) if found else None

    def update_issue(self, repo: RepoRef, number: int, spec: IssueSpec) -> Issue:
        updated, _ = self._request("PATCH", f"/repos/{repo.full}/issues/{number}",
                                   self._issue_body(repo, spec))
        return _to_issue(updated or {})

    def _issue_body(self, repo: RepoRef, spec: IssueSpec) -> dict:
        body: dict = {"title": spec.title, "body": spec.body, "labels": list(spec.labels)}
        if spec.milestone:
            known = self.milestones(repo)
            if spec.milestone not in known:
                known = self.milestones(repo, refresh=True)
            if spec.milestone not in known:
                raise TrackerItemError(f"milestone {spec.milestone!r} does not exist")
            body["milestone"] = known[spec.milestone]
        return body


def _next_link(link_header: str | None) -> str | None:
    """Follow RFC 5988 pagination; GitHub caps a page at 100 items."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1].replace(" ", "").replace("'", '"'):
            return section[0].strip().strip("<>")
    return None


def _to_issue(data: dict) -> Issue:
    milestone = data.get("milestone") or {}
    return Issue(
        number=data.get("number", 0),
        title=data.get("title", ""),
        body=data.get("body") or "",
        labels=tuple(sorted(label["name"] for label in data.get("labels", []))),
        milestone=milestone.get("title"),
        url=data.get("html_url", ""),
    )
