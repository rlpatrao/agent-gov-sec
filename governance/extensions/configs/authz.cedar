// Standards-based authz policy (Cedar) — loaded by
// governance.extensions.policy_engine.CedarAuthorizer and evaluated by cedarpy.
// Covers BOTH agent/tool authz and data authz from one policy set. Entity-free:
// rules use principal/action/resource UIDs + request `context` attributes, so no
// entity store is required.
//
// Requests are built as:
//   data:  principal=Agent::"<agent>"  action=Action::"read"      resource=Resource::"<ds.tbl.col>"  context={classification:int, categories:[...], geography:str}
//   tool:  principal=Agent::"<agent>"  action=Action::"use_tool"  resource=Tool::"<tool>"

// ── Data authz (ABAC over the data label) ────────────────────────────────────
// Reads at or below CONFIDENTIAL (classification <= 2) are permitted...
permit(principal, action == Action::"read", resource)
when { context.classification <= 2 };

// ...and the Auditor may read any classification.
permit(principal == Agent::"Auditor", action == Action::"read", resource);

// Hard floor: RESTRICTED+ (>= 3) data is forbidden for everyone except the Auditor.
// (Cedar forbid overrides permit, so this beats the rule above.)
forbid(principal, action == Action::"read", resource)
when { context.classification >= 3 && !(principal == Agent::"Auditor") };

// ── Agent / tool authz (capability allow-list, expressed in Cedar) ────────────
// Each agent's permitted tools are explicit permits — the Cedar form of the
// per-agent capability allow-list. No permit ⇒ default-deny.
permit(principal == Agent::"Coder", action == Action::"use_tool", resource == Tool::"read_file");
permit(principal == Agent::"Coder", action == Action::"use_tool", resource == Tool::"write_file");
permit(principal == Agent::"Tester", action == Action::"use_tool", resource == Tool::"run_tests");
// (Analyzer is a read-only leaf — no tool permits, so all tool use is denied.)
