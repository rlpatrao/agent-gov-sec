"""
Deterministic AST extractor tests — pure domain logic, no LLM.
"""

from pathlib import Path

import pytest

from agents.ast_parser import extract_ast


class TestPythonExtractor:
    def test_fastapi_route_and_symbols(self, tmp_path: Path):
        (tmp_path / "app.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/users/{id}')\n"
            "def get_user(id: int):\n"
            "    return {'id': id}\n"
        )
        result = extract_ast(str(tmp_path), ["app.py"])
        assert result.language == "python"
        assert result.files_analyzed == 1
        assert result.files_skipped == 0
        names = {s.name for s in result.symbols}
        assert "get_user" in names
        assert len(result.routes) == 1
        route = result.routes[0]
        assert route.framework == "fastapi"
        assert route.method == "GET"
        assert route.path == "/users/{id}"
        assert route.handler == "get_user"

    def test_flask_route_methods_parsed(self, tmp_path: Path):
        (tmp_path / "app.py").write_text(
            "@app.route('/submit', methods=['POST'])\n"
            "def submit():\n"
            "    return 'ok'\n"
        )
        result = extract_ast(str(tmp_path), ["app.py"])
        assert len(result.routes) == 1
        assert result.routes[0].framework == "flask"
        assert result.routes[0].method == "POST"

    def test_bare_except_and_todo_flagged(self, tmp_path: Path):
        (tmp_path / "bad.py").write_text(
            "def f():\n"
            "    # TODO: clean up\n"
            "    try:\n"
            "        1/0\n"
            "    except:\n"
            "        pass\n"
        )
        result = extract_ast(str(tmp_path), ["bad.py"])
        rules = {f.rule for f in result.findings}
        assert "bare_except" in rules
        assert "todo" in rules

    def test_sqlalchemy_call_recorded_once_per_line(self, tmp_path: Path):
        (tmp_path / "repo.py").write_text(
            "def load(uid):\n"
            "    return session.query(User).filter_by(id=uid).first()\n"
        )
        result = extract_ast(str(tmp_path), ["repo.py"])
        sqla = [d for d in result.db_calls if d.kind == "sqlalchemy"]
        # Chained call should dedupe to 1 db_call for the row
        assert len(sqla) == 1
        assert "session.query" in sqla[0].snippet

    def test_call_edge_records_caller(self, tmp_path: Path):
        (tmp_path / "m.py").write_text(
            "def a():\n"
            "    b()\n"
            "def b():\n"
            "    pass\n"
        )
        result = extract_ast(str(tmp_path), ["m.py"])
        edges = [(e.caller, e.callee) for e in result.call_edges]
        assert ("a", "b") in edges


class TestJavaExtractor:
    def test_spring_get_mapping_parsed(self, tmp_path: Path):
        (tmp_path / "Ctrl.java").write_text(
            "package x;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "@RestController\n"
            "public class Ctrl {\n"
            "  @GetMapping(\"/hello\")\n"
            "  public String hi() { return \"hi\"; }\n"
            "}\n"
        )
        result = extract_ast(str(tmp_path), ["Ctrl.java"])
        assert result.language == "java"
        assert len(result.routes) == 1
        assert result.routes[0].framework == "spring"
        assert result.routes[0].method == "GET"
        assert result.routes[0].path == "/hello"

    def test_jpa_repository_recorded(self, tmp_path: Path):
        (tmp_path / "UserRepo.java").write_text(
            "package x;\n"
            "import org.springframework.data.jpa.repository.JpaRepository;\n"
            "public interface UserRepo extends JpaRepository<User, Long> {}\n"
        )
        result = extract_ast(str(tmp_path), ["UserRepo.java"])
        jpas = [d for d in result.db_calls if d.kind == "jpa_repository"]
        assert len(jpas) == 1
        assert "JpaRepository" in jpas[0].snippet


class TestErrorHandling:
    def test_missing_file_recorded_not_raised(self, tmp_path: Path):
        result = extract_ast(str(tmp_path), ["nope.py"])
        assert result.files_analyzed == 0
        assert result.files_skipped == 1
        assert any("not a file" in reason for _, reason in result.errors)

    def test_unsupported_extension_skipped(self, tmp_path: Path):
        (tmp_path / "f.rb").write_text("puts 'hi'\n")
        result = extract_ast(str(tmp_path), ["f.rb"])
        assert result.files_skipped == 1
        assert any("unsupported" in reason for _, reason in result.errors)
