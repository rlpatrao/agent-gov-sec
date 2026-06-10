"""
tests/test_repo_classifier.py — unit tests for the deterministic repo classifier.

Covers:
  - classify_repo returns None for an empty/unknown directory
  - python_serverless detection from boto3 import + requirements.txt
  - typescript_serverless detection from @aws-sdk imports + tsconfig
  - java_spring_boot detection from @SpringBootApplication + pom.xml + ECS infra
  - ecs_docker detection from Dockerfile presence + infra markers
  - iac_terraform detection from .tf files with provider "aws"
  - frontend_spa detection from package.json with "react" + CloudFront infra
  - required-file gate: ecs_docker needs Dockerfile
  - confidence score is non-zero when signals fire
  - does not misclassify Java Spring Boot as java_serverless (Spring signals dominate)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from payload_agents._lib.repo_classifier import classify_repo


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ── Empty / unknown dir ───────────────────────────────────────────────────────

class TestUnknown:
    def test_empty_dir_returns_none(self, tmp_path: Path):
        r = classify_repo(tmp_path)
        assert r.codebase_type is None
        assert r.confidence == 0.0

    def test_nonexistent_dir_returns_none(self, tmp_path: Path):
        r = classify_repo(tmp_path / "ghost")
        assert r.codebase_type is None

    def test_plain_python_no_aws_returns_none(self, tmp_path: Path):
        _write(tmp_path / "app.py", "print('hello')\n")
        r = classify_repo(tmp_path)
        # No AWS signals → no type should win
        assert r.codebase_type is None


# ── python_serverless ─────────────────────────────────────────────────────────

class TestPythonServerless:
    def _scaffold(self, root: Path) -> None:
        _write(root / "requirements.txt", "boto3==1.34.0\naws-lambda-powertools\n")
        _write(root / "handler.py", (
            "import boto3\n"
            "def lambda_handler(event, context):\n"
            "    return {'statusCode': 200}\n"
        ))

    def test_detects_python_serverless(self, tmp_path: Path):
        self._scaffold(tmp_path)
        r = classify_repo(tmp_path)
        assert r.codebase_type == "python_serverless"
        assert r.confidence > 0

    def test_boto3_alone_is_sufficient_signal(self, tmp_path: Path):
        _write(tmp_path / "main.py", "import boto3\ndef handler(event, context): pass\n")
        r = classify_repo(tmp_path)
        assert r.codebase_type == "python_serverless"

    def test_terraform_aws_lambda_marker_boosts_score(self, tmp_path: Path):
        self._scaffold(tmp_path)
        _write(tmp_path / "main.tf", 'resource "aws_lambda_function" "fn" {}\n')
        r = classify_repo(tmp_path)
        assert r.codebase_type == "python_serverless"
        assert r.confidence > 0.3


# ── typescript_serverless ─────────────────────────────────────────────────────

class TestTypescriptServerless:
    def test_detects_typescript_serverless(self, tmp_path: Path):
        _write(tmp_path / "tsconfig.json", '{"compilerOptions": {}}\n')
        _write(tmp_path / "src" / "handler.ts", (
            "import * as AWS from '@aws-sdk/client-s3';\n"
            "export const handler: APIGatewayProxyHandler = async (event) => {\n"
            "    return { statusCode: 200, body: 'ok' };\n"
            "};\n"
        ))
        r = classify_repo(tmp_path)
        assert r.codebase_type == "typescript_serverless"

    def test_sqs_handler_type_fires(self, tmp_path: Path):
        _write(tmp_path / "tsconfig.json", '{}')
        _write(tmp_path / "handler.ts", "export const handler: SQSHandler = async () => {};\n")
        r = classify_repo(tmp_path)
        assert r.codebase_type == "typescript_serverless"


# ── node_serverless ───────────────────────────────────────────────────────────

class TestNodeServerless:
    def test_detects_node_serverless(self, tmp_path: Path):
        _write(tmp_path / "package.json", '{"name": "fn", "dependencies": {"aws-sdk": "^2"}}')
        _write(tmp_path / "handler.js", (
            "const AWS = require('aws-sdk');\n"
            "exports.handler = async (event, context) => ({ statusCode: 200 });\n"
        ))
        r = classify_repo(tmp_path)
        assert r.codebase_type == "node_serverless"


# ── java_serverless ───────────────────────────────────────────────────────────

class TestJavaServerless:
    def test_detects_java_serverless(self, tmp_path: Path):
        _write(tmp_path / "pom.xml", (
            "<project><dependencies>"
            "<dependency><groupId>com.amazonaws</groupId>"
            "<artifactId>aws-lambda-java-core</artifactId></dependency>"
            "</dependencies></project>\n"
        ))
        _write(tmp_path / "src" / "main" / "java" / "Handler.java", (
            "import com.amazonaws.services.lambda.runtime.Context;\n"
            "public class Handler implements RequestHandler<Map, Map> {}\n"
        ))
        r = classify_repo(tmp_path)
        assert r.codebase_type == "java_serverless"


# ── java_spring_boot ──────────────────────────────────────────────────────────

class TestJavaSpringBoot:
    def test_detects_java_spring_boot(self, tmp_path: Path):
        _write(tmp_path / "pom.xml", (
            "<project><dependencies>"
            "<dependency><groupId>org.springframework.boot</groupId>"
            "<artifactId>spring-boot-starter-web</artifactId></dependency>"
            "</dependencies></project>\n"
        ))
        _write(tmp_path / "src" / "main" / "java" / "App.java", (
            "import org.springframework.boot.SpringApplication;\n"
            "@SpringBootApplication\n"
            "public class App { public static void main(String[] a) { SpringApplication.run(App.class, a); } }\n"
        ))
        _write(tmp_path / "main.tf", (
            'resource "aws_ecs_service" "svc" {}\n'
            'resource "ecs_task_definition" "task" {}\n'
        ))
        r = classify_repo(tmp_path)
        assert r.codebase_type == "java_spring_boot"

    def test_spring_beats_java_serverless_when_both_present(self, tmp_path: Path):
        """Spring signals should win over bare Lambda signals."""
        _write(tmp_path / "pom.xml", (
            "<project><dependencies>"
            "<dependency><artifactId>spring-boot-starter-web</artifactId></dependency>"
            "<dependency><groupId>com.amazonaws</groupId>"
            "<artifactId>aws-lambda-java-core</artifactId></dependency>"
            "</dependencies></project>"
        ))
        _write(tmp_path / "src" / "main" / "java" / "App.java", (
            "@SpringBootApplication\n"
            "public class App { public static void main(String[] a) { SpringApplication.run(App.class, a); } }\n"
        ))
        _write(tmp_path / "main.tf", 'resource "aws_ecs_service" "svc" {}\n')
        r = classify_repo(tmp_path)
        # Spring Boot signals (ECS + @SpringBootApplication) should score higher
        # than java_serverless which has no ECS infra markers
        assert r.codebase_type == "java_spring_boot"


# ── ecs_docker ────────────────────────────────────────────────────────────────

class TestEcsDocker:
    def test_detects_ecs_docker_with_dockerfile(self, tmp_path: Path):
        _write(tmp_path / "Dockerfile", "FROM python:3.11\nEXPOSE 8080\n")
        _write(tmp_path / "docker-compose.yml", "version: '3'\nservices:\n  app:\n    build: .\n")
        _write(tmp_path / "main.tf", (
            'resource "ecs_task_definition" "task" {}\n'
            'resource "aws_ecs_service" "svc" {}\n'
        ))
        r = classify_repo(tmp_path)
        assert r.codebase_type == "ecs_docker"

    def test_no_dockerfile_means_ecs_docker_not_selected(self, tmp_path: Path):
        # Without Dockerfile the required-file gate blocks ecs_docker
        _write(tmp_path / "main.tf", 'resource "aws_ecs_service" "svc" {}\n')
        r = classify_repo(tmp_path)
        assert r.codebase_type != "ecs_docker"


# ── iac_terraform ─────────────────────────────────────────────────────────────

class TestIacTerraform:
    def test_detects_terraform(self, tmp_path: Path):
        _write(tmp_path / "main.tf", (
            'provider "aws" { region = "us-east-1" }\n'
            'resource "aws_s3_bucket" "b" { bucket = "my-bucket" }\n'
        ))
        _write(tmp_path / "terraform.lock.hcl", '# This file is maintained automatically.\n')
        r = classify_repo(tmp_path)
        assert r.codebase_type == "iac_terraform"

    def test_hashicorp_source_fires(self, tmp_path: Path):
        _write(tmp_path / "versions.tf", (
            'terraform {\n'
            '  required_providers {\n'
            '    aws = { source = "hashicorp/aws" }\n'
            '  }\n'
            '}\n'
        ))
        r = classify_repo(tmp_path)
        assert r.codebase_type == "iac_terraform"


# ── frontend_spa ──────────────────────────────────────────────────────────────

class TestFrontendSpa:
    def test_detects_spa(self, tmp_path: Path):
        _write(tmp_path / "package.json", '{"name": "ui", "dependencies": {"react": "^18"}}')
        _write(tmp_path / "src" / "App.tsx", 'import React from "react";\nexport default function App() { return <div/>; }')
        _write(tmp_path / "infra" / "cloudfront.tf", (
            'resource "aws_cloudfront_distribution" "cdn" {}\n'
            'resource "aws_s3_bucket_website_configuration" "web" {}\n'
        ))
        r = classify_repo(tmp_path)
        assert r.codebase_type == "frontend_spa"


# ── Scores dict completeness ──────────────────────────────────────────────────

class TestScoresDict:
    def test_scores_contains_entry_for_every_type(self, tmp_path: Path):
        _write(tmp_path / "main.py", "import boto3\n")
        r = classify_repo(tmp_path)
        # All type keys must appear (even those with score=0)
        expected_types = {
            "python_serverless", "typescript_serverless", "node_serverless",
            "java_serverless", "java_spring_boot", "ecs_docker",
            "dotnet_serverless", "frontend_spa", "php_web_app", "iac_terraform",
        }
        assert expected_types.issubset(set(r.scores.keys()))
