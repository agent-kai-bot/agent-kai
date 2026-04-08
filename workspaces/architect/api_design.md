# REST API Design Document

**Project:** Generic Project Management Platform  
**Workspace:** `/home/atc/git/claude-local-ai-agent/workspaces/architect`  
**Author:** Architect (Claude)  
**Date:** 2025‑11‑03

---  

## 1. Overview  

The API provides a set of CRUD‑style and domain‑specific endpoints for managing **projects**, **tasks**, **users**, and **comments**. It is intended to be consumed by web, mobile, and internal microservices clients.  

Key goals:  

- **Scalability** – Stateless design, horizontal scaling behind a load balancer.  
- **Extensibility** – Clear versioning and resource‑oriented URLs.  
- **Security** – OAuth2‑based authentication with fine‑grained scopes and JWT access tokens.  
- **Observability** – Structured error responses, rate limiting, and comprehensive OpenAPI documentation.  

---  

## 2. Architectural Style  

| Aspect | Choice | Rationale |
|--------|--------|-----------|
| **Style** | RESTful, resource‑oriented | Aligns with HTTP standards, easy caching & client adoption |
| **Transport** | HTTPS (TLS 1.3) | Confidentiality & integrity |
| **Data Format** | JSON (UTF‑8) | Universally supported, easy to parse |
| **Response Format** | JSON API spec (optional) | Consistency across endpoints |
| **Error Handling** | Standard HTTP status codes + problem‑detail+json body | Machine‑readable errors |
| **Pagination** | Cursor‑based (offset + limit) + `Link` headers | Efficient for large collections |
| **Rate Limiting** | 60 requests/min per client‑id (configurable) | Prevent abuse, fair usage |
| **CORS** | Allow specific origins via whitelist | Security best practice |

---  

## 3. Versioning Strategy  

- **URI Versioning**: `/api/v{version}/resource`  
- **Initial version**: `v1` (stable contract)  
- **Future versions**: Introduce new resources or breaking changes under a new major version (`v2`, `v3`, …)  
- **Deprecation policy**: 12‑month deprecation notice via `Deprecation` header and documentation.  

---  

## 4. Authentication & Authorization  

### 4.1 OAuth2 Authorization Code Flow with PKCE  

| Step | Description |
|------|-------------|
| 1️⃣ | Client redirects user to Authorization Server (`/oauth/authorize`) with `client_id`, `redirect_uri`, `scope`, `code_challenge`, `response_type=code`. |
| 2️⃣ | User authenticates and authorizes the client. Authorization Server redirects back with `code`. |
| 3️⃣ | Client exchanges `code` for an `access_token` and optional `refresh_token` at `/oauth/token`. |
| 4️⃣ | Access token is a JWT (`RS256`) containing `sub`, `scope`, `exp`, `iat`, `aud`, and custom claims (`role`, `tenant_id`). |
| 5️⃣ | Client includes `Authorization: Bearer <jwt>` header on each request. |

### 4.2 Scopes  

| Scope | Description |
|-------|-------------|
| `project:read` | Read projects & tasks |
| `project:write` | Create/modify/delete projects |
| `task:write` | Create/modify/delete tasks |
| `user:read` | Read user profiles |
| `admin:manage` | Full admin access (rare) |

### 4.3 Access Token Validation  

- **Signature**: Verified against the Authorization Server’s JWKS endpoint (`/.well-known/jwks.json`).  
- **Claims Validation**: `exp`, `nbf`, `iss` (must match expected issuer), `aud` (must match client_id).  
- **Scope Enforcement**: Middleware checks that the required scope is present in the token.  

### 4.4 Refresh Tokens  

- Optional long‑lived refresh token (`offline_access` scope) used to obtain new access tokens without user interaction.  
- Refresh tokens are stored hashed in DB and rotated on each use.  

---  

## 5. Base URL & Resource Naming  

```
https://api.projectmanager.example.com/api/v1
```

All resources are plural nouns, nested where logical:

- `/api/v1/projects/{projectId}/tasks`
- `/api/v1/users/{userId}/tasks`
- `/api/v1/projects/{projectId}/comments`

---  

## 6. Endpoint Specification  

Below is a concise catalog of the core resources. Each endpoint lists **HTTP method**, **URL pattern**, **request body**, **query parameters**, **response structure**, and **error codes**.

### 6.1 Projects  

