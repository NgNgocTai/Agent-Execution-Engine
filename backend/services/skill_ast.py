"""Static review signals; Docker remains the execution isolation boundary."""

import ast
from pathlib import PurePosixPath

MODULES = {"json", "sys", "math", "statistics", "datetime", "collections", "re"}
RESTRICTED = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "vars",
    "globals",
    "locals",
    "breakpoint",
    "input",
    "memoryview",
}
DANGEROUS_ATTRIBUTES = {
    "system",
    "popen",
    "execv",
    "execve",
    "spawnv",
    "spawnve",
    "fork",
    "modules",
    "environ",
    "getenv",
    "read_text",
    "write_text",
    "read_bytes",
    "write_bytes",
    "load_module",
    "find_spec",
    "mro",
    "gi_frame",
    "f_globals",
    "f_locals",
    "f_builtins",
    "cr_frame",
    "tb_frame",
}


def safe_relative(path):
    return (
        bool(path)
        and not path.startswith(("/", "~"))
        and "\\" not in path
        and ":" not in path
        and "\x00" not in path
        and ".." not in PurePosixPath(path).parts
        and not any(
            value in path.lower() for value in (".env", "id_rsa", "private_key")
        )
    )


def analyze_python(source, path):
    findings = []
    if len(source.encode("utf-8")) > 131072:
        return [
            {
                "path": path,
                "line": 1,
                "code": "AST_LIMIT",
                "message": "Python source exceeds the static review size limit",
            }
        ]

    def deny(node, code, message):
        if len(findings) < 100:
            findings.append(
                {
                    "path": path,
                    "line": getattr(node, "lineno", 1),
                    "code": code,
                    "message": message,
                }
            )

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            {
                "path": path,
                "line": exc.lineno or 1,
                "code": "PYTHON_SYNTAX",
                "message": "Python source cannot be parsed",
            }
        ]
    except (RecursionError, ValueError):
        return [
            {
                "path": path,
                "line": 1,
                "code": "PYTHON_SYNTAX",
                "message": "Python source cannot be parsed",
            }
        ]
    nodes = []
    for node in ast.walk(tree):
        if len(nodes) >= 20000:
            return [
                {
                    "path": path,
                    "line": 1,
                    "code": "AST_LIMIT",
                    "message": "Python source is too complex",
                }
            ]
        nodes.append(node)
    aliases = {"sys": "sys"}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
                if alias.name not in MODULES:
                    deny(
                        node,
                        "IMPORT_DENIED",
                        "Import is outside supported transformation modules",
                    )
        elif isinstance(node, ast.ImportFrom):
            if (
                node.level
                or node.module not in MODULES
                or any(alias.name == "*" for alias in node.names)
            ):
                deny(node, "IMPORT_DENIED", "Relative, wildcard or unsupported import")
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                if node.module == "sys" and alias.name not in {
                    "stdin",
                    "stdout",
                    "stderr",
                }:
                    deny(
                        node,
                        "SYS_CAPABILITY",
                        "Only standard input/output streams are available from sys",
                    )
        elif isinstance(node, ast.Attribute):
            if (
                node.attr.startswith("_")
                or node.attr in DANGEROUS_ATTRIBUTES
                or node.attr in RESTRICTED
            ):
                deny(
                    node,
                    "ATTRIBUTE_DENIED",
                    "Reflective, environment or process attribute is restricted",
                )
            if (
                isinstance(node.value, ast.Name)
                and aliases.get(node.value.id) == "sys"
                and node.attr not in {"stdin", "stdout", "stderr"}
            ):
                deny(
                    node,
                    "SYS_CAPABILITY",
                    "Only standard input/output streams are available from sys",
                )
        elif isinstance(node, ast.Name):
            if node.id in RESTRICTED or (
                node.id.startswith("__")
                and node.id not in {"__name__", "__doc__", "__file__"}
            ):
                deny(
                    node,
                    "NAME_DENIED",
                    "Dynamic execution or internal name is restricted",
                )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ):
            literal = lambda value: isinstance(value, ast.Constant) and isinstance(
                value.value, str
            )
            mode = node.args[1] if len(node.args) > 1 else ast.Constant(value="r")
            allowed = (
                bool(node.args)
                and len(node.args) <= 2
                and literal(node.args[0])
                and safe_relative(node.args[0].value)
            )
            for keyword in node.keywords:
                if keyword.arg == "mode":
                    mode = keyword.value
                elif keyword.arg != "encoding" or not literal(keyword.value):
                    allowed = False
            if not allowed or not literal(mode) or mode.value not in {"r", "rt", "rb"}:
                deny(
                    node,
                    "FILE_ACCESS_DENIED",
                    "open() requires a literal workspace path and read-only mode",
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(
                value in node.value.lower()
                for value in (
                    "/etc/passwd",
                    "/etc/shadow",
                    ".env",
                    "id_rsa",
                    "private_key",
                )
            ):
                deny(node, "SENSITIVE_PATH", "Source references a sensitive path")
    return findings
