"""
Tree-sitter based AST extractor.

Given a list of source files, produces structured `ASTFindings`:
  - symbols (functions, classes, methods) with file+line
  - call-graph edges (caller → callee) inside the scanned set
  - HTTP routes (FastAPI @app.get/@router.post, Flask @app.route, Spring
    @RestController / @RequestMapping) with path+method+handler
  - DB call sites (sqlalchemy session.query, cursor.execute, JDBC/JPA
    Repository interfaces) — heuristic, good enough for a migration risk
    ranking
  - static findings — deep nesting, bare except, long functions, TODO markers

Design rules:
  - Pure function of (files on disk, encoding). Same input → same output.
  - Failures per-file are captured in `ASTFindings.errors`; one bad file
    never blocks the rest.
  - Only Python and Java wired for now (matches Galaxy's migration scope).
    Unknown extensions are skipped with a note in `errors`.
  - We do NOT inline source bodies in findings — just names and line
    numbers — keeping the A2A payload small and ledger-safe.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from tree_sitter import Language, Node, Parser
    import tree_sitter_python as _tsp
    import tree_sitter_java as _tsj
    _TS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TS_AVAILABLE = False


# ── Public schema ─────────────────────────────────────────────────────────────

@dataclass
class Symbol:
    name: str
    kind: str            # "function" | "method" | "class"
    file: str
    line: int
    parent: Optional[str] = None   # enclosing class name for methods


@dataclass
class CallEdge:
    caller: str          # qualified symbol name or "<module>"
    callee: str          # raw callee text (may be attribute access)
    file: str
    line: int


@dataclass
class Route:
    framework: str       # "fastapi" | "flask" | "spring"
    method: str          # GET|POST|PUT|DELETE|ANY
    path: str
    handler: str         # enclosing function/method name
    file: str
    line: int


@dataclass
class DBCall:
    kind: str            # "sqlalchemy" | "cursor" | "jpa_repository" | "jdbc"
    snippet: str         # short (≤120 chars) snippet for context
    file: str
    line: int


@dataclass
class Finding:
    rule: str            # "deep_nesting" | "bare_except" | "long_function" | "todo"
    message: str
    file: str
    line: int
    severity: str = "info"    # "info" | "warn" | "risk"


@dataclass
class ASTFindings:
    language: str
    files_analyzed: int
    files_skipped: int
    symbols: list = field(default_factory=list)
    call_edges: list = field(default_factory=list)
    routes: list = field(default_factory=list)
    db_calls: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # [(file, reason), ...]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def counts(self) -> dict:
        return {
            "symbols":    len(self.symbols),
            "call_edges": len(self.call_edges),
            "routes":     len(self.routes),
            "db_calls":   len(self.db_calls),
            "findings":   len(self.findings),
            "errors":     len(self.errors),
        }


# ── Entry point ───────────────────────────────────────────────────────────────

_MAX_FILE_BYTES = 256_000      # don't try to AST-parse huge generated files
_LONG_FUNCTION_LINES = 50
_DEEP_NESTING_LEVELS = 5


def extract_ast(repo_root: str, files: list[str]) -> ASTFindings:
    """
    Parse the given files (paths relative to repo_root) and extract
    structured findings. Missing / unreadable files are recorded in
    `errors` rather than raised.
    """
    if not _TS_AVAILABLE:
        return ASTFindings(
            language="unknown",
            files_analyzed=0,
            files_skipped=len(files),
            errors=[(f, "tree-sitter not installed") for f in files],
        )

    root = Path(repo_root).resolve()
    py_parser = Parser(Language(_tsp.language()))
    java_parser = Parser(Language(_tsj.language()))

    out = ASTFindings(language="unknown", files_analyzed=0, files_skipped=0)
    lang_counts: dict[str, int] = {}

    for rel in files:
        full = (root / rel).resolve()
        try:
            if not full.is_file():
                out.errors.append((rel, "not a file"))
                out.files_skipped += 1
                continue
            size = full.stat().st_size
            if size > _MAX_FILE_BYTES:
                out.errors.append((rel, f"file too large ({size} bytes)"))
                out.files_skipped += 1
                continue
            source = full.read_bytes()
        except OSError as e:
            out.errors.append((rel, f"read failed: {e}"))
            out.files_skipped += 1
            continue

        suffix = full.suffix.lower()
        if suffix == ".py":
            _extract_python(py_parser, source, rel, out)
            lang_counts["python"] = lang_counts.get("python", 0) + 1
            out.files_analyzed += 1
        elif suffix == ".java":
            _extract_java(java_parser, source, rel, out)
            lang_counts["java"] = lang_counts.get("java", 0) + 1
            out.files_analyzed += 1
        else:
            out.errors.append((rel, f"unsupported extension {suffix}"))
            out.files_skipped += 1

    if lang_counts:
        out.language = max(lang_counts.items(), key=lambda kv: kv[1])[0]
    return out


# ── Python extraction ─────────────────────────────────────────────────────────

_PY_ROUTE_DECORATORS = {
    "get":     ("fastapi", "GET"),
    "post":    ("fastapi", "POST"),
    "put":     ("fastapi", "PUT"),
    "delete":  ("fastapi", "DELETE"),
    "patch":   ("fastapi", "PATCH"),
    "route":   ("flask",   "ANY"),
}


def _extract_python(parser: "Parser", source: bytes, rel: str, out: ASTFindings) -> None:
    tree = parser.parse(source)
    source_text = source.decode("utf-8", errors="replace")
    _walk_python(tree.root_node, source, source_text, rel, out,
                 class_stack=[], func_stack=[])


def _walk_python(
    node: "Node",
    source: bytes,
    source_text: str,
    rel: str,
    out: ASTFindings,
    class_stack: list[str],
    func_stack: list[str],
) -> None:
    t = node.type

    if t == "class_definition":
        name = _py_name(node, source)
        if name:
            out.symbols.append(Symbol(
                name=name, kind="class", file=rel, line=node.start_point[0] + 1,
                parent=class_stack[-1] if class_stack else None,
            ))
            class_stack = class_stack + [name]

    elif t == "function_definition":
        name = _py_name(node, source)
        if name:
            kind = "method" if class_stack else "function"
            parent = class_stack[-1] if class_stack else None
            qualified = f"{parent}.{name}" if parent else name
            out.symbols.append(Symbol(
                name=name, kind=kind, file=rel,
                line=node.start_point[0] + 1, parent=parent,
            ))
            _scan_py_decorators(node, source, qualified, rel, out)
            _scan_py_function_body(node, source, source_text, qualified, rel, out)
            func_stack = func_stack + [qualified]

    elif t == "call":
        callee = _py_call_target(node, source)
        if callee:
            caller = func_stack[-1] if func_stack else "<module>"
            out.call_edges.append(CallEdge(
                caller=caller, callee=callee, file=rel,
                line=node.start_point[0] + 1,
            ))
            _maybe_record_py_dbcall(callee, node, source, rel, out)

    elif t == "except_clause":
        # bare `except:` has no exception type child — only the keyword + ':'
        has_type = any(
            c.type not in ("except", ":", "block", "comment")
            for c in node.children
        )
        if not has_type:
            out.findings.append(Finding(
                rule="bare_except",
                message="bare 'except:' swallows all exceptions",
                file=rel, line=node.start_point[0] + 1, severity="warn",
            ))

    elif t == "comment":
        text = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", text):
            out.findings.append(Finding(
                rule="todo", message=text.strip()[:120],
                file=rel, line=node.start_point[0] + 1, severity="info",
            ))

    for child in node.children:
        _walk_python(child, source, source_text, rel, out, class_stack, func_stack)


def _py_name(node: "Node", source: bytes) -> Optional[str]:
    ident = node.child_by_field_name("name")
    if ident is None:
        return None
    return source[ident.start_byte:ident.end_byte].decode("utf-8", errors="replace")


def _py_call_target(call_node: "Node", source: bytes) -> Optional[str]:
    func = call_node.child_by_field_name("function")
    if func is None:
        return None
    text = source[func.start_byte:func.end_byte].decode("utf-8", errors="replace")
    # Trim to a reasonable size so the envelope stays small.
    return text[:120]


def _scan_py_decorators(func_node: "Node", source: bytes, qualified: str,
                         rel: str, out: ASTFindings) -> None:
    """Look at the function's decorated parent to find @app.get/@router.post etc."""
    parent = func_node.parent
    if parent is None or parent.type != "decorated_definition":
        return
    for child in parent.children:
        if child.type != "decorator":
            continue
        deco_text = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
        m = re.match(r"@(?:\w+\.)?(\w+)\s*\(\s*['\"]([^'\"]+)['\"]", deco_text)
        if not m:
            continue
        deco_name, path = m.group(1), m.group(2)
        info = _PY_ROUTE_DECORATORS.get(deco_name)
        if not info:
            continue
        framework, method = info
        # Flask @app.route(..., methods=['POST']) — pull method from methods=
        if framework == "flask":
            mm = re.search(r"methods\s*=\s*\[([^\]]+)\]", deco_text)
            if mm:
                method = re.sub(r"['\"\s]", "", mm.group(1).split(",")[0]).upper()
        out.routes.append(Route(
            framework=framework, method=method, path=path,
            handler=qualified, file=rel, line=child.start_point[0] + 1,
        ))


