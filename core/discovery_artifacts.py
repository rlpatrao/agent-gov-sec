"""Pydantic models for every discovery artifact.

Shared across all Discovery pipeline agents (Scanner, Grapher, BRD,
Architect, Stories) and the WaveScheduler. Ported from agentrepo's
agent_harness/discovery/artifacts.py with no schema changes so existing
artifact JSON files round-trip cleanly.
"""
from typing import Literal

from pydantic import BaseModel, Field

ResourceKind = Literal[
    "dynamodb_table", "s3_bucket", "sqs_queue", "sns_topic",
    "kinesis_stream", "secrets_manager_secret", "lambda_function",
]
EdgeKind = Literal[
    "imports", "reads", "writes", "produces", "consumes",
    "invokes", "shares_db",
]
NodeKind = Literal["module", "aws_resource", "library"]


class RepoMeta(BaseModel):
    root_path: str
    total_files: int
    total_loc: int
    discovered_at: str  # ISO-8601 UTC


class ModuleRecord(BaseModel):
    id: str
    path: str
    language: str
    handler_entrypoint: str
    loc: int
    config_files: list[str] = Field(default_factory=list)


class Inventory(BaseModel):
    repo_meta: RepoMeta
    modules: list[ModuleRecord]


class GraphNode(BaseModel):
    id: str
    kind: NodeKind
    attrs: dict = Field(default_factory=dict)


class GraphEdge(BaseModel):
    src: str
    dst: str
    kind: EdgeKind


class DependencyGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class ModuleBRD(BaseModel):
    module_id: str
    body: str  # markdown


class SystemBRD(BaseModel):
    body: str  # markdown


class ModuleDesign(BaseModel):
    module_id: str
    body: str


class SystemDesign(BaseModel):
    body: str


class AcceptanceCriterion(BaseModel):
    text: str


class Story(BaseModel):
    id: str
    epic_id: str
    title: str
    description: str
    acceptance_criteria: list[AcceptanceCriterion]
    depends_on: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    estimate: Literal["S", "M", "L"] = "M"


class Epic(BaseModel):
    id: str
    module_id: str
    title: str
    story_ids: list[str] = Field(default_factory=list)


class Stories(BaseModel):
    epics: list[Epic]
    stories: list[Story]


class BacklogItem(BaseModel):
    """Migration work item — strict superset of MigrationRequest."""
    module: str
    language: str
    work_item_id: str = "LOCAL"
    title: str = ""
    description: str = ""
    acceptance_criteria: str = ""
    source_paths: list[str] = Field(default_factory=list)
    context_paths: list[str] = Field(default_factory=list)
    wave: int


class Backlog(BaseModel):
    items: list[BacklogItem]


class CriticReport(BaseModel):
    verdict: Literal["PASS", "FAIL"]
    reasons: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
