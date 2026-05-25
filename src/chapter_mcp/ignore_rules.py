from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from pathlib import PurePosixPath


AiIgnoreRule = tuple[bool, bool, bool, str]


def resolve_git_root(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    git_root = result.stdout.strip()
    return Path(git_root).resolve() if git_root else None


def git_root_relative_files(root: Path, git_root: Path | None) -> list[Path] | None:
    if git_root is None:
        return None
    try:
        root_prefix = root.relative_to(git_root)
    except ValueError:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "ls-files", "--cached", "--others", "--exclude-standard", "--full-name"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    files: list[Path] = []
    for line in result.stdout.splitlines():
        repo_relative = Path(line)
        if root_prefix.parts:
            try:
                root_relative = repo_relative.relative_to(root_prefix)
            except ValueError:
                continue
        else:
            root_relative = repo_relative
        if root_relative.parts:
            files.append(root_relative)
    return files


class AiIgnoreMatcher:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[Path, tuple[AiIgnoreRule, ...]] = {}

    def matches(self, path: Path, category_dir: Path) -> bool:
        matched = False
        for base_dir in self._directories_for(path, category_dir):
            relative_path = path.relative_to(base_dir).as_posix()
            for negate, anchored, directory_only, pattern in self._load_rules(base_dir):
                if _matches_aiignore_rule(
                    pattern=pattern,
                    relative_path=relative_path,
                    anchored=anchored,
                    directory_only=directory_only,
                ):
                    matched = not negate
        return matched

    def _directories_for(self, path: Path, category_dir: Path) -> tuple[Path, ...]:
        candidate_dirs: list[Path] = []
        if category_dir.is_file():
            current = category_dir.parent
        else:
            current = path.parent
        lower_bound = self._search_root(category_dir)
        while True:
            try:
                current.relative_to(lower_bound)
            except ValueError:
                break
            candidate_dirs.append(current)
            if current == lower_bound:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
        candidate_dirs.reverse()
        return tuple(candidate_dirs)

    def _search_root(self, category_dir: Path) -> Path:
        candidate = category_dir if category_dir.is_dir() else category_dir.parent
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return candidate
        return self.root

    def _load_rules(self, directory: Path) -> tuple[AiIgnoreRule, ...]:
        cached = self._cache.get(directory)
        if cached is not None:
            return cached

        aiignore_path = directory / ".aiignore"
        if not aiignore_path.is_file():
            rules: tuple[AiIgnoreRule, ...] = ()
            self._cache[directory] = rules
            return rules

        parsed_rules: list[AiIgnoreRule] = []
        try:
            contents = aiignore_path.read_text(encoding="utf-8")
        except OSError:
            rules = ()
            self._cache[directory] = rules
            return rules
        for raw_line in contents.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            if not line:
                continue
            anchored = line.startswith("/")
            if anchored:
                line = line[1:]
            directory_only = line.endswith("/")
            if directory_only:
                line = line[:-1]
            if not line:
                continue
            parsed_rules.append((negate, anchored, directory_only, line))

        rules = tuple(parsed_rules)
        self._cache[directory] = rules
        return rules


def _matches_aiignore_rule(*, pattern: str, relative_path: str, anchored: bool, directory_only: bool) -> bool:
    normalized_path = PurePosixPath(relative_path)
    file_candidates = _relative_match_candidates(normalized_path, anchored=anchored)
    directory_candidates = _relative_match_candidates(normalized_path.parent, anchored=anchored)

    if "/" not in pattern:
        basename_candidates = [normalized_path.name]
        basename_candidates.extend(directory.name for directory in normalized_path.parents if directory.name)
        if directory_only:
            return any(fnmatch.fnmatchcase(name, pattern) for name in basename_candidates[1:])
        return any(fnmatch.fnmatchcase(name, pattern) for name in basename_candidates)

    if directory_only:
        return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in directory_candidates)
    return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in file_candidates)


def _relative_match_candidates(path: PurePosixPath, *, anchored: bool) -> tuple[str, ...]:
    if str(path) in {"", "."}:
        return ()
    full_path = path.as_posix()
    if anchored:
        return (full_path,)
    parts = path.parts
    return tuple("/".join(parts[index:]) for index in range(len(parts)))