def _scan_py_function_body(func_node: "Node", source: bytes, source_text: str,
                           qualified: str, rel: str, out: ASTFindings) -> None:
    body = func_node.child_by_field_name("body")
    if body is None:
        return
    start_line = body.start_point[0] + 1
    end_line = body.end_point[0] + 1
    if end_line - start_line >= _LONG_FUNCTION_LINES:
        out.findings.append(Finding(
            rule="long_function",
            message=f"{qualified} spans {end_line - start_line + 1} lines",
            file=rel, line=func_node.start_point[0] + 1, severity="info",
        ))
    depth = _max_nesting_depth(body)
    if depth >= _DEEP_NESTING_LEVELS:
        out.findings.append(Finding(
            rule="deep_nesting",
            message=f"{qualified} nests to depth {depth}",
            file=rel, line=func_node.start_point[0] + 1, severity="warn",
        ))


_PY_NESTING_TYPES = {
    "if_statement", "for_statement", "while_statement", "try_statement",
    "with_statement", "match_statement",
}


def _max_nesting_depth(node: "Node", current: int = 0) -> int:
    next_depth = current + (1 if node.type in _PY_NESTING_TYPES else 0)
    best = next_depth
    for child in node.children:
        best = max(best, _max_nesting_depth(child, next_depth))
    return best


