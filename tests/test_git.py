"""Git 仓库准备与 Diff 构建（使用本地临时仓库）。"""
from __future__ import annotations

import pytest
from helpers import commit_all, feature_repo, git, make_task

from app.review.git import GitError, RepositoryManager, repository_name_from_url, validate_repository_url


def _task(repo, source, target, **overrides):
    return make_task(repository_url=str(repo), source_sha=source, target_sha=target, **overrides)


def test_repository_url_helpers():
    assert repository_name_from_url("git@host.com:group/demo.git") == "demo"
    assert repository_name_from_url("https://host.com/group/sub/demo") == "demo"
    validate_repository_url("git@host.com:g/r.git", allowed_hosts=frozenset({"host.com"}))
    validate_repository_url("ssh://git@host.com:22/g/r.git", allowed_hosts=frozenset({"host.com"}))
    for url in ("git@evil.com:g/r.git", "/tmp/repo", "file:///tmp/repo"):
        with pytest.raises(GitError):
            validate_repository_url(url, allowed_hosts=frozenset({"host.com"}))


def test_prepare_full_diff_and_cleanup(settings, tmp_path):
    repo, target, source = feature_repo(tmp_path)
    manager = RepositoryManager(settings)
    task = _task(repo, source, target)
    prepared = manager.prepare(task)
    try:
        assert git(prepared.worktree_path, "rev-parse", "HEAD") == source
        assert git(prepared.worktree_path, "remote") == ""  # 不保留推送目标
        built = manager.build_diff(prepared, task)
        assert built.mode == "full" and built.base_sha == target
        assert built.source_unique_commits == 1 and built.target_unique_commits == 0
        [file] = built.parsed.files
        assert file.path == "app.py" and file.added_lines == {2, 3}
        # 末尾上下文行必须完整保留（Diff 输出不能被 strip）
        assert built.parsed.files[0].raw.endswith("\n")
    finally:
        manager.cleanup(prepared)
    assert not prepared.worktree_path.exists()
    # 第二次准备复用镜像（fetch 而不是重新 clone）
    manager.cleanup(manager.prepare(task))


def test_incremental_diff_and_force_push_fallback(settings, tmp_path):
    repo, target, first = feature_repo(tmp_path)
    (repo / "extra.py").write_text("x = 1\n")
    second = commit_all(repo, "extra")
    manager = RepositoryManager(settings)

    task = _task(repo, second, target, old_source_sha=first, action="update")
    prepared = manager.prepare(task)
    built = manager.build_diff(prepared, task)
    manager.cleanup(prepared)
    assert built.mode == "incremental" and [f.path for f in built.parsed.files] == ["extra.py"]

    # rebase 后旧提交不再包含 MR base：回退全量
    git(repo, "checkout", "-q", "main")
    (repo / "main.py").write_text("m\n")
    new_target = commit_all(repo, "main moves")
    git(repo, "checkout", "-q", "-b", "rebased", new_target)
    (repo / "app.py").write_text("def run():\n    return 2\n")
    rebased = commit_all(repo, "rebased")
    task = _task(repo, rebased, new_target, old_source_sha=first, action="update")
    prepared = manager.prepare(task)
    built = manager.build_diff(prepared, task)
    manager.cleanup(prepared)
    assert built.mode == "full"


def test_prepare_rejects_unknown_commit(settings, tmp_path):
    repo, target, _ = feature_repo(tmp_path)
    manager = RepositoryManager(settings)
    with pytest.raises(GitError, match="does not exist"):
        manager.prepare(_task(repo, "f" * 40, target))
    with pytest.raises(GitError, match="invalid"):
        manager.prepare(_task(repo, "not-a-sha", target))


def test_describe_local_commit(settings, tmp_path):
    repo, target, source = feature_repo(tmp_path)
    (repo / "sub").mkdir()
    commit = RepositoryManager(settings).describe_local_commit(str(repo / "sub"))
    assert (commit.repository, commit.name, commit.sha, commit.parent) == (str(repo), "demo", source, target)
    assert (commit.branch, commit.subject, commit.author) == ("feature/x", "change", "Test")
    root = RepositoryManager(settings).describe_local_commit(str(repo), target)
    assert root.parent == ""  # 根提交
    for path, revision in (("relative", "HEAD"), (str(tmp_path / "missing"), "HEAD"), (str(repo), "nope")):
        with pytest.raises(GitError):
            RepositoryManager(settings).describe_local_commit(path, revision)
