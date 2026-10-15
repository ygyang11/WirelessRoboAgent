#!/usr/bin/env python3
"""Check repository configuration and benchmark tasks."""

from __future__ import annotations

import argparse
import configparser
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    sys.exit("PyYAML is required. Install it with: python -m pip install pyyaml")

ROOT = Path(__file__).resolve().parents[2]
AGENT = "wirelessroboagent/arktor"
BENCH = "wirelessrobobench/arktor-bench-selfevolution"
BRANCH = "research/wireless-robo-agent"
SUBMODULES = {
    "Arktor": (AGENT, "https://github.com/ygyang11/Arktor.git"),
    "Arktor-Bench-SelfEvolution": (
        BENCH,
        "https://github.com/ygyang11/Arktor-Bench-SelfEvolution.git",
    ),
}
EDITABLES = {".": {"dev"}, AGENT: {"dev", "app", "cli"}, BENCH: {"dev"}}
TASK_COUNT = 60


class CheckError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def read_text(path: Path) -> str:
    label = path.relative_to(ROOT)
    require(path.is_file(), f"Missing file: {label}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CheckError(f"{label}: {exc}") from exc


def mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CheckError(f"{label}: expected a mapping")
    return value


def read_project(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(read_text(path))
    except tomllib.TOMLDecodeError as exc:
        raise CheckError(f"{path.relative_to(ROOT)}: {exc}") from exc
    return mapping(data.get("project"), f"{path.relative_to(ROOT)} [project]")


def read_frontmatter(path: Path) -> dict[str, Any]:
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\s*\Z)", read_text(path), re.DOTALL)
    if match is None:
        raise CheckError(f"{path.relative_to(ROOT)}: missing YAML frontmatter")
    try:
        metadata = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise CheckError(f"{path.relative_to(ROOT)}: {exc}") from exc
    return mapping(metadata, str(path.relative_to(ROOT)))


def git(directory: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CheckError("Git is required but was not found on PATH") from exc
    detail = result.stderr.strip() or "Git command failed"
    require(result.returncode == 0, f"{directory}: {detail}")
    return result.stdout.strip()


def check_project() -> str:
    for name in (".gitignore", ".gitmodules", "LICENSE", "environment.yaml"):
        read_text(ROOT / name)
    project = read_project(ROOT / "pyproject.toml")
    require(project.get("name") == "wirelessroboagent", "Unexpected root project name")
    readme = project.get("readme")
    if isinstance(readme, str):
        read_text(ROOT / readme)
    elif isinstance(readme, dict):
        require(len({"file", "text"} & readme.keys()) == 1,
                "[project.readme] must contain either file or text")
        field = "file" if "file" in readme else "text"
        require(isinstance(readme[field], str), f"[project.readme].{field} must be a string")
        if field == "file":
            read_text(ROOT / readme[field])
    else:
        require(readme is None, "[project.readme] must be a path or a table")
    return "files and metadata"


def check_submodules() -> str:
    require(Path(git(ROOT, "rev-parse", "--show-toplevel")).resolve() == ROOT,
            "The script must belong to the parent Git working tree")
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(read_text(ROOT / ".gitmodules"))
    sections = {f'submodule "{name}"' for name in SUBMODULES}
    require(set(config.sections()) == sections, ".gitmodules: unexpected submodule sections")
    for name, (relative, url) in SUBMODULES.items():
        section = config[f'submodule "{name}"']
        for field, expected in (("path", relative), ("url", url), ("branch", BRANCH)):
            require(section.get(field) == expected, f"{name}: expected {field} = {expected}")
        directory = ROOT / relative
        require((directory / ".git").exists(),
                f"{relative}: run git submodule update --init --recursive")
        actual_root = Path(git(directory, "rev-parse", "--show-toplevel")).resolve()
        require(actual_root == directory.resolve(), f"{relative}: not a submodule working tree")
        record = git(ROOT, "ls-tree", "HEAD", "--", relative)
        metadata, separator, recorded_path = record.partition("\t")
        fields = metadata.split()
        require(separator == "\t" and recorded_path == relative and len(fields) == 3,
                f"{relative}: no gitlink in the parent commit")
        require(fields[:2] == ["160000", "commit"], f"{relative}: expected a gitlink")
        actual = git(directory, "rev-parse", "HEAD")
        require(actual == fields[2],
                f"{relative}: HEAD {actual} differs from parent gitlink {fields[2]}")
    return "configuration and pinned commits"


def check_environment() -> str:
    environment = mapping(yaml.safe_load(read_text(ROOT / "environment.yaml")), "environment.yaml")
    dependencies = environment.get("dependencies")
    if not isinstance(dependencies, list):
        raise CheckError("environment.yaml: dependencies must be a list")
    groups = [item["pip"] for item in dependencies if isinstance(item, dict) and "pip" in item]
    require(len(groups) == 1 and isinstance(groups[0], list),
            "environment.yaml: expected one pip dependency list")
    found: set[str] = set()
    for entry in groups[0]:
        require(isinstance(entry, str), "environment.yaml: pip requirements must be strings")
        words = shlex.split(entry)
        if not words:
            continue
        if words[0].startswith("--editable="):
            require(len(words) == 1, f"Invalid editable requirement: {entry}")
            target = words[0].split("=", 1)[1]
        elif words[0] in {"-e", "--editable"}:
            require(len(words) == 2, f"Invalid editable requirement: {entry}")
            target = words[1]
        else:
            continue
        match = re.fullmatch(r"([^\[\]]+)(?:\[([^\[\]]+)\])?", target)
        if match is None:
            raise CheckError(f"Invalid editable requirement: {entry}")
        path = Path(match[1])
        require(not path.is_absolute() and ".." not in path.parts,
                f"Editable paths must be relative to the repository: {entry}")
        relative = path.as_posix()
        require(relative in EDITABLES, f"Unexpected editable path: {relative}")
        require(relative not in found, f"Duplicate editable path: {relative}")
        found.add(relative)
        names = [extra.strip() for extra in match[2].split(",")] if match[2] else []
        require(all(names), f"Invalid extras in editable requirement: {entry}")
        extras = {re.sub(r"[-_.]+", "-", extra).lower() for extra in names}
        missing = EDITABLES[relative] - extras
        require(not missing, f"{relative}: missing required extras {sorted(missing)}")
        project = read_project(ROOT / relative / "pyproject.toml")
        available = mapping(project.get("optional-dependencies", {}), f"{relative}: extras")
        available_names = {re.sub(r"[-_.]+", "-", extra).lower() for extra in available}
        unknown = extras - available_names
        require(not unknown, f"{relative}: unknown extras {sorted(unknown)}")
    missing_paths = EDITABLES.keys() - found
    require(not missing_paths, f"Missing editable installations: {sorted(missing_paths)}")
    return "editable paths and extras"


def check_tasks() -> str:
    pack = ROOT / BENCH / "packs/wireless_robot"
    require(pack.is_dir(), f"Missing task pack: {pack.relative_to(ROOT)}")
    tasks = sorted(
        path for path in pack.iterdir()
        if path.is_dir() and (path.name.startswith("T") or (path / "task.md").is_file())
    )
    require(len(tasks) == TASK_COUNT, f"Expected {TASK_COUNT} tasks, found {len(tasks)}")
    for index, directory in enumerate(tasks, start=1):
        prefix, separator, slug = directory.name.partition("_")
        require(prefix == f"T{index:03d}" and separator == "_" and bool(slug),
                f"Expected task T{index:03d}_<name>, found {directory.name}")
        task = read_frontmatter(directory / "task.md")
        require(task.get("id") == directory.name,
                f"{directory.name}: task ID differs from directory")
        require(isinstance(task.get("name"), str) and bool(task["name"].strip()),
                f"{directory.name}: missing task name")
        read_text(directory / "grader.py")
    return f"{TASK_COUNT} tasks, consecutive IDs and required files"


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    checks = (
        ("Project", check_project),
        ("Submodules", check_submodules),
        ("Environment", check_environment),
        ("Tasks", check_tasks),
    )
    failures = 0
    for name, check in checks:
        try:
            detail = check()
        except (CheckError, OSError, ValueError, configparser.Error,
                yaml.YAMLError, subprocess.SubprocessError) as exc:
            print(f"[FAIL] {name}: {exc}", file=sys.stderr)
            failures += 1
        else:
            print(f"[PASS] {name}: {detail}", flush=True)
    print(f"\n{len(checks) - failures}/{len(checks)} repository checks passed.", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
