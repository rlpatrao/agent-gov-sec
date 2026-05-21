## Code Quality Standards

The code produced for this project is expected to meet the following quality standards. Automated gates check compliance.

### Standard 1: Small Modules
- 300 lines per file is the upper limit
- A warning applies at 200 lines — consider splitting
- One module carries one responsibility
- Growing files extract helpers into sub-modules

### Standard 2: Static Typing
- Return type annotations on all functions (Python: `-> type`, TypeScript: `: ReturnType`)
- All parameters are typed
- Structured data uses `@dataclass` or Pydantic models rather than raw dicts
- `__init__`, `__str__`, `__repr__` are exempt from return annotations

### Standard 3: Function Size
- 100 lines per function is the upper limit
- A warning applies at 50 lines — refactor into smaller helpers
- Each function addresses one responsibility
- Complex conditionals are extracted into named helpers

### Standard 4: State Mutation Ownership
- A single module owns writes to any given database table or state store
- Other modules access state through that owner's API
- Existing repository modules are checked before adding new write paths
- DB write logic is not duplicated across services

### Standard 5: Specific Error Handling
- Bare `except:` clauses are not used — exception types are named specifically
- `except CosmosResourceNotFoundError` is preferred over `except Exception`
- Exceptions are logged with context (module, operation, relevant IDs)
- HTTP-triggered functions return structured error responses:
  ```python
  except CosmosResourceNotFoundError as e:
      logger.warning("Item %s not found in %s: %s", item_id, container, e)
      return func.HttpResponse(json.dumps({"error": "Not found"}), status_code=404)
  ```

### Standard 6: No Dead Code
- Commented-out code is removed before merging
- Unused imports are removed
- Unused variables and functions are removed
- Code preserved for future use belongs in a branch, not in comments

---

## Testing Standards

- Minimum 80% code coverage (ratchet — coverage can only go up)
- Each public function has at least one test
- Test structure: Arrange / Act / Assert
- Test names describe the scenario: `test_create_order_returns_400_when_missing_required_fields`
- External services (Azure SDK, HTTP clients) are mocked — real services are not called from unit tests
- Integration tests reside in `tests/integration/`

## Logging Standards

- Structured logging uses consistent fields: `module`, `operation`, `duration_ms`, `status`
- Log levels:
  - `DEBUG`: Detailed diagnostic info (not in production)
  - `INFO`: Normal operations (startup, request received, operation completed)
  - `WARNING`: Recoverable issues (retry, fallback, degraded)
  - `ERROR`: Failures requiring attention (unhandled exception, external service down)
- Correlation IDs appear in all log entries for distributed tracing
- Secrets, tokens, passwords, and PII are not logged

## Error Response Format

HTTP-triggered functions return errors in this format:
```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "Order with ID 12345 not found",
    "details": []
  }
}
```
- Appropriate HTTP status codes (400, 401, 403, 404, 409, 422, 500)
- Internal stack traces are not exposed to callers
- A machine-readable error code is included for client-side handling
