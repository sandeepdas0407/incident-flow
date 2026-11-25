"""GitHub operations (PyGithub is synchronous, so callers wrap these in asyncio.to_thread)."""
from __future__ import annotations

from dataclasses import dataclass

from github import Auth, Github, GithubException

from app.config import Settings


@dataclass
class FileEdit:
    path: str
    new_content: str


@dataclass
class PullRequestInfo:
    number: int
    url: str
    state: str  # open|closed
    merged: bool
    merge_sha: str | None
    head_sha: str | None = None  # latest commit on the PR branch (for its PR checks)


@dataclass
class RunInfo:
    status: str  # queued|in_progress|completed
    conclusion: str | None  # success|failure|cancelled|...
    url: str


class GitHubOps:
    def __init__(self, settings: Settings):
        self.s = settings
        self.gh = Github(auth=Auth.Token(settings.github_token))
        self._repo = None

    @property
    def repo(self):
        if self._repo is None:
            self._repo = self.gh.get_repo(self.s.github_repo)
        return self._repo

    def read_file(self, path: str, ref: str | None = None) -> str:
        f = self.repo.get_contents(path, ref=ref or self.s.github_base_branch)
        if isinstance(f, list):
            raise IsADirectoryError(path)
        return f.decoded_content.decode("utf-8")

    def list_files(self, prefix: str = "") -> list[str]:
        tree = self.repo.get_git_tree(self.s.github_base_branch, recursive=True).tree
        return [t.path for t in tree if t.type == "blob" and t.path.startswith(prefix)]

    def open_pr(self, branch: str, title: str, body: str, edits: list[FileEdit], commit_msg: str) -> PullRequestInfo:
        base = self.repo.get_branch(self.s.github_base_branch)
        try:
            self.repo.create_git_ref(f"refs/heads/{branch}", base.commit.sha)
        except GithubException as e:
            if e.status != 422:  # 422 = branch already exists (retry after crash)
                raise
        for e in edits:
            try:
                current = self.repo.get_contents(e.path, ref=branch)
            except GithubException as ex:
                if ex.status != 404:
                    raise
                self.repo.create_file(e.path, commit_msg, e.new_content, branch=branch)  # new file
            else:
                self.repo.update_file(e.path, commit_msg, e.new_content, current.sha, branch=branch)
        pr = self.repo.create_pull(title=title, body=body, head=branch, base=self.s.github_base_branch)
        return PullRequestInfo(pr.number, pr.html_url, pr.state, False, None, pr.head.sha)

    def pr_info(self, number: int) -> PullRequestInfo:
        pr = self.repo.get_pull(number)
        return PullRequestInfo(pr.number, pr.html_url, pr.state, pr.merged,
                               pr.merge_commit_sha if pr.merged else None, pr.head.sha)

    def workflow_run(self, workflow_file: str, head_sha: str) -> RunInfo | None:
        runs = self.repo.get_workflow(workflow_file).get_runs(head_sha=head_sha)
        for run in runs:
            return RunInfo(run.status, run.conclusion, run.html_url)
        return None

    def superseding_run(self, workflow_file: str, merge_sha: str) -> RunInfo | None:
        """Newest run of the workflow on the base branch whose commit already contains merge_sha.

        GitHub keeps only one pending run per concurrency group, so a queued deploy can be
        cancelled by a newer push; the newer run then deploys our change too.
        """
        runs = self.repo.get_workflow(workflow_file).get_runs(branch=self.s.github_base_branch, event="push")
        for i, run in enumerate(runs):
            if i >= 10:
                break
            if run.head_sha == merge_sha:
                continue
            if self.repo.compare(merge_sha, run.head_sha).status in ("ahead", "identical"):
                return RunInfo(run.status, run.conclusion, run.html_url)
        return None
