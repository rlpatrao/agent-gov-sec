// Standards-based authz policy (Cedar) — loaded by
// governance.extensions.policy_engine.CedarAuthorizer via MSGK's Cedar backend.
// Covers BOTH agent/tool authz and data authz from one policy set.
//
// NOTE: full evaluation of the `when { ... }` conditions below requires the
// Cedar engine (cedarpy or the `cedar` CLI — `pip install '.[cedar]'`). MSGK's
// built-in fallback matcher only handles coarse permit/forbid and will
// deny-by-default for these conditional rules.

// ── Agent / tool authz ──────────────────────────────────────────────────────
// Known agents may read.
permit(principal, action == Action::"read", resource)
when { principal in [Agent::"FinOps", Agent::"Analyzer", Agent::"Auditor"] };

// Tool use is allow-listed per agent (the Cedar form of the capability guard).
permit(principal, action == Action::"use_tool", resource)
when { resource in principal.allowed_tools };

// ── Data authz (ABAC over the data label) ────────────────────────────────────
// Read permitted when the resource's classification is at/below the agent's
// clearance AND its category is in the agent's allowed set.
permit(principal, action == Action::"read", resource)
when {
  context.classification <= context.clearance &&
  context.category_allowed == true
};

// Hard deny: RESTRICTED+ (>=3) data is never readable except by Auditor.
forbid(principal, action == Action::"read", resource)
when { context.classification >= 3 && !(principal == Agent::"Auditor") };