| Method | Endpoint | Description | Request Body | Response (200/201) |
|--------|----------|-------------|--------------|--------------------|
| `GET` | `/api/v1/projects` | List projects (filterable) | – | `{ "data": [...], "meta": { "pagination": {...} } }` |
| `GET` | `/api/v1/projects/{projectId}` | Retrieve a project | – | `{ "id": "...", "name": "...", "status": "...", "createdAt": "...", "updatedAt": "..." }` |
| `POST` | `/api/v1/projects` | Create a project | `{ "name": "String", "description": "String", "ownerId": "UUID" }` | `201 Created` + location header |
| `PATCH` | `/api/v1/projects/{projectId}` | Update partially | Same as POST (partial fields) | Updated project object |
| `DELETE` | `/api/v1/projects/{projectId}` | Delete a project (soft‑delete) | – | `204 No Content` |

**Filtering / Query Params** (optional):  

- `status` (e.g., `active`, `archived`)  
- `ownerId`  
- `createdAfter`, `createdBefore` (ISO8601)  

### 6.2 Tasks  

| Method | Endpoint | Description | Request Body | Response |
|--------|----------|-------------|--------------|----------|
| `GET` | `/api/v1/projects/{projectId}/tasks` | List tasks in a project | – | `{ "data": [...], "meta": {...} }` |
| `GET` | `/api/v1/tasks/{taskId}` | Retrieve a task | – | Task object |
| `POST` | `/api/v1/tasks` | Create a task (can be root or nested) | `{ "title":"String", "description":"String", "projectId":"UUID", "assigneeId":"UUID", "dueDate":"ISO8601", "priority":"LOW|MEDIUM|HIGH" }` | `201 Created` |
| `PATCH` | `/api/v1/tasks/{taskId}` | Update task | Partial fields | Updated task |
| `DELETE` | `/api/v1/tasks/{taskId}` | Delete a task (soft) | – | `204` |
| `POST` | `/api/v1/tasks/{taskId}/comments` | Add a comment | `{ "authorId":"UUID","content":"String" }` | `201 Created` |

**Task Status Transitions** (example):  

- `PATCH /api/v1/tasks/{taskId}/status` with body `{ "status":"IN_PROGRESS" }`  

### 6.3 Users  

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/users/{userId}` | Retrieve user profile (public fields) |
| `GET` | `/api/v1/me` | Current authenticated user profile |
| `PATCH` | `/api/v1/me` | Update own profile (name, avatar, etc.) |

### 6.4 Comments  

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/comments/{commentId}` | Retrieve comment |
| `POST` | `/api/v1/comments` | Create comment on a project or task (determined by `resourceType` + `resourceId`) |
| `PATCH` | `/api/v1/comments/{commentId}` | Update own comment |
| `DELETE` | `/api/v1/comments/{commentId}` | Delete own comment |

### 6.5 Search  

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/search` | Query across projects, tasks, users (`q` param) |
| `GET` | `/api/v1/projects/{projectId}/search` | Search within a project (e.g., `q=bug`) |

---  

## 7. Data Models  

All models follow a **JSON API**-like structure with `id`, `type`, `attributes`, and optional `relationships`. Below are simplified schemas.

### 7.1 Project  

```json
{
  "id": "string (UUID)",
  "type": "project",
  "attributes": {
    "name": "string",
    "description": "string",
    "status": "draft|active|archived|completed",
    "ownerId": "string (UUID)",
    "createdAt": "ISO8601 timestamp",
    "updatedAt": "ISO8601 timestamp"
  },
  "relationships": {
    "tasks": { "data": [{ "id":"...", "type":"task" }, ...] }
  }
}
```

### 7.2 Task  

```json
{
  "id": "string (UUID)",
  "type": "task",
  "attributes": {
    "title": "string",
    "description": "string",
    "projectId": "string (UUID)",
    "assigneeId": "string (UUID)",
    "dueDate": "ISO8601 or null",
    "priority": "LOW|MEDIUM|HIGH",
    "status": "todo|in_progress|blocked|done",
    "tags": ["string"],
    "createdAt": "...",
    "updatedAt": "..."
  },
  "relationships": {
    "project": { "data": { "id":"...", "type":"project" } },
    "assignee": { "data": { "id":"...", "type":"user" } },
    "comments": { "data": [{ "id":"...", "type":"comment" }, ...] }
  }
}
```

### 7.3 User (public fields)

```json
{
  "id": "string (UUID)",
  "type": "user",
  "attributes": {
    "email": "string",
    "fullName": "string",
    "avatarUrl": "string|null",
    "role": "admin|member|guest"
  }
}
```

### 7.4 Comment  

```json
{
  "id": "string (UUID)",
  "type": "comment",
  "attributes": {
    "authorId": "string (UUID)",
    "content": "string",
    "createdAt": "ISO8601"
  },
  "relationships": {
    "author": { "data": { "id":"...", "type":"user" } },
    "resource": { "data": { "type":"task|project", "id":"..." } }
  }
}
```

---  

## 8. Error Representation  

All error responses follow **RFC 7807** (`application/problem+json`).

```json
{
  "type": "https://api.projectmanager.example.com/problems/not-found",
  "title": "Resource not found",
  "status": 404,
  "detail": "Task with id '123' does not exist",
  "instance": "/api/v1/tasks/123"
}
```

Common problem types:  

- `bad_request` (400) – validation errors (`detail` includes field errors).  
- `unauthenticated` (401) – missing/invalid token.  
- `forbidden` (403) – insufficient scope.  
- `not_found` (404) – unknown resource.  
- `conflict` (409) – duplicate key, optimistic lock failure.  
- `too_many_requests` (429) – rate limit exceeded (`Retry-After` header).  

---  

## 9. Pagination & Sorting  

- **Pagination**: Use `limit` (max 100) and `cursor` (opaque base64-encoded opaque token) or `offset`/`page`.  
- **Response Header**:  

```
Link: <https://api.projectmanager.example.com/api/v1/projects?cursor=abc>; rel="next",
      <https://api.projectmanager.example.com/api/v1/projects?cursor=xyz>; rel="prev"
