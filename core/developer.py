"""Developer assistant: source, search, diagnostics, docs and read-only Git.

Commands that write or execute are exposed only through confirmed tools. All
paths resolve inside ``[tools] root`` and subprocesses are started without a
shell or arbitrary command strings.
"""

from __future__ import annotations

import ast
import difflib
import os
import re
import shlex
import subprocess
import sys
import textwrap
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
TEXT_SUFFIXES = set(LANGUAGES) | {".txt", ".rst", ".ini", ".cfg", ".env.example"}
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
    """Raised when a developer operation cannot complete."""


class DeveloperService:
    """Bounded local developer operations with read-only defaults."""

    def __init__(self, config, tools_config):
        self._config = config
        self._root = tools_config.resolved_root

    def explain_source(self, path):
        target = self._read_target(path)
        content = self._read_text(target)
        relative = self._relative(target)
        lines = content.splitlines()
        suffix = target.suffix.lower()
        language = LANGUAGES.get(suffix, "text")
        data = {
            "path": relative,
            "language": language,
            "lines": len(lines),
            "bytes": len(content.encode("utf-8")),
            "classes": [],
            "functions": [],
            "imports": [],
            "docstring": "",
            "text": content,
        }
        report = [
            f"Source explanation for {relative}:",
            f"- Language: {language}; {len(lines)} line(s),"
            f" {len(content.encode('utf-8'))} bytes.",
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
        folder = self._directory(path)
        files, directories = self._scan(folder)
        extensions = Counter(entry.suffix.lower() or "(none)" for entry in files)
        python_files = [entry for entry in files if entry.suffix == ".py"]
        tests = [
            entry
            for entry in files
            if "test" in entry.name.lower() or "tests" in entry.parts
        ]
        top_level = self._top_level(folder)
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

    def search_code(self, query, path="."):
        needle = str(query or "").strip()
        if not needle:
            raise DeveloperError("a code search needs a word or pattern")
        target = self._resolve(path or ".")
        results = []
        scanned = 0
        for entry in self._text_files(target):
            if scanned >= self._config.max_files:
                break
            scanned += 1
            content = self._read_text(entry, required=False)
            if content is None:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if needle.lower() in line.lower():
                    results.append(
                        {
                            "path": self._relative(entry),
                            "line": number,
                            "text": line.strip()[:240],
                        }
                    )
                    if len(results) >= self._config.max_results:
                        break
            if len(results) >= self._config.max_results:
                break
        if not results:
            return f"No code matches for {needle!r}.", {"query": needle, "results": []}
        lines = [f"Code matches for {needle!r}:"]
        lines.extend(
            f"- {item['path']}:{item['line']}: {item['text']}" for item in results
        )
        return "\n".join(lines), {"query": needle, "results": results}

    def find_symbol(self, name, path="."):
        wanted = str(name or "").strip()
        if not wanted:
            raise DeveloperError("a function or class name is required")
        results = []
        for entry in self._python_files(self._resolve(path or ".")):
            content = self._read_text(entry, required=False)
            if content is None:
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for item in _symbols(tree):
                if item["name"].lower() == wanted.lower():
                    results.append({**item, "path": self._relative(entry)})
                    if len(results) >= self._config.max_results:
                        break
            if len(results) >= self._config.max_results:
                break
        if not results:
            return f"No function or class named {wanted!r} was found.", {
                "name": wanted,
                "results": [],
            }
        lines = [f"Symbols named {wanted!r}:"]
        for item in results:
            kind = item["kind"]
            parent = f" in {item['parent']}" if item.get("parent") else ""
            lines.append(
                f"- {kind} {item['name']}{parent} at"
                f" {item['path']}:{item['line']}"
            )
        return "\n".join(lines), {"name": wanted, "results": results}

    def explain_architecture(self, path="."):
        folder = self._directory(path)
        inspection_text, inspection = self.inspect_project(folder)
        modules = []
        external = Counter()
        for entry in self._python_files(folder):
            content = self._read_text(entry, required=False)
            if content is None:
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            relative = self._relative(entry)
            modules.append(relative)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    external.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    external.update([node.module.split(".")[0]])
        lines = ["Project architecture:"]
        lines.append(inspection_text.split("\n", 1)[-1])
        if modules:
            lines.append("- Python modules: " + ", ".join(modules[:20]) + ".")
        if external:
            lines.append(
                "- External imports: "
                + ", ".join(name for name, _count in external.most_common(12))
                + "."
            )
        if inspection["test_files"]:
            lines.append(
                f"- Test coverage surface: {inspection['test_files']} test file(s)."
            )
        data = {
            "path": inspection["path"],
            "modules": modules,
            "external_imports": dict(external),
            "inspection": inspection,
        }
        return "\n".join(lines), data

    def generate_snippet(self, language, description=""):
        kind = str(language or "python").strip().lower()
        detail = str(description or "").strip()
        safe = _identifier(detail) or "example"
        doc = _comment(detail or "Generated snippet")
        templates = {
            "python": textwrap.dedent(
                f'''\
                def {safe}(value):
                    """{doc}"""
                    if value is None:
                        raise ValueError("value is required")
                    return value
                '''
            ),
            "python-class": textwrap.dedent(
                f'''\
                class {safe.title().replace("_", "")}:
                    """{doc}"""

                    def __init__(self, value):
                        self.value = value
                '''
            ),
            "python-test": textwrap.dedent(
                f'''\
                import unittest


                class Test{safe.title().replace("_", "")}(unittest.TestCase):
                    def test_{safe}(self):
                        self.assertTrue(True)
                '''
            ),
            "javascript": textwrap.dedent(
                f'''\
                // {doc}
                function {safe}(value) {{
                  if (value == null) throw new Error("value is required");
                  return value;
                }}
                '''
            ),
            "typescript": textwrap.dedent(
                f'''\
                // {doc}
                export function {safe}(value: string): string {{
                  return value;
                }}
                '''
            ),
        }
        code = templates.get(kind)
        if code is None:
            raise DeveloperError(
                "supported snippet languages: python, python-class,"
                " python-test, javascript, typescript"
            )
        return f"Generated {kind} snippet:\n```{kind}\n{code.rstrip()}\n```", {
            "language": kind,
            "code": code.rstrip(),
        }

    def debug_error(self, error_text, source_path=""):
        text = str(error_text or "").strip()
        if not text:
            raise DeveloperError("an error message is required")
        exception, detail = _exception(text)
        explanation = _ERROR_HELP.get(exception, "The error name is not in the local hint table.")
        location = ""
        match = re.search(r'File "([^"]+)", line (\d+)', text)
        if match:
            location = f" at {match.group(1)}:{match.group(2)}"
        lines = [
            f"Error analysis: {exception}{location}.",
            f"- Meaning: {explanation}",
        ]
        if detail:
            lines.append(f"- Detail: {detail[:500]}")
        if source_path:
            try:
                data = self.explain_source(source_path)[1]
                if data.get("language") == "Python":
                    lines.append(
                        f"- Related source: {data['path']} has"
                        f" {data['lines']} lines; inspect the traceback line first."
                    )
            except DeveloperError:
                pass
        return "\n".join(lines), {"exception": exception, "detail": detail}

    def interpret_test_failure(self, output):
        text = str(output or "").strip()
        if not text:
            raise DeveloperError("test output is required")
        ran = re.search(r"Ran (\d+) tests?", text)
        failed = re.search(r"FAILED \((.*?)\)", text)
        problems = [
            line.strip()
            for line in text.splitlines()
            if line.startswith(("FAIL:", "ERROR:"))
        ]
        exception, detail = _exception(text)
        lines = ["Test failure interpretation:"]
        if ran:
            lines.append(f"- Tests run: {ran.group(1)}.")
        if failed:
            lines.append(f"- Result: FAILED ({failed.group(1)}).")
        elif "OK" in text:
            lines.append("- Result: the output reports success.")
        if problems:
            lines.append("- Failing tests: " + "; ".join(problems[:10]) + ".")
        lines.append(f"- Last exception: {exception}: {detail[:400] or 'none'}.")
        lines.append(f"- Likely cause: {_ERROR_HELP.get(exception, 'inspect the first traceback frame')}.")
        return "\n".join(lines), {
            "tests_run": int(ran.group(1)) if ran else 0,
            "failed": bool(failed),
            "failures": problems,
            "exception": exception,
        }

    def format_source(self, path, write=False):
        target = self._read_target(path)
        if target.suffix != ".py":
            raise DeveloperError("only Python source files are formatted")
        original = self._read_text(target)
        try:
            ast.parse(original)
        except SyntaxError as exc:
            raise DeveloperError(f"cannot format invalid Python: {exc.msg}") from exc
        formatted = _format_python(original)
        changed = formatted != original
        if write and changed:
            try:
                target.write_text(formatted, encoding="utf-8")
            except OSError as exc:
                raise DeveloperError(f"I could not write {target.name}: {exc}") from exc
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                formatted.splitlines(keepends=True),
                fromfile=str(self._relative(target)),
                tofile=str(self._relative(target)),
            )
        )
        text = (
            f"Formatted {self._relative(target)};"
            f" {'changes written' if write and changed else 'preview only'}."
        )
        if changed and diff:
            text += "\n```diff\n" + diff[:4000] + "```"
        return text, {
            "path": self._relative(target),
            "changed": changed,
            "written": bool(write and changed),
            "formatted": formatted,
        }

    def generate_documentation(self, path, output="", write=False):
        target = self._directory(path)
        files, _directories = self._scan(target)
        lines = [f"# {target.name} API documentation", ""]
        count = 0
        for entry in files:
            if entry.suffix != ".py":
                continue
            content = self._read_text(entry, required=False)
            if content is None:
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            module_doc = ast.get_docstring(tree)
            lines.append(f"## `{self._relative(entry)}`")
            if module_doc:
                lines.extend(["", module_doc, ""])
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    lines.append(f"### {kind} `{node.name}`")
                    doc = ast.get_docstring(node)
                    lines.append("")
                    lines.append(doc or "No docstring.")
                    lines.append("")
                    count += 1
        if count == 0:
            lines.append("No Python classes or functions were found.")
        text = "\n".join(lines).rstrip() + "\n"
        destination = self._optional_output(target, output, "docs.generated.md")
        if write:
            self._write_output(destination, text)
        return text, {
            "path": self._relative(target),
            "symbols": count,
            "output": self._relative(destination) if destination else "",
        }

    def generate_readme(self, path, output="README.generated.md", write=False):
        folder = self._directory(path)
        _text, data = self.inspect_project(folder)
        lines = [
            f"# {folder.name}",
            "",
            "Generated local project overview.",
            "",
            "## Structure",
            "",
            f"- Files: {data['files']}",
            f"- Python modules: {data['python_files']}",
            f"- Test files: {data['test_files']}",
            "",
            "## Top level",
            "",
        ]
        lines.extend(f"- `{item}`" for item in data["top_level"])
        lines.extend(
            [
                "",
                "## Configuration",
                "",
                "Document environment variables and local settings here.",
                "",
                "## Usage",
                "",
                "Add install and run instructions here.",
            ]
        )
        text = "\n".join(lines).rstrip() + "\n"
        destination = self._optional_output(folder, output, "README.generated.md")
        if write:
            self._write_output(destination, text)
        return text, {
            "path": self._relative(folder),
            "output": self._relative(destination) if destination else "",
            "written": bool(write),
        }

    def run_tests(self, path="."):
        self._require_execution()
        folder = self._directory(path)
        tests_dir = folder / "tests"
        start = "tests" if tests_dir.is_dir() else "."
        command = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            start,
            "-v",
        ]
        return self._run(command, folder, self._config.test_timeout_s)

    def run_script(self, path, arguments=""):
        self._require_execution()
        target = self._read_target(path)
        if target.suffix != ".py":
            raise DeveloperError("only Python scripts can be executed")
        args = shlex.split(str(arguments or ""))
        command = [sys.executable, "-I", str(target), *args]
        return self._run(command, self._root, self._config.script_timeout_s)

    def git_status(self):
        return self._git("status", "--short"), {"command": "git status"}

    def git_log(self, limit=10):
        count = max(1, min(int(limit), 50))
        return self._git("log", "--oneline", "-n", str(count)), {
            "command": "git log",
            "limit": count,
        }

    def git_changes(self):
        text = self._git("diff", "--stat")
        if not text.strip():
            text = "No unstaged changes."
        return text, {"command": "git diff --stat"}

    def commit_message(self):
        status = self._git("status", "--short")
        files = [line[3:].strip() for line in status.splitlines() if line.strip()]
        if not files:
            return "No changes to describe.", {"files": [], "message": ""}
        summary = ", ".join(files[:3])
        if len(files) > 3:
            summary += f", and {len(files) - 3} more"
        message = f"Update {len(files)} file(s): {summary}"
        return (
            "Suggested commit message:\n" + message,
            {"files": files, "message": message},
        )

    def search_documentation(self, query):
        needle = str(query or "").strip()
        if not needle:
            raise DeveloperError("a documentation search needs a word")
        results = []
        for entry in self._text_files(self._root):
            if entry.suffix not in {".md", ".rst", ".txt"}:
                continue
            content = self._read_text(entry, required=False)
            if content is None:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if needle.lower() in line.lower():
                    results.append(
                        {
                            "path": self._relative(entry),
                            "line": number,
                            "text": line.strip()[:240],
                        }
                    )
                    if len(results) >= self._config.max_results:
                        break
            if len(results) >= self._config.max_results:
                break
        if not results:
            return f"No local documentation matched {needle!r}.", {
                "query": needle,
                "results": [],
            }
        lines = [f"Documentation matches for {needle!r}:"]
        lines.extend(
            f"- {item['path']}:{item['line']}: {item['text']}" for item in results
        )
        return "\n".join(lines), {"query": needle, "results": results}

    def explain_terminal_error(self, error_text):
        return self.debug_error(error_text)

    def create_template(self, kind, name, path="."):
        kind = str(kind or "python").strip().lower()
        base = self._resolve(path or ".")
        project = base / (_identifier(name) or "example_project")
        if project.exists() and any(project.iterdir()):
            raise DeveloperError(f"{project.name} already exists and is not empty")
        project.mkdir(parents=True, exist_ok=True)
        if kind == "python-cli":
            (project / "main.py").write_text(
                '"""Command-line entry point."""\n\n\ndef main():\n'
                '    print("Hello from the project")\n\n\n'
                'if __name__ == "__main__":\n    main()\n',
                encoding="utf-8",
            )
            (project / "tests").mkdir(exist_ok=True)
            (project / "tests" / "test_main.py").write_text(
                "import unittest\n\n\nclass TestMain(unittest.TestCase):\n"
                "    def test_placeholder(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
        elif kind == "python-package":
            package = project / (_identifier(name) or "package")
            package.mkdir(exist_ok=True)
            (package / "__init__.py").write_text(
                '"""Package marker."""\n', encoding="utf-8"
            )
            (package / "core.py").write_text(
                '"""Core module."""\n\n\ndef run():\n    return "ready"\n',
                encoding="utf-8",
            )
            (project / "tests").mkdir(exist_ok=True)
        elif kind not in {"web", "empty"}:
            raise DeveloperError(
                "supported templates: python-cli, python-package, web, empty"
            )
        if kind == "web":
            (project / "index.html").write_text(
                "<!doctype html>\n<html lang=\"en\">\n<head>"
                "<meta charset=\"utf-8\"><title>Project</title></head>\n"
                "<body><h1>Project</h1></body>\n</html>\n",
                encoding="utf-8",
            )
        (project / "README.md").write_text(f"# {project.name}\n", encoding="utf-8")
        return f"Created {kind} template at {self._relative(project)}.", {
            "kind": kind,
            "path": self._relative(project),
            "files": [self._relative(item) for item in project.rglob("*") if item.is_file()],
        }

    def _run(self, command, cwd, timeout):
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=_clean_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise DeveloperError(f"command timed out after {timeout} seconds") from exc
        except OSError as exc:
            raise DeveloperError(f"could not start the command: {exc}") from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        output = output[: self._config.max_output_chars]
        summary = (
            f"Command finished with exit code {completed.returncode}:\n"
            f"{output or '(no output)'}"
        )
        return summary, {
            "returncode": completed.returncode,
            "output": output,
        }

    def _git(self, *args):
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                env=_clean_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeveloperError(f"Git is unavailable: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise DeveloperError(detail or "Git returned an error")
        return completed.stdout.strip() or "(no output)"

    def _require_execution(self):
        if not self._config.allow_execution:
            raise DeveloperError(
                "execution is disabled; set [developer] allow_execution = true"
            )

    def _explain_python(self, content, report, data):
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            report.append(f"- Python syntax error at line {exc.lineno}: {exc.msg}.")
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
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        classes = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [
                    child.name
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
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

    def _read_target(self, path):
        target = self._resolve(path)
        if not target.is_file():
            raise DeveloperError(f"{target.name} is not a source file I can read")
        return target

    def _directory(self, path):
        target = self._resolve(path or ".")
        if not target.is_dir():
            raise DeveloperError(f"{target.name} is not a folder")
        return target

    def _read_text(self, target, required=True):
        try:
            size = target.stat().st_size
        except OSError as exc:
            if not required:
                return None
            raise DeveloperError(f"I could not inspect {target.name}: {exc}") from exc
        if size > self._config.max_read_bytes:
            if not required:
                return None
            raise DeveloperError(
                f"{target.name} is larger than the {self._config.max_read_bytes}"
                " byte limit"
            )
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            if not required:
                return None
            raise DeveloperError(f"I could not read {target.name}: {exc}") from exc

    def _scan(self, folder):
        files = []
        directories = []
        for entry in _walk(folder, self._config.max_depth):
            if len(files) >= self._config.max_files:
                break
            if entry.is_dir():
                directories.append(entry)
            elif entry.is_file():
                files.append(entry)
        return files, directories

    def _python_files(self, target):
        if target.is_file():
            if target.suffix == ".py":
                yield target
            return
        count = 0
        for entry in _walk(target, self._config.max_depth):
            if entry.is_file() and entry.suffix == ".py":
                yield entry
                count += 1
                if count >= self._config.max_files:
                    return

    def _text_files(self, target):
        if target.is_file():
            if target.suffix.lower() in TEXT_SUFFIXES:
                yield target
            return
        for entry in _walk(target, self._config.max_depth):
            if entry.is_file() and entry.suffix.lower() in TEXT_SUFFIXES:
                yield entry

    def _top_level(self, folder):
        try:
            return sorted(
                [entry.name + ("/" if entry.is_dir() else "") for entry in folder.iterdir()],
                key=str.lower,
            )[:20]
        except OSError:
            return []

    def _optional_output(self, base, output, default):
        if not output:
            return base / default
        candidate = Path(str(output)).expanduser()
        if candidate.is_absolute():
            return self._resolve(output)
        resolved = (base / candidate).resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise DeveloperError(
                f"{output} is outside the allowed folder ({self._root})"
            )
        return resolved

    def _write_output(self, destination, text):
        if destination != self._root and self._root not in destination.parents:
            raise DeveloperError("the output path is outside the allowed folder")
        if destination.exists():
            raise DeveloperError(f"{destination.name} already exists")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise DeveloperError(f"I could not write {destination.name}: {exc}") from exc

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
            return str(path.relative_to(self._root)).replace("\\", "/")
        except ValueError:
            return path.name


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


def _symbols(tree, parent=""):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield {
                "name": node.name,
                "kind": "class",
                "line": getattr(node, "lineno", 0),
                "parent": parent,
            }
            yield from _symbols(node, node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield {
                "name": node.name,
                "kind": "function",
                "line": getattr(node, "lineno", 0),
                "parent": parent,
            }
            yield from _symbols(node, f"{parent}.{node.name}".strip("."))
        else:
            yield from _symbols(node, parent)


def _identifier(value):
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "task_" + text
    return text.lower()


def _comment(value):
    return " ".join(str(value or "Generated snippet").split()).replace('"', "'")


def _clean_env():
    secret_words = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in secret_words)
    }
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _format_python(content):
    lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    cleaned = []
    for line in lines:
        line = line.rstrip()
        leading = len(line) - len(line.lstrip("\t"))
        if leading:
            line = "    " * leading + line[leading:]
        cleaned.append(line)
    return "\n".join(cleaned).rstrip() + "\n"


def _exception(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        match = re.match(
            r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::\s*(?P<detail>.*))?$",
            line,
        )
        if match:
            return match.group("name").split(".")[-1], match.group("detail") or ""
    return "UnknownError", lines[-1] if lines else ""


_ERROR_HELP = {
    "SyntaxError": "Python could not parse the file; check the reported line and indentation.",
    "IndentationError": "A block has inconsistent leading whitespace.",
    "ModuleNotFoundError": "The module is not installed or not on PYTHONPATH.",
    "ImportError": "An import target is missing or the package structure is wrong.",
    "NameError": "A variable or function is used before it is defined.",
    "TypeError": "An operation received an incompatible type or argument count.",
    "ValueError": "The value is the right type but outside the accepted range or format.",
    "KeyError": "A dictionary key is absent; use dict.get or check membership.",
    "IndexError": "A sequence index is outside its valid range.",
    "FileNotFoundError": "The requested path does not exist from the current working directory.",
    "AttributeError": "The object does not expose the requested attribute.",
    "PermissionError": "The process lacks permission for the file or operation.",
    "ConnectionError": "A network connection failed or was refused.",
    "TimeoutError": "The operation exceeded its allowed time.",
    "JSONDecodeError": "The input is not valid JSON.",
}
