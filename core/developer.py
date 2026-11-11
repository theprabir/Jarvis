"""Read-only developer assistant: source explanation and project inspection."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

LANGUAGES = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript React",
    ".jsx": "JavaScript React",
    ".java": "Java",
    ".cs": "C#",
    ".c": "C",
    ".h": "C header",
    ".cpp": "C++",
    ".rs": "Rust",
    ".go": "Go",
    ".rb": "Ruby",
    ".php": "PHP",
    ".md": "Markdown",
    ".toml": "TOML",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".html": "HTML",
    ".css": "CSS",
    ".sql": "SQL",
}
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "models",
    "cache",
    "logs",
    "screenshots",
    ".freebuff",
}


class DeveloperError(Exception):
    """Raised when source or project inspection cannot complete."""


class DeveloperService:
    """Read-only source summaries and bounded project inspection."""

    def __init__(self, config, tools_config):
        self._config = config
        self._root = tools_config.resolved_root

    def explain_source(self, path):
        target = self._resolve(path)
        if not target.is_file():
            raise DeveloperError(f"{target.name} is not a source file I can read")
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise DeveloperError(f"I could not inspect {target.name}: {exc}") from exc
        if size > self._config.max_read_bytes:
            raise DeveloperError(
                f"{target.name} is larger than the {self._config.max_read_bytes}"
                " byte source limit"
            )
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise DeveloperError(f"I could not read {target.name}: {exc}") from exc
        relative = self._relative(target)
        lines = content.splitlines()
        suffix = target.suffix.lower()
        language = LANGUAGES.get(suffix, "text")
        data = {
            "path": relative,
            "language": language,
            "lines": len(lines),
            "bytes": size,
            "classes": [],
            "functions": [],
            "imports": [],
            "docstring": "",
            "text": content,
        }
        report = [
            f"Source explanation for {relative}:",
            f"- Language: {language}; {len(lines)} line(s), {size} bytes.",
        ]
        if suffix == ".py":
            self._explain_python(content, report, data)
        else:
            preview = [
                line.strip()
                for line in lines
                if line.strip() and not line.lstrip().startswith(("#", "//", "/*"))
            ][:5]
            if preview:
                report.append("- Preview: " + " ".join(preview)[:300])
            report.append("- Non-Python files use a text overview.")
        return "\n".join(report), data

    def inspect_project(self, path="."):
        folder = self._resolve(path or ".")
        if not folder.is_dir():
            raise DeveloperError(f"{folder.name} is not a project folder")
        files = []
        directories = []
        for entry in _walk(folder, self._config.max_depth):
            if len(files) >= self._config.max_files:
                break
            if entry.is_dir():
                directories.append(entry)
            elif entry.is_file():
                files.append(entry)
        extensions = Counter(
            entry.suffix.lower() or "(none)" for entry in files
        )
        python_files = [entry for entry in files if entry.suffix == ".py"]
        tests = [
            entry
            for entry in files
            if "test" in entry.name.lower() or "tests" in entry.parts
        ]
        top_level = []
        try:
            top_level = sorted(
                [entry.name + ("/" if entry.is_dir() else "") for entry in folder.iterdir()],
                key=str.lower,
            )[:20]
        except OSError:
            pass
        important = [
            name
            for name in ("README.md", "pyproject.toml", "requirements.txt", "main.py")
            if (folder / name).exists()
        ]
        relative = self._relative(folder) or folder.name
        report = [
            f"Project inspection for {relative}:",
            f"- Files: {len(files)}; directories: {len(directories)};"
            f" Python modules: {len(python_files)}.",
        ]
        if top_level:
            report.append("- Top level: " + ", ".join(top_level) + ".")
        if extensions:
            report.append(
                "- File types: "
                + ", ".join(
                    f"{suffix} ({count})"
                    for suffix, count in extensions.most_common(8)
                )
                + "."
            )
        report.append(f"- Test files: {len(tests)}.")
        if important:
            report.append("- Entry/configuration files: " + ", ".join(important) + ".")
        if len(files) >= self._config.max_files:
            report.append(
                f"- Scan stopped at the {self._config.max_files}-file limit."
            )
        data = {
            "path": relative,
            "files": len(files),
            "directories": len(directories),
            "python_files": len(python_files),
            "test_files": len(tests),
            "top_level": top_level,
            "important": important,
            "extensions": dict(extensions),
        }
        return "\n".join(report), data

    def _explain_python(self, content, report, data):
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            report.append(
                f"- Python syntax error at line {exc.lineno}: {exc.msg}."
            )
            return
        docstring = ast.get_docstring(tree) or ""
        data["docstring"] = docstring.splitlines()[0] if docstring else ""
        if docstring:
            report.append(f"- Module: {data['docstring']}")
        imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        functions = [
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        ]
        classes = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [
                    child.name
                    for child in node.body
                    if isinstance(child, ast.FunctionDef)
                ]
                classes.append((node.name, methods))
        data["imports"] = imports
        data["functions"] = functions
        data["classes"] = [
            {"name": name, "methods": methods} for name, methods in classes
        ]
        if imports:
            report.append("- Imports: " + ", ".join(imports[:20]) + ".")
        if functions:
            report.append("- Functions: " + ", ".join(functions) + ".")
        for name, methods in classes:
            method_text = ", ".join(methods) if methods else "no methods"
            report.append(f"- Class {name}: {method_text}.")

    def _resolve(self, raw):
        text = str(raw or "").strip().strip('"').strip("'")
        if not text:
            raise DeveloperError("a source or project path is required")
        path = Path(text).expanduser()
        candidate = path if path.is_absolute() else self._root / path
        resolved = candidate.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise DeveloperError(
                f"{text} is outside the allowed folder ({self._root})"
            )
        return resolved

    def _relative(self, path):
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            return path.name
        return str(relative).replace("\\", "/")


def _walk(root, max_depth):
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name.lower() in SKIP_DIRS:
                continue
            yield entry
            if entry.is_dir() and depth + 1 < max_depth:
                stack.append((entry, depth + 1))
