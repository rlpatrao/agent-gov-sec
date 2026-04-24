# Config Integration Example

How to use the new Pydantic + YAML config system in your agents.

## Before (Hardcoded)

```python
# agents/scanner_agent.py
AGENT_TYPE = "Scanner"
MAX_FILE_SCAN_BYTES = 50_000
ALLOWED_A2A_RECIPIENTS = ["ASTAnalyzer"]
MAX_AST_FILES_PER_DISPATCH = 40

def traverse_repo(repo_path: str) -> dict:
    for dirpath, dirnames, filenames in os.walk(root):
        for filename in filenames:
            # ... logic ...
            head = f.read(MAX_FILE_SCAN_BYTES)  # Hardcoded
```

## After (Config-Driven)

```python
# agents/scanner_agent.py
import logging
from agents.config import load_agent_config_cached, ConfigError

logger = logging.getLogger(__name__)

# Load once at module import
try:
    _config = load_agent_config_cached("scanner")
    AGENT_TYPE = _config.agent_type
    MAX_FILE_SCAN_BYTES = _config.max_file_scan_bytes
    ALLOWED_A2A_RECIPIENTS = _config.a2a.allowed_recipients
    MAX_AST_FILES_PER_DISPATCH = _config.a2a.max_files_per_dispatch
except ConfigError as e:
    logger.error(f"Failed to load scanner config: {e}")
    raise

def traverse_repo(repo_path: str) -> dict:
    for dirpath, dirnames, filenames in os.walk(root):
        for filename in filenames:
            # ... logic ...
            head = f.read(MAX_FILE_SCAN_BYTES)  # Now from config

async def build_scanner_agent(run_id: str, ...) -> tuple[Agent, ...]:
    # Pass allowed recipients to governance
    middleware, pg_backend, audit = await build_governance_stack(
        agent_id=agent_id,
        allowed_tools=None,  # or derive from _config.governance.allowed_tools
        agent_config=_config,  # NEW: pass full config to middleware
    )
    
    agent = Agent(
        client=client,
        instructions=SYSTEM_PROMPT,
        name=AGENT_TYPE,
        id=agent_id,
        middleware=middleware,
        a2a_recipients=ALLOWED_A2A_RECIPIENTS,  # From config
    )
    
    return agent, pg_backend, audit
```

## YAML Config

**File: `agents/config/scanner.yaml`**

```yaml
version: "1.0"
name: scanner-agent-config

agent:
  type: "Scanner"
  max_file_scan_bytes: 50000
  max_allowed_errors: 5

a2a:
  allowed_recipients:
    - "ASTAnalyzer"
  max_files_per_dispatch: 40
  timeout_seconds: 30

governance:
  enable_rogue_detection: true
  policies:
    - galaxy-core.yaml
    - galaxy-scanner.yaml
```

## Usage Patterns

### Pattern 1: Direct Load

```python
from agents.config import load_agent_config

config = load_agent_config("scanner")
print(config.agent_type)  # "Scanner"
print(config.a2a.allowed_recipients)  # ["ASTAnalyzer"]
```

### Pattern 2: Cached Load (Recommended)

```python
from agents.config import load_agent_config_cached

# At module import time (happens once)
_config = load_agent_config_cached("scanner")

# Throughout the module
def some_function():
    if _config.governance.enable_rogue_detection:
        # enable anomaly detection
        pass
```

### Pattern 3: Custom Config Directory (Testing)

```python
from pathlib import Path
from agents.config import load_agent_config

custom_dir = Path("/tmp/test-configs")
config = load_agent_config("scanner", config_dir=custom_dir)
```

## Error Handling

### ConfigError: File Not Found

```python
from agents.config import ConfigError, load_agent_config

try:
    config = load_agent_config("nonexistent")
except ConfigError as e:
    print(f"Config not found: {e}")
    # Handle gracefully: use defaults, exit, etc.
```

### ValidationError: Schema Mismatch

```python
from agents.config import ConfigError, load_agent_config

try:
    config = load_agent_config("scanner")
except ConfigError as e:
    print(f"Config validation failed: {e}")
    # The error message lists which fields failed and why
```

Example error:
```
Config validation failed for .../scanner.yaml:
2 error(s):
  - ('a2a', 'max_files_per_dispatch'): ensure this value is greater than 0
  - ('agent_type'): value is not a valid enumeration member
```

## Running Tests

```bash
pytest tests/test_config.py -v
```

## Updating Config Files

To change Scanner's max file scan size from 50KB to 100KB:

**Before (Code Change Required):**
```python
# agents/scanner_agent.py
MAX_FILE_SCAN_BYTES = 100_000  # Change code, redeploy
```

**After (Config Only):**
```yaml
# agents/config/scanner.yaml
agent:
  max_file_scan_bytes: 100000  # Change config, redeploy/restart
```

In Kubernetes, this can be:
```bash
kubectl create configmap scanner-config --from-file=agents/config/scanner.yaml
kubectl set env deployment/scanner GALAXY_CONFIG_DIR=/etc/config
```

## Schema Validation

Get the full JSON schema for documentation:

```python
from agents.config import AgentConfigModel
import json

schema = AgentConfigModel.model_json_schema()
print(json.dumps(schema, indent=2))
```

This generates a full OpenAPI-compatible schema you can publish.

## Next Steps

1. **Update `agents/scanner_agent.py`**: Replace hardcoded values with config loading
2. **Update `agents/ast_agent.py`**: Same pattern
3. **Update `tests/test_security_traceability.py`**: Use test configs
4. **Add to CI/CD**: Validate all YAML configs against schema on commit
5. **Document for ops**: Show how to update configs in production

## Benefits

✅ **No Code Changes Needed**: Update behavior by changing YAML  
✅ **Type Safety**: Pydantic validates all fields at load time  
✅ **Human-Readable**: YAML is easy to read and review  
✅ **Testable**: Load custom configs from test fixtures  
✅ **Auditable**: Changes to config can be tracked in git  
✅ **Deploy-Friendly**: Works with Kubernetes ConfigMaps  
✅ **Error Messages**: Clear validation failures tell you exactly what's wrong  