_PY_DB_HINTS = (
    ("cursor.execute", "cursor"),
    (".execute(",      "cursor"),
    ("session.query",  "sqlalchemy"),
    ("session.add",    "sqlalchemy"),
    ("session.commit", "sqlalchemy"),
    ("db.session",     "sqlalchemy"),
)


def _maybe_record_py_dbcall(callee: str, node: "Node", source: bytes,
                            rel: str, out: ASTFindings) -> None:
    for needle, kind in _PY_DB_HINTS:
        if needle in callee:
            line = node.start_point[0] + 1
            # Chained calls (session.query(...).filter_by(...).first()) hit this
            # branch once per link. Keep only the first — outermost — match per
            # (file, line, kind) so the envelope stays small.
            if any(d.file == rel and d.line == line and d.kind == kind
                   for d in out.db_calls):
                return
            snippet = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
            out.db_calls.append(DBCall(
                kind=kind, snippet=snippet[:120].replace("\n", " "),
                file=rel, line=line,
            ))
            return


# ── Java extraction ───────────────────────────────────────────────────────────

_JAVA_SPRING_HTTP = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
    "RequestMapping": "ANY",
}


def _extract_java(parser: "Parser", source: bytes, rel: str, out: ASTFindings) -> None:
    tree = parser.parse(source)
    _walk_java(tree.root_node, source, rel, out, class_stack=[], method_stack=[])


