"""Small AST helpers shared by the checks. Code is parsed, never imported or run."""

from __future__ import annotations

import ast


def parse(text: str | None, path: str = "<file>"):
    if not text:
        return None
    try:
        return ast.parse(text, filename=path)
    except SyntaxError:
        return None


def call_name(node: ast.Call) -> str:
    f, parts = node.func, []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    elif isinstance(f, ast.Call):
        parts.append(call_name(f) + "()")
    return ".".join(reversed(parts))


def kw(node: ast.Call, name: str):
    return next((k.value for k in node.keywords if k.arg == name), None)


def is_const(node, value=...) -> bool:
    return isinstance(node, ast.Constant) and (
        value is ... or node.value is value or node.value == value
    )


def is_dynamic_str(node) -> bool:
    """f-string with a value, concatenation, %-formatting or .format()."""
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    )


def names_in(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def functions(tree) -> list:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def touches(node, added: set[int]) -> bool:
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    return any(node.lineno <= n <= end for n in added)


def main_guard_lines(tree, text: str) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "__main__" in (
            ast.get_source_segment(text, node.test) or ""
        ):
            out |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return out


def enclosing_class_names(tree) -> dict[int, str]:
    """Line -> name of the class that contains it (innermost)."""
    out: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for n in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                out[n] = node.name
    return out


def body_is_stub(fn) -> bool:
    body = [
        s
        for s in fn.body
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]  # drop the docstring
    if not body:
        return True
    return all(
        isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and is_const(s.value, Ellipsis))
        for s in body
    )


def is_abstract(fn, tree) -> bool:
    for dec in fn.decorator_list:
        name = dec.attr if isinstance(dec, ast.Attribute) else getattr(dec, "id", "")
        if name in ("abstractmethod", "overload", "abstractproperty"):
            return True
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and fn in node.body:
            bases = {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases}
            return bool(bases & {"Protocol", "ABC", "ABCMeta"})
    return False
