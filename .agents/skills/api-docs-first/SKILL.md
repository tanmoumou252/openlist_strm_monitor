---
name: api-docs-first
description: Use this skill when modifying API calls, request parameters, response parsing, authentication, OpenList Admin API, WebDAV behavior, TMDB API behavior, or any code that should follow local markdown documentation under the docs folder. 中文触发词：API 文档先行、先看文档、OpenList 接口变更、WebDAV 行为、接口契约核对。
---

# API Documentation First Guidelines

## Main rule

Before changing API-related behavior, inspect the local `docs/` folder.

Do not guess endpoint behavior if local markdown documentation exists.

## Applies to

Use this skill for:

- OpenList Admin API
- OpenList WebDAV
- TMDB API
- authentication
- request payloads
- query parameters
- response parsing
- error handling
- API timeout/retry behavior
- storage mapping
- STRM engine path behavior

## Workflow

When modifying API-related code:

1. Locate the relevant markdown document under `docs/`.
2. Identify the endpoint path.
3. Identify the request method.
4. Identify required parameters.
5. Identify optional parameters.
6. Identify authentication requirements.
7. Identify response shape.
8. Compare documentation with existing implementation.
9. Preserve existing working behavior unless the requested change requires modification.
10. Normalize raw API responses before exposing them to services or WebUI.

## OpenList-specific rule

OpenList is self-hosted.

Always consider:

- service unavailable
- authentication failure
- path missing
- storage unavailable
- network timeout
- WebDAV XML parsing failure
- Admin API response differences
- local configuration mismatch

Convert these into project-level statuses instead of leaking raw errors.

## TMDB-specific rule

TMDB integration is already stable in this project.

Do not refactor TMDB code unless explicitly requested.

## WebUI rule

The WebUI should not parse raw API responses.

Backend services should provide stable response objects.

Response contract and project-level status categories are owned by the `openlist-strm-bridge` skill（见其 "WebUI data contract" 节，以 `src/webui/routes.py` 实际返回风格为准）.
