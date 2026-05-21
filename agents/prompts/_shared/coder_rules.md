## HARD RULES — Will Cause BLOCK From Reviewer

These are non-negotiable regardless of source stack or target platform.

### 0. NO STUBS OF ANY KIND — IMPLEMENTATIONS MUST RUN IN PRODUCTION
- `raise NotImplementedError(...)` — forbidden in production code.
- `pass` as the entire body of a non-trivial method — forbidden.
- `# TODO`, `# FIXME`, `# placeholder` in production paths — forbidden (fine in test fixtures).
- Empty stand-in classes (`InMemoryRepository`, `FakeQueueClient`, etc.) — forbidden.

If you cannot generate a real implementation, generate one anyway using `os.environ` for missing values. The app will fail at startup with a clear `KeyError` — vastly better than `NotImplementedError`.

### 1. NO IN-MEMORY STAND-INS — CALL THE REAL AZURE SDK

| Source concept | Required Azure SDK |
|---|---|
| DynamoDB / RDS | `azure.cosmos.aio.CosmosClient` with `DefaultAzureCredential` |
| SQS | `azure.servicebus.aio.ServiceBusClient` |
| SNS | Service Bus Topics or `azure.eventgrid.EventGridPublisherClient` |
| S3 | `azure.storage.blob.aio.BlobServiceClient` |
| Secrets Manager | `azure.keyvault.secrets.SecretClient` |
| IAM role | `DefaultAzureCredential` everywhere — never hardcoded keys |
| RDS MySQL/Postgres | `azure.mysql` / `azure.postgresql` SDK or `pyodbc` |
| Elastic Beanstalk / EC2 | Azure App Service SDK / Container Apps SDK |

### 2. NO BROAD `except Exception:`
Catch the specific SDK exception: `CosmosResourceNotFoundError`, `ServiceBusError`, `HttpResponseError`, etc.

### 3. TEST THE FUNCTION / SERVICE ENTRYPOINT, MOCK EVERY AZURE NETWORK CALL
- Tests MUST invoke the decorated or public entrypoint and assert on the response shape.
- Tests MUST mock every SDK call with `unittest.mock.patch` / `AsyncMock` / `MagicMock`. No live Azure, no emulators.
- Stub `DefaultAzureCredential` and every SDK client class.
- Use `AsyncMock` for `async` methods.
- Assert on the calls made to the mock, not on round-tripped state.

### 3.12. NO MODULE-SCOPE READS OF ENV VARS OR AZURE SDK CLIENTS

Importing any production module must succeed with **zero environment variables set and zero network calls**.

Forbidden at module top-level (outside functions/methods):
- `os.environ["X"]`, `os.getenv("X")` without a literal default.
- Any SDK client constructor (`CosmosClient(...)`, `ServiceBusClient(...)`, etc.) evaluated at import time.
- `DefaultAzureCredential()` evaluated at import time.

Required pattern — lazy initialization via factory:
```python
from functools import lru_cache
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

@lru_cache(maxsize=1)
def _cosmos_client() -> CosmosClient:
    return CosmosClient(os.environ["COSMOS_ENDPOINT"], DefaultAzureCredential())
```

### 3.15. SELF-CONTAINED MODULE LAYOUT (NO SIBLING-PACKAGE IMPORTS)

Production code may only import from:
- Python/Node/Java/C# stdlib
- Azure SDK packages
- Third-party packages declared in the module's own dependency file (`requirements.txt`, `package.json`, `pom.xml`, `*.csproj`)
- Modules located inside the current output directory

If the legacy code imports shared utilities from outside its own directory, **inline the needed code** and strip anything unused. Never emit relative imports that escape the output root.

### 4. PRESERVE THE LEGACY CONTRACT
The migrated service must accept the same input fields and return the same response shape as the source. If the source returned `{"statusCode": 400, "body": json.dumps({"errorCode": X})}`, the Azure equivalent must return the same status and body shape.

### 5. IaC MUST DECLARE THE RESOURCES YOU USE
If your code calls `CosmosClient`, the Bicep (or Terraform) template MUST declare `Microsoft.DocumentDB/databaseAccounts`. If it calls `ServiceBusClient`, declare `Microsoft.ServiceBus/namespaces`. Mismatched IaC ↔ code is a BLOCK.

---

## Code Quality Gates

Automated checks enforce the following. Non-compliance causes a BLOCK from the reviewer.

**File size**: 300 lines maximum per file. Split at 200.

**Typing**: Every function has a return type annotation and typed parameters. Use `@dataclass` or Pydantic for structured data.

**Function size**: 100 lines maximum. Refactor at 50.

**State ownership**: One module owns writes to each table or queue. Others read via that module's API.

**Error handling**: Catch specific exception types. Log with module, operation, and relevant IDs. HTTP functions return:
```json
{"error": {"code": "...", "message": "...", "details": []}}
```

**Dead code**: No commented-out code. No unused imports or variables.

**Testing gates** (enforced by tester):
- Each Azure SDK class is patched with `unittest.mock.patch` or `AsyncMock` in the test file.
- Tests assert on response status and body, not on internal state.
- No env vars are read at module scope without a literal fallback default.

**Security gates** (enforced by security reviewer):
- No hardcoded secrets or connection strings — use env vars with placeholder values in local settings.
- Authenticated endpoints declare an explicit auth level — never expose unauthenticated unless the design explicitly requires it.
- Input from request bodies is validated before persistence.