def _walk_java(
    node: "Node",
    source: bytes,
    rel: str,
    out: ASTFindings,
    class_stack: list[str],
    method_stack: list[str],
) -> None:
    t = node.type

    if t == "class_declaration" or t == "interface_declaration":
        name = _java_name(node, source)
        if name:
            out.symbols.append(Symbol(
                name=name, kind="class", file=rel,
                line=node.start_point[0] + 1,
                parent=class_stack[-1] if class_stack else None,
            ))
            _scan_java_repository(node, name, source, rel, out)
            class_stack = class_stack + [name]

    elif t == "method_declaration":
        name = _java_name(node, source)
        if name:
            parent = class_stack[-1] if class_stack else None
            qualified = f"{parent}.{name}" if parent else name
            out.symbols.append(Symbol(
                name=name, kind="method", file=rel,
                line=node.start_point[0] + 1, parent=parent,
            ))
            _scan_java_method_annotations(node, qualified, source, rel, out)
            method_stack = method_stack + [qualified]

    elif t == "method_invocation":
        # Tree-sitter-java exposes an `object` + `name` field; stringify both.
        obj = node.child_by_field_name("object")
        name_n = node.child_by_field_name("name")
        if name_n is not None:
            obj_text = source[obj.start_byte:obj.end_byte].decode("utf-8", errors="replace") if obj else ""
            name_text = source[name_n.start_byte:name_n.end_byte].decode("utf-8", errors="replace")
            callee = f"{obj_text}.{name_text}" if obj_text else name_text
            caller = method_stack[-1] if method_stack else "<class>"
            out.call_edges.append(CallEdge(
                caller=caller, callee=callee[:120], file=rel,
                line=node.start_point[0] + 1,
            ))
            if name_text == "executeQuery" or name_text == "executeUpdate":
                out.db_calls.append(DBCall(
                    kind="jdbc",
                    snippet=source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")[:120],
                    file=rel, line=node.start_point[0] + 1,
                ))

    for child in node.children:
        _walk_java(child, source, rel, out, class_stack, method_stack)


def _java_name(node: "Node", source: bytes) -> Optional[str]:
    ident = node.child_by_field_name("name")
    if ident is None:
        return None
    return source[ident.start_byte:ident.end_byte].decode("utf-8", errors="replace")


def _scan_java_method_annotations(method_node: "Node", qualified: str,
                                   source: bytes, rel: str, out: ASTFindings) -> None:
    # tree-sitter-java doesn't expose `modifiers` as a named field — it's a
    # plain child. Iterate to find it.
    modifiers = next((c for c in method_node.children if c.type == "modifiers"), None)
    if modifiers is None:
        return
    for child in modifiers.children:
        if child.type not in ("annotation", "marker_annotation"):
            continue
        text = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
        m = re.match(r"@(\w+)(?:\(\s*(?:value\s*=\s*)?['\"]?([^'\",)]+)['\"]?)?", text)
        if not m:
            continue
        anno_name, path = m.group(1), m.group(2) or "/"
        method = _JAVA_SPRING_HTTP.get(anno_name)
        if method is None:
            continue
        out.routes.append(Route(
            framework="spring", method=method, path=path.strip(),
            handler=qualified, file=rel, line=child.start_point[0] + 1,
        ))


def _scan_java_repository(class_node: "Node", class_name: str, source: bytes,
                          rel: str, out: ASTFindings) -> None:
    """Any interface extending JpaRepository / CrudRepository is a DB surface."""
    # Scan all children for the inheritance clauses — tree-sitter-java uses
    # `extends_interfaces` for interface_declaration and `superclass`/`interfaces`
    # for class_declaration, and field-name exposure varies by grammar version.
    candidate_types = {"superclass", "super_interfaces", "interfaces", "extends_interfaces"}
    for child in class_node.children:
        if child.type not in candidate_types:
            continue
        text = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
        if re.search(r"\b(JpaRepository|CrudRepository|PagingAndSortingRepository)\b", text):
            out.db_calls.append(DBCall(
                kind="jpa_repository",
                snippet=f"{class_name} {text.strip()[:80]}",
                file=rel, line=class_node.start_point[0] + 1,
            ))
            return
