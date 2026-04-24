# Add to `requirements.txt`

Add this section after the Azure identity section:

```txt
# ── Configuration Management ────────────────────────────────────────────────
# Pydantic v2 for schema validation, type safety, JSON schema generation
pydantic>=2.0.0,<3
```

**Full addition to requirements.txt:**

```txt
# ── Azure identity + secrets (Workload Identity + Key Vault) ───────────────
azure-identity>=1.19.0
azure-keyvault-secrets>=4.8.0

# ── Configuration Management ────────────────────────────────────────────────
# Pydantic v2 for schema validation, type safety, JSON schema generation
pydantic>=2.0.0,<3

# ── OTel — spans propagated via APIM to Application Insights ────────────────
```

Then install:

```bash
pip install -r requirements.txt
```

## Why Pydantic v2?

✅ Already widely used in the Python ecosystem  
✅ Built-in JSON schema generation (`model_json_schema()`)  
✅ Excellent error messages  
✅ Type hints are enforced  
✅ ~1 MB footprint, no heavy dependencies  
✅ Works great with YAML via `pydantic.BaseModel` + `yaml.safe_load()`  

## Pydantic is likely already installed via transitive deps

Since you have:
- agent-framework-core
- agent-os-kernel
- agentmesh-platform
- azure-* packages

These almost certainly pull Pydantic as a transitive dependency. But it's good practice to declare it explicitly so you control the version.

Check:
```bash
pip show pydantic
```

If it's already there, just update `requirements.txt` to pin the version for safety.
