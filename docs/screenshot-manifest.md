# Screenshot Manifest — Galaxy SDLC Video Showcase

Every `[ON SCREEN]` block in `video-demo-script.md` maps to one slide in the PPTX.  
Take each screenshot below, paste it into the corresponding slide in PowerPoint (replacing the green placeholder text), then upload to Narakeet.

**Naming convention:** `screenshots/slide_<N>_<scene>.png`  
Create a `screenshots/` folder at the project root (gitignored) and save files there.

---

## Pre-recording screenshots (can be slides / prepared images)

| # | Slide | What to capture | Source |
|---|---|---|---|
| 1 | A.0 — Reference Architecture | Full 5-layer Enterprise Agentic Security Reference Architecture diagram | Your slide deck (slide 9) |
| 2 | A.1 — Galaxy sits in this framework | Same diagram with overlay annotations showing which boxes Galaxy implements | Annotate slide 9 or use the mapping table from the script |
| 3 | A.2 — Traditional API vs Agentic AI | Side-by-side comparison table | Create from the table in Scene A.2 of the script |
| 4 | A.3 — OWASP Threat Map | 5-row table: OWASP ID → threat → our control | Create from the table in Scene A.3 of the script |
| 5 | A.4 — Shared Identity Problem | Conventional account vs NHI architecture comparison | Create from the ASCII art in Scene A.4 of the script |
| 6 | B.1 — Migration at Scale | Manual cost table + Galaxy estimate | Create from the table in Scene B.1 of the script |

---

## Architecture diagrams (from docs/)

| # | Slide | What to capture | Source |
|---|---|---|---|
| 7 | B.2 — Pipeline Layer | Migration pipeline diagram: RepoClassifier → Analyzer → Coder → Tester → Reviewer → SecurityReviewer | Render `docs/architecture.md §2.1` Mermaid diagram, or screenshot the rendered markdown in VS Code |
| 8 | B.2 — Azure Infrastructure | Azure resource map showing all services in `galaxyscanner-rg` | `docs/architecture.md §1.7` — or screenshot Azure Portal → Resource Group → galaxyscanner-rg → Overview |
| 9 | B.2 — Governance Layer | Seven-guard middleware pipeline ①–⑦ | `docs/architecture.md §1.3` Mermaid diagram |
| 10 | B.3 — Guardrails Inventory | "What's wired today" table (7 middleware rows) | Screenshot `docs/guardrails-inventory.md` rendered in browser or VS Code preview |
| 11 | B.3 — Available modules | "High value (small lift to wire)" section | Same file, scroll to next section |

---

## Terminal screenshots (run live or use saved output)

| # | Slide | Command to run | Expected output |
|---|---|---|---|
| 12 | Scene 1 — Live Demo Setup | `ls agents/ governance/ core/ a2a/ scripts/` | Directory listing showing all key folders |
| 13 | Scene 2.1 — Migration run | `python scripts/run_migration.py --source-dir legacy/aws_legacy` | Full pipeline output: classify → 5 phases → "Pipeline complete" |
| 14 | Scene 4.3 — Governance demo | `python scripts/demo_governance.py` | All 4 scenarios: ALLOW / DENY / REDACT / HASH CHAIN VALID |
| 15 | Scene 6 — Output directory | `ls migrated/aws_legacy/v1/` | `function_app.py  tests/  infrastructure/  analysis/  logs/  run-summary.json` |

> **Tip:** Run these first, capture the output to a file (`... 2>&1 | tee /tmp/output.txt`), then replay from saved output during recording so timing is consistent.

---

## Code editor screenshots

| # | Slide | File to show | What to highlight |
|---|---|---|---|
| 16 | Scene 3.1a — NHI Code | `core/nhi_identity.py` lines 39–61 | The `_NHI_CLIENT_IDS` dict — all 18 agent types |
| 17 | Scene 3.1b — APIM Headers | `agents/_base.py` | The `default_headers` block: `x-agent-type`, `x-nhi-id`, `Ocp-Apim-Subscription-Key` |
| 18 | Scene 4.1a — Middleware Stack | `governance/middleware.py` | The `build_governance_stack` docstring showing all 7 guards in order |
| 19 | Scene 4.1b — Token Budgets | `agents/config/coder.yaml` or similar | `context_budget_tokens` and `prompt_injection_block_threshold` values |
| 20 | Scene 4.2a — Injection YAML | `governance/configs/prompt-injection.yaml` | `detection_patterns` block: `direct_override`, `delimiter`, `role_play`, `encoding` |
| 21 | Scene 4.2b — Policy YAML | `governance/policies/galaxy-ast.yaml` | `deny-network-egress-tools` rule |

---

## Azure Portal screenshots (live or from a previous run)

| # | Slide | Portal path | What to show |
|---|---|---|---|
| 22 | Scene 2.2a — Transaction Search | App Insights → Investigate → Transaction Search | Paste `operation_Id`, click "See all telemetry" |
| 23 | Scene 2.2b — Span waterfall | App Insights → End-to-end transaction details | Full waterfall: `pipeline.run` root → 5 `a2a.dispatch.*` children |
| 24 | Scene 2.2c — Span dimensions | Click into `a2a.dispatch.SecurityReviewer` | Custom dimensions panel: `galaxy.run_id`, `galaxy.module`, `gen_ai.usage.*` |
| 25 | Scene 2.3a — Query 1 results | App Insights → Logs | Run Query 1 from script; table with 5 rows (one per agent) |
| 26 | Scene 2.3b — Query 2 results | App Insights → Logs | Run Query 2; token cost per agent (SecurityReviewer highest) |
| 27 | Scene 2.3c — Query 3 results | App Insights → Logs | Run Query 3; governance deny events table |
| 28 | Scene 3.2a — Entra App list | Entra ID → Enterprise Applications → filter "galaxy-" | List of galaxy-* registrations |
| 29 | Scene 3.2b — Sign-in logs | Click galaxy-securityreviewer → Sign-in logs | Rows showing per-agent Entra sign-ins |
| 30 | Scene 3.3a — APIM KQL | APIM → Monitoring → Logs | Run APIM KQL from script; per-NHI call count table |
| 31 | Scene 4.4 — Audit events | App Insights → Logs | Run Query 4 from script; governance audit event rows |

---

## Optional: slides for roadmap / closing

| # | Slide | What to capture | Source |
|---|---|---|---|
| 32 | Scene 5 — Governance Roadmap | 8-row roadmap table | Create from the table in Scene 5 of the script |
| 33 | Scene 5 — Regulatory Mapping | Controls today → Standards mapping table | Create from the regulatory mapping table in Scene 5 |

---

## Summary checklist

- [ ] Slides 1–6: create from script tables / annotate existing deck
- [ ] Slides 7–11: render docs/ markdown diagrams
- [ ] Slides 12–15: capture terminal output (run commands once, save to file)
- [ ] Slides 16–21: open files in editor, scroll to right sections, screenshot
- [ ] Slides 22–31: Azure Portal live (or from a previous recorded run)
- [ ] Slides 32–33: create from script tables

**Total: ~33 screenshots across 5 categories.**

---

## Workflow

```
1. uv run python scripts/build_narakeet_pptx.py
   → docs/galaxy-showcase-narakeet.pptx  (narration pre-filled in speaker notes)

2. Open in PowerPoint.
   Each slide shows green placeholder text describing what screenshot goes there.

3. For each slide:
   - Take / find the screenshot from this manifest
   - Insert → Picture → This Device
   - Resize to fill the slide
   - Delete the green placeholder text box

4. File → Save

5. Upload to Narakeet → select the PPTX → generate video.
```