```

- **Sorting**: `sort=` query param with comma‑separated fields (`sort=createdAt,-priority`).  

---  

## 10. Request/Response Headers  

| Header | Value | Description |
|--------|-------|-------------|
| `Accept` | `application/json` | Client expects JSON |
| `Content-Type` | `application/json` | Body is JSON |
| `Authorization` | `Bearer <jwt>` | OAuth2 token |
| `X-Request-ID` | UUID | For tracing / logging |
| `User-Agent` | – | Optional client identifier |
| `Prefer` | `return=minimal` | Hint to return only essential fields (if supported) |

---  

## 11. Security Considerations  

1. **HTTPS Only** – Enforced at the load balancer.  
2. **Token Revocation** – Maintain a revocation list for access tokens; refresh tokens are rotated.  
3. **Input Validation** – Server‑side validation of all request bodies (e.g., using JSON Schema).  
4. **CORS** – Whitelist allowed origins; expose `Access-Control-Allow-Origin` header only for those.  
5. **Audit Logging** – Log authentication events, sensitive actions, and admin changes (PII masked).  
6. **Rate Limiting** – Per‑client quotas; back‑off on `429`.  

---  

## 12. Documentation  

- **OpenAPI 3.1** spec served at `/openapi.json`.  
- Interactive Swagger UI at `/docs`.  
- Versioned docs: `/docs/v1/`, `/docs/v2/`.  
- SDK generation (TypeScript, Python, Go) via `openapi-generator`.  

---  

## 13. Testing Strategy  

- **Unit Tests**: Business logic, validation, repository layers.  
- **Integration Tests**: End‑to‑end HTTP flow using `curl`/Postman collections.  
- **Contract Tests**: Verify response schemas against OpenAPI.  
- **Load Tests**: Simulate 10k RPS with realistic payloads (k6 or Gatling).  
- **Security Scans**: OWASP ZAP, static code analysis.  

---  

## 14. Deployment & Operations  

| Concern | Detail |
|---------|--------|
| **Containerization** | Docker images built from `Dockerfile`; multi‑stage builds. |
| **Orchestration** | Deploy to Kubernetes (Helm chart) with `Deployment`, `Service`, `Ingress`. |
| **Config** | Environment variables + ConfigMap for non‑secret settings; secrets via Vault. |
| **Observability** | Prometheus metrics (`http_requests_total`, `request_duration_seconds`), Grafana dashboards, centralized logging (ELK). |
| **Feature Flags** | Use LaunchDarkly‑style flag service for rolling out API changes. |
| **Canary Releases** | Deploy new version under a different path (`/api/v2/`) and route a small % of traffic. |

---  

## 15. Sample OpenAPI Snippet (excerpt)

```yaml
openapi: 3.1.0
info:
  title: Project Management API
  version: 1.0.0
serv