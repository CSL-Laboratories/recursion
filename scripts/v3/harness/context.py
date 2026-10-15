"""Size-capped dependency context appended to a worker prompt."""

from __future__ import annotations


SUMMARY_LIMIT = 500
DIFF_LIMIT = 2000
CONTEXT_LIMIT = 6000


def task_prompt(task: str, dependencies: list | None) -> str:
    """Return the worker prompt. Dependency diffs are clipped first."""

    if not dependencies:
        return task
    section = dependency_context(dependencies)
    if not section:
        return task
    if not task:
        return section
    return task + "\n\n" + section


def dependency_context(dependencies: list) -> str:
    """Clip each dependency summary and diff, then the whole section."""

    blocks = []
    for dep in dependencies:
        if not isinstance(dep, dict):
            continue
        dep_id = dep.get("id")
        label = dep_id if isinstance(dep_id, str) and dep_id else "dependency"
        lines = [f"Task {label}:", "Summary: " + _clip(_summary(dep), SUMMARY_LIMIT)]
        commit = dep.get("commit")
        if isinstance(commit, str) and commit:
            lines.append(f"Commit: {commit}")
        diff = dep.get("diff") if isinstance(dep.get("diff"), str) else ""
        if diff.strip():
            lines.append("Diff:")
            lines.append(_clip(diff.strip(), DIFF_LIMIT))
        elif not isinstance(commit, str) or not commit:
            lines.append("Commit: none")
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    section = "Dependency context:\n" + "\n\n".join(blocks)
    if len(section) > CONTEXT_LIMIT:
        return section[:CONTEXT_LIMIT] + "\n[dependency context truncated]"
    return section


def _summary(dep: dict) -> str:
    text = dep.get("text")
    if isinstance(text, str) and text.strip():
        return " ".join(text.split())
    task = dep.get("task")
    if isinstance(task, str) and task.strip():
        return " ".join(task.split())
    return ""


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"
