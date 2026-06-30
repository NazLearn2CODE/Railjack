---
title: API Reference
created: {{DATE}}
project: {{PROJECT_NAME}}
---

# {{PROJECT_NAME}} - API Reference

## REST Endpoints

### Authentication

<!-- Auto-generate from OpenAPI spec or document manually -->

| Method | Path | Description | Auth required |
|--------|------|-------------|---------------|
| POST | `/api/auth/login` | Authenticate user | No |
| GET | `/api/auth/me` | Get current user | Yes |

### Core Resources

<!-- Add API sections here -->

## Request/Response Schemas

<!-- Example payloads for key endpoints -->

### Login Request
```json
{
  "email": "user@example.com",
  "password": "plaintext"
}
```

### Login Response
```json
{
  "token": "jwt...",
  "user": {
    "id": 123,
    "email": "user@example.com"
  }
}
```

## Error Codes

| Code | Meaning | Retry? |
|------|---------|--------|
| 401 | Invalid token | No |
| 429 | Rate limited | Yes (after delay) |

## Internal APIs

<!-- If this project has internal modules/functions -->

| Module | Function | When to use |
|--------|----------|-------------|
| `auth/middleware.ts` | `validateToken()` | All protected routes |
| `db/queries.ts` | `getUserById()` | User lookups |

---
*For Claude: Reference this when implementing or calling APIs. Keep in sync with code.*
