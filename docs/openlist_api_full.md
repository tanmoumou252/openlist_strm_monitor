---
title: 默认模块
language_tabs:
  - shell: Shell
  - http: HTTP
  - javascript: JavaScript
  - ruby: Ruby
  - python: Python
  - php: PHP
  - java: Java
  - go: Go
toc_footers: []
includes: []
search: true
code_clipboard: true
highlight_theme: darkula
headingLevel: 2
generator: "@tarslib/widdershins v4.0.30"

---

# 默认模块

Welcome to the **OpenList Project API documentation**!

OpenList is a resilient, community-driven fork of AList, built to ensure long-term freedom and trust in open-source file listing systems amid the Open Source Trust Crisis.

**GitHub Repository**: https://github.com/OpenListTeam/OpenList

## Authentication

Most endpoints require JWT authentication. Include the token in the Authorization header:
```
Authorization: <token>
```

Obtain a token via `/api/auth/login` endpoint.

## Response Format

All responses follow this structure:
```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

Error responses:
```json
{
  "code": 400,
  "message": "error message",
  "data": null
}
```

Base URLs:

Web: <a href="https://github.com/OpenListTeam/OpenList">OpenList Team</a> 
License: <a href="https://github.com/OpenListTeam/OpenList/blob/main/LICENSE">AGPL-3.0</a>

# Authentication

- HTTP Authentication, scheme: bearer<br/>JWT token obtained from login endpoint.
Include the token directly (without "Bearer" prefix) in Authorization header.

# Authentication

<a id="opIdpostAuthlogin"></a>

## POST User login

POST /api/auth/login

Authenticate user with username and password, returns JWT token

> Body Parameters

```json
{
  "username": "admin",
  "password": "my password",
  "otp_code": "123456"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[LoginRequest](#schemaloginrequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Login successful|[LoginResponse](#schemaloginresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid request or wrong credentials|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAuthloginhash"></a>

## POST User login with pre-hashed password

POST /api/auth/login/hash

Authenticate using username and pre-hashed password (SHA256)

> Body Parameters

```json
{
  "username": "admin",
  "password": "hashed_password_string",
  "otp_code": "string"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» username|body|string| yes |none|
|» password|body|string| yes |SHA256 hash of password|
|» otp_code|body|string| no |2FA code if enabled|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Login successful|[LoginResponse](#schemaloginresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Authentication failed|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAuthloginldap"></a>

## POST LDAP login

POST /api/auth/login/ldap

Authenticate user via LDAP directory service

> Body Parameters

```json
{
  "username": "admin",
  "password": "my password",
  "otp_code": "123456"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[LoginRequest](#schemaloginrequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Login successful|[LoginResponse](#schemaloginresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|LDAP authentication failed|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetAuthlogout"></a>

## GET User logout

GET /api/auth/logout

Invalidate current session token

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Logout successful|[ApiResponse](#schemaapiresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAuth2fagenerate"></a>

## POST Generate 2FA secret

POST /api/auth/2fa/generate

Generate a new 2FA (TOTP) secret for current user

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "qr_code": "string",
    "secret": "string"
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|2FA secret generated|Inline|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

### Responses Data Schema

HTTP Status Code **401**

|Name|Type|Required|Restrictions|Title|description|
|---|---|---|---|---|---|
|» code|integer|true|none||Error code|
|» message|string|true|none||Error message|
|» data|object¦null|false|none||none|

<a id="opIdpostAuth2faverify"></a>

## POST Verify and enable 2FA

POST /api/auth/2fa/verify

Verify TOTP code and enable 2FA for current user

> Body Parameters

```json
{
  "code": "123456"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» code|body|string| yes |6-digit TOTP code|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|2FA enabled successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid verification code|[ErrorResponse](#schemaerrorresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetAuthsso"></a>

## GET SSO login redirect

GET /api/auth/sso

Redirect to configured SSO provider for authentication

> Response Examples

> 400 Response

```json
{
  "code": 400,
  "message": "Invalid request parameters",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|302|[Found](https://tools.ietf.org/html/rfc7231#section-6.4.3)|Redirect to SSO provider|None|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|SSO not configured|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetAuthsso_callback"></a>

## GET SSO callback handler

GET /api/auth/sso_callback

Handle callback from SSO provider after authentication

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|code|query|string| yes |Authorization code from SSO provider|
|state|query|string| no |State parameter for CSRF protection|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|SSO login successful|[LoginResponse](#schemaloginresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|SSO callback failed|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetAuthnwebauthn_begin_login"></a>

## GET Begin WebAuthn login

GET /api/authn/webauthn_begin_login

Initiate WebAuthn (FIDO2/Passkey) authentication flow

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|username|query|string| yes |Username for WebAuthn login|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|WebAuthn challenge generated|Inline|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|WebAuthn not enabled or user not found|[ErrorResponse](#schemaerrorresponse)|

### Responses Data Schema

HTTP Status Code **400**

|Name|Type|Required|Restrictions|Title|description|
|---|---|---|---|---|---|
|» code|integer|true|none||Error code|
|» message|string|true|none||Error message|
|» data|object¦null|false|none||none|

<a id="opIdpostAuthnwebauthn_finish_login"></a>

## POST Finish WebAuthn login

POST /api/authn/webauthn_finish_login

Complete WebAuthn authentication with signed challenge

> Body Parameters

```json
{}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|WebAuthn login successful|[LoginResponse](#schemaloginresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|WebAuthn verification failed|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetAuthnwebauthn_begin_registration"></a>

## GET Begin WebAuthn registration

GET /api/authn/webauthn_begin_registration

Start registering a new WebAuthn credential (requires authentication)

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|WebAuthn registration challenge generated|Inline|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

### Responses Data Schema

HTTP Status Code **401**

|Name|Type|Required|Restrictions|Title|description|
|---|---|---|---|---|---|
|» code|integer|true|none||Error code|
|» message|string|true|none||Error message|
|» data|object¦null|false|none||none|

<a id="opIdpostAuthnwebauthn_finish_registration"></a>

## POST Finish WebAuthn registration

POST /api/authn/webauthn_finish_registration

Complete WebAuthn credential registration

> Body Parameters

```json
{}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|WebAuthn credential registered successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Registration failed|[ErrorResponse](#schemaerrorresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAuthndelete_authn"></a>

## POST Delete WebAuthn credential

POST /api/authn/delete_authn

Remove a registered WebAuthn credential

> Body Parameters

```json
{
  "id": "credential_id_base64"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|string| yes |Base64-encoded credential ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Credential deleted successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid request|[ErrorResponse](#schemaerrorresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetAuthngetcredentials"></a>

## GET Get WebAuthn credentials

GET /api/authn/getcredentials

List all registered WebAuthn credentials for current user

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    {
      "id": "string",
      "created_at": "2019-08-24T14:15:22Z"
    }
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Credentials list retrieved|Inline|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

### Responses Data Schema

HTTP Status Code **401**

|Name|Type|Required|Restrictions|Title|description|
|---|---|---|---|---|---|
|» code|integer|true|none||Error code|
|» message|string|true|none||Error message|
|» data|object¦null|false|none||none|

# User

<a id="opIdgetMe"></a>

## GET Get current user info

GET /api/me

Retrieve information about the currently authenticated user

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": 1,
    "username": "admin",
    "password": "",
    "base_path": "/",
    "role": 2,
    "disabled": false,
    "permission": 29183,
    "sso_id": "",
    "otp": false
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|User info retrieved successfully|[UserResponse](#schemauserresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostMeupdate"></a>

## POST Update current user

POST /api/me/update

Update password or other settings for current user

> Body Parameters

```json
{
  "password": "string",
  "old_password": "string"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» password|body|string| no |New password|
|» old_password|body|string| no |Current password (required for password change)|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|User updated successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid request or wrong old password|[ErrorResponse](#schemaerrorresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetMesshkeylist"></a>

## GET List my SSH public keys

GET /api/me/sshkey/list

Get list of SSH public keys for current user

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    {
      "id": 0,
      "name": "string",
      "public_key": "string",
      "created_at": "2019-08-24T14:15:22Z"
    }
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|SSH keys retrieved|Inline|

### Responses Data Schema

<a id="opIdpostMesshkeyadd"></a>

## POST Add SSH public key

POST /api/me/sshkey/add

Add a new SSH public key for current user

> Body Parameters

```json
{
  "name": "my-laptop",
  "public_key": "ssh-rsa AAAAB3NzaC1..."
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» name|body|string| yes |Key name/label|
|» public_key|body|string| yes |SSH public key content|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|SSH key added successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid public key format|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostMesshkeydelete"></a>

## POST Delete SSH public key

POST /api/me/sshkey/delete

Remove an SSH public key from current user

> Body Parameters

```json
{
  "id": 1
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |SSH key ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|SSH key deleted successfully|[ApiResponse](#schemaapiresponse)|
|404|[Not Found](https://tools.ietf.org/html/rfc7231#section-6.5.4)|SSH key not found|[ErrorResponse](#schemaerrorresponse)|

# Admin

<a id="opIdgetAdminuserlist"></a>

## GET List all users (Admin)

GET /api/admin/user/list

Get paginated list of all users

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|page|query|integer| no |none|
|per_page|query|integer| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": 1,
        "username": "admin",
        "password": "",
        "base_path": "/",
        "role": 2,
        "disabled": false,
        "permission": 29183,
        "sso_id": "",
        "otp": false
      }
    ],
    "total": 5
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Users list retrieved|[UsersListResponse](#schemauserslistresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Forbidden - Admin role required|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetAdminuserget"></a>

## GET Get user by ID (Admin)

GET /api/admin/user/get

Retrieve specific user information

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|id|query|integer| yes |User ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": 1,
    "username": "admin",
    "password": "",
    "base_path": "/",
    "role": 2,
    "disabled": false,
    "permission": 29183,
    "sso_id": "",
    "otp": false
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|User info retrieved|[UserResponse](#schemauserresponse)|
|404|[Not Found](https://tools.ietf.org/html/rfc7231#section-6.5.4)|User not found|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAdminusercreate"></a>

## POST Create new user (Admin)

POST /api/admin/user/create

Create a new user account

> Body Parameters

```json
{
  "username": "newuser",
  "password": "password123",
  "base_path": "/",
  "role": 0,
  "permission": 0,
  "disabled": false
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» username|body|string| yes |none|
|» password|body|string| yes |none|
|» base_path|body|string| no |none|
|» role|body|integer| no |0=General, 1=Guest, 2=Admin|
|» permission|body|integer| no |Permission bitmap|
|» disabled|body|boolean| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|User created successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid request or username already exists|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAdminuserupdate"></a>

## POST Update user (Admin)

POST /api/admin/user/update

Update user information

> Body Parameters

```json
{
  "id": 0,
  "username": "string",
  "password": "string",
  "base_path": "string",
  "role": 0,
  "permission": 0,
  "disabled": true
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |none|
|» username|body|string| no |none|
|» password|body|string| no |none|
|» base_path|body|string| no |none|
|» role|body|integer| no |none|
|» permission|body|integer| no |none|
|» disabled|body|boolean| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|User updated successfully|[ApiResponse](#schemaapiresponse)|
|404|[Not Found](https://tools.ietf.org/html/rfc7231#section-6.5.4)|User not found|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAdminuserdelete"></a>

## POST Delete user (Admin)

POST /api/admin/user/delete

Delete a user account

> Body Parameters

```json
{
  "id": 0
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |User ID to delete|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|User deleted successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Cannot delete admin user|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAdminusercancel_2fa"></a>

## POST Cancel user 2FA (Admin)

POST /api/admin/user/cancel_2fa

Disable 2FA for a specific user

> Body Parameters

```json
{
  "id": 0
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |User ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|2FA cancelled successfully|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminuserdel_cache"></a>

## POST Clear user cache (Admin)

POST /api/admin/user/del_cache

Clear cached data for a specific user

> Body Parameters

```json
{
  "username": "string"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» username|body|string| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|User cache cleared|[ApiResponse](#schemaapiresponse)|

<a id="opIdgetAdminusersshkeylist"></a>

## GET List user SSH keys (Admin)

GET /api/admin/user/sshkey/list

Get SSH keys for any user

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|user_id|query|integer| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    {}
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|SSH keys retrieved|Inline|

### Responses Data Schema

<a id="opIdpostAdminusersshkeydelete"></a>

## POST Delete user SSH key (Admin)

POST /api/admin/user/sshkey/delete

Delete SSH key for any user

> Body Parameters

```json
{
  "id": 0
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |SSH key ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|SSH key deleted|[ApiResponse](#schemaapiresponse)|

<a id="opIdgetAdminstoragelist"></a>

## GET List all storages (Admin)

GET /api/admin/storage/list

Get list of all configured storage mounts

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|page|query|integer| no |none|
|per_page|query|integer| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": 1,
        "mount_path": "/local",
        "order": 0,
        "driver": "Local",
        "cache_expiration": 30,
        "status": "work",
        "addition": "{\"root_folder_path\":\"D:\\\\files\"}",
        "remark": "Local storage",
        "modified": "2019-08-24T14:15:22Z",
        "disabled": false,
        "disable_index": false,
        "enable_sign": false,
        "order_by": "name",
        "order_direction": "asc",
        "extract_folder": "front",
        "web_proxy": false,
        "webdav_policy": "native_proxy",
        "down_proxy_url": ""
      }
    ],
    "total": 0
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Storages list retrieved|[StoragesListResponse](#schemastorageslistresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Forbidden - Admin required|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdgetAdminstorageget"></a>

## GET Get storage by ID (Admin)

GET /api/admin/storage/get

Retrieve specific storage configuration

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|id|query|integer| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": 1,
    "mount_path": "/local",
    "order": 0,
    "driver": "Local",
    "cache_expiration": 30,
    "status": "work",
    "addition": "{\"root_folder_path\":\"D:\\\\files\"}",
    "remark": "Local storage",
    "modified": "2019-08-24T14:15:22Z",
    "disabled": false,
    "disable_index": false,
    "enable_sign": false,
    "order_by": "name",
    "order_direction": "asc",
    "extract_folder": "front",
    "web_proxy": false,
    "webdav_policy": "native_proxy",
    "down_proxy_url": ""
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Storage info retrieved|[StorageResponse](#schemastorageresponse)|
|404|[Not Found](https://tools.ietf.org/html/rfc7231#section-6.5.4)|Storage not found|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAdminstoragecreate"></a>

## POST Create storage (Admin)

POST /api/admin/storage/create

Add a new storage mount

> Body Parameters

```json
{
  "mount_path": "/local",
  "driver": "Local",
  "order": 0,
  "cache_expiration": 30,
  "addition": "string",
  "remark": "string"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» mount_path|body|string| yes |none|
|» driver|body|string| yes |none|
|» order|body|integer| no |none|
|» cache_expiration|body|integer| no |none|
|» addition|body|string| no |Driver-specific config (JSON string)|
|» remark|body|string| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Storage created successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid configuration or mount path already exists|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAdminstorageupdate"></a>

## POST Update storage (Admin)

POST /api/admin/storage/update

Modify existing storage configuration

> Body Parameters

```json
{
  "id": 1,
  "mount_path": "/local",
  "order": 0,
  "driver": "Local",
  "cache_expiration": 30,
  "status": "work",
  "addition": "{\"root_folder_path\":\"D:\\\\files\"}",
  "remark": "Local storage",
  "modified": "2019-08-24T14:15:22Z",
  "disabled": false,
  "disable_index": false,
  "enable_sign": false,
  "order_by": "name",
  "order_direction": "asc",
  "extract_folder": "front",
  "web_proxy": false,
  "webdav_policy": "native_proxy",
  "down_proxy_url": ""
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|any| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Storage updated successfully|[ApiResponse](#schemaapiresponse)|
|404|[Not Found](https://tools.ietf.org/html/rfc7231#section-6.5.4)|Storage not found|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostAdminstoragedelete"></a>

## POST Delete storage (Admin)

POST /api/admin/storage/delete

Remove a storage mount

> Body Parameters

```json
{
  "id": 0
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Storage deleted successfully|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminstorageenable"></a>

## POST Enable storage (Admin)

POST /api/admin/storage/enable

Enable a disabled storage mount

> Body Parameters

```json
{
  "id": 0
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Storage enabled|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminstoragedisable"></a>

## POST Disable storage (Admin)

POST /api/admin/storage/disable

Temporarily disable a storage mount

> Body Parameters

```json
{
  "id": 0
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Storage disabled|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminstorageload_all"></a>

## POST Reload all storages (Admin)

POST /api/admin/storage/load_all

Force reload all storage mounts

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Storages reloaded|[ApiResponse](#schemaapiresponse)|

<a id="opIdgetAdmindriverlist"></a>

## GET List all drivers (Admin)

GET /api/admin/driver/list

Get list of available storage drivers with their configurations

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    {
      "name": "Local",
      "config": {}
    }
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Drivers list retrieved|Inline|

### Responses Data Schema

<a id="opIdgetAdmindrivernames"></a>

## GET Get driver names (Admin)

GET /api/admin/driver/names

Get list of available driver names

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    "Local",
    "AliyunDrive",
    "OneDrive",
    "S3"
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Driver names retrieved|Inline|

### Responses Data Schema

<a id="opIdgetAdmindriverinfo"></a>

## GET Get driver info (Admin)

GET /api/admin/driver/info

Get configuration schema for a specific driver

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|driver|query|string| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "name": "Local",
    "config": {}
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Driver info retrieved|Inline|
|404|[Not Found](https://tools.ietf.org/html/rfc7231#section-6.5.4)|Driver not found|[ErrorResponse](#schemaerrorresponse)|

### Responses Data Schema

HTTP Status Code **404**

|Name|Type|Required|Restrictions|Title|description|
|---|---|---|---|---|---|
|» code|integer|true|none||Error code|
|» message|string|true|none||Error message|
|» data|object¦null|false|none||none|

<a id="opIdgetAdminsettinglist"></a>

## GET List all settings (Admin)

GET /api/admin/setting/list

Get all system settings

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    {
      "key": "string",
      "value": "string",
      "type": "string",
      "options": "string",
      "group": 0,
      "flag": 0
    }
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Settings list retrieved|Inline|

### Responses Data Schema

<a id="opIdgetAdminsettingget"></a>

## GET Get setting by key (Admin)

GET /api/admin/setting/get

Retrieve a specific setting value

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|key|query|string| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Setting retrieved|Inline|

### Responses Data Schema

<a id="opIdpostAdminsettingsave"></a>

## POST Save settings (Admin)

POST /api/admin/setting/save

Update one or more system settings

> Body Parameters

```json
[
  {
    "key": "string",
    "value": "string"
  }
]
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|array[object]| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Settings saved successfully|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminsettingdelete"></a>

## POST Delete setting (Admin)

POST /api/admin/setting/delete

Remove a custom setting

> Body Parameters

```json
{
  "key": "string"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» key|body|string| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Setting deleted|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminsettingreset_token"></a>

## POST Reset API token (Admin)

POST /api/admin/setting/reset_token

Generate a new API token

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Token reset successfully|[ApiResponse](#schemaapiresponse)|

<a id="opIdgetAdminmetalist"></a>

## GET List all metas (Admin)

GET /api/admin/meta/list

Get list of metadata configurations

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    {}
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Metas list retrieved|Inline|

### Responses Data Schema

<a id="opIdgetAdminmetaget"></a>

## GET Get meta by ID (Admin)

GET /api/admin/meta/get

Retrieve specific metadata configuration

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|id|query|integer| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Meta retrieved|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminmetacreate"></a>

## POST Create meta (Admin)

POST /api/admin/meta/create

Add new metadata configuration

> Body Parameters

```json
{
  "path": "string",
  "password": "string",
  "readme": "string",
  "header": "string",
  "hide": "string"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» path|body|string| yes |none|
|» password|body|string| no |none|
|» readme|body|string| no |none|
|» header|body|string| no |none|
|» hide|body|string| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Meta created|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminmetaupdate"></a>

## POST Update meta (Admin)

POST /api/admin/meta/update

Modify metadata configuration

> Body Parameters

```json
{
  "id": 0,
  "path": "string",
  "password": "string",
  "readme": "string",
  "header": "string",
  "hide": "string"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |none|
|» path|body|string| no |none|
|» password|body|string| no |none|
|» readme|body|string| no |none|
|» header|body|string| no |none|
|» hide|body|string| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Meta updated|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminmetadelete"></a>

## POST Delete meta (Admin)

POST /api/admin/meta/delete

Remove metadata configuration

> Body Parameters

```json
{
  "id": 0
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|integer| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Meta deleted|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminindexbuild"></a>

## POST Build search index (Admin)

POST /api/admin/index/build

Build full-text search index for all storages

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Index build started|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminindexupdate"></a>

## POST Update search index (Admin)

POST /api/admin/index/update

Update search index for specific paths

> Body Parameters

```json
{
  "paths": [
    "string"
  ]
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» paths|body|[string]| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Index update started|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminindexstop"></a>

## POST Stop indexing (Admin)

POST /api/admin/index/stop

Stop current indexing operation

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Indexing stopped|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostAdminindexclear"></a>

## POST Clear search index (Admin)

POST /api/admin/index/clear

Delete all search index data

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Index cleared|[ApiResponse](#schemaapiresponse)|

<a id="opIdgetAdminindexprogress"></a>

## GET Get indexing progress (Admin)

GET /api/admin/index/progress

Check current indexing operation progress

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "total": 0,
    "current": 0,
    "status": "string"
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Progress retrieved|Inline|

### Responses Data Schema

# File System

<a id="opIdpostFslist"></a>

## POST List directory contents

POST /api/fs/list

Get list of files and directories at specified path with pagination

> Body Parameters

```json
{
  "path": "/",
  "password": "",
  "refresh": false,
  "page": 1,
  "per_page": 30
}
```

```yaml
path: /
password: ""
refresh: false
page: 1
per_page: 30

```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[FsListRequest](#schemafslistrequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": "",
        "path": "D:\\files\\document.pdf",
        "name": "document.pdf",
        "size": 1024000,
        "is_dir": false,
        "modified": "2025-10-20T15:30:00+08:00",
        "created": "2025-10-20T10:00:00+08:00",
        "sign": "YBgnmykwCXUstXvNGtECaz_12gseXSL03cpqh5rTcGA=:0",
        "thumb": "",
        "type": 4,
        "hashinfo": "null",
        "hash_info": null,
        "mount_details": {}
      }
    ],
    "total": 14,
    "readme": "",
    "header": "",
    "write": true,
    "provider": "Local"
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Directory listing retrieved successfully|[FsListResponse](#schemafslistresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Forbidden - insufficient permissions or wrong password|[ErrorResponse](#schemaerrorresponse)|
|404|[Not Found](https://tools.ietf.org/html/rfc7231#section-6.5.4)|Path not found|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsget"></a>

## POST Get file or directory info

POST /api/fs/get

Retrieve metadata for a specific file or directory

> Body Parameters

```json
{
  "path": "/document.pdf",
  "password": ""
}
```

```yaml
path: /document.pdf
password: ""

```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[FsGetRequest](#schemafsgetrequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": "",
    "path": "D:\\files\\document.pdf",
    "name": "document.pdf",
    "size": 1024000,
    "is_dir": false,
    "modified": "2025-10-20T15:30:00+08:00",
    "created": "2025-10-20T10:00:00+08:00",
    "sign": "YBgnmykwCXUstXvNGtECaz_12gseXSL03cpqh5rTcGA=:0",
    "thumb": "",
    "type": 4,
    "hashinfo": "null",
    "hash_info": null,
    "mount_details": {
      "driver_name": "Local",
      "total_space": 1000000000000,
      "free_space": 500000000000
    }
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|File/directory info retrieved|[FsGetResponse](#schemafsgetresponse)|
|401|[Unauthorized](https://tools.ietf.org/html/rfc7235#section-3.1)|Unauthorized|[ErrorResponse](#schemaerrorresponse)|
|404|[Not Found](https://tools.ietf.org/html/rfc7231#section-6.5.4)|File not found|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFssearch"></a>

## POST Search files and directories

POST /api/fs/search

Search for files/folders by name (requires search index to be built)

> Body Parameters

```json
{
  "parent": "/",
  "keywords": "document",
  "scope": 0,
  "page": 1,
  "per_page": 30
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» parent|body|string| yes |Base path to search within|
|» keywords|body|string| yes |Search query|
|» scope|body|integer| no |Search scope (0=current folder, 1=recursive)|
|» page|body|integer| no |none|
|» per_page|body|integer| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": "",
        "path": "D:\\files\\document.pdf",
        "name": "document.pdf",
        "size": 1024000,
        "is_dir": false,
        "modified": "2025-10-20T15:30:00+08:00",
        "created": "2025-10-20T10:00:00+08:00",
        "sign": "YBgnmykwCXUstXvNGtECaz_12gseXSL03cpqh5rTcGA=:0",
        "thumb": "",
        "type": 4,
        "hashinfo": "null",
        "hash_info": null,
        "mount_details": {}
      }
    ],
    "total": 0
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Search results|Inline|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Search index not enabled|[ErrorResponse](#schemaerrorresponse)|

### Responses Data Schema

HTTP Status Code **400**

|Name|Type|Required|Restrictions|Title|description|
|---|---|---|---|---|---|
|» code|integer|true|none||Error code|
|» message|string|true|none||Error message|
|» data|object¦null|false|none||none|

<a id="opIdpostFsdirs"></a>

## POST Get directory tree

POST /api/fs/dirs

Retrieve directory structure (folders only) for navigation

> Body Parameters

```json
{
  "path": "/",
  "password": "string",
  "force_root": false
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» path|body|string| no |none|
|» password|body|string| no |none|
|» force_root|body|boolean| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    {
      "name": "string",
      "path": "string"
    }
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Directory tree retrieved|Inline|

### Responses Data Schema

<a id="opIdpostFsother"></a>

## POST Get additional file operations

POST /api/fs/other

Retrieve provider-specific operations available for a file/folder

> Body Parameters

```json
{
  "path": "/file.txt",
  "method": "string"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» path|body|string| yes |none|
|» method|body|string| no |Operation method name|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Operations retrieved|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostFsmkdir"></a>

## POST Create directory

POST /api/fs/mkdir

Create a new directory at specified path

> Body Parameters

```json
{
  "path": "/newfolder"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[FsMkdirRequest](#schemafsmkdirrequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Directory created successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid path or directory already exists|[ErrorResponse](#schemaerrorresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsrename"></a>

## POST Rename file or directory

POST /api/fs/rename

Rename a file or directory

> Body Parameters

```json
{
  "path": "/oldname.txt",
  "name": "newname.txt"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[FsRenameRequest](#schemafsrenamerequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Renamed successfully|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid request|[ErrorResponse](#schemaerrorresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsbatch_rename"></a>

## POST Batch rename files

POST /api/fs/batch_rename

Rename multiple files using a pattern

> Body Parameters

```json
{
  "src_dir": "/folder",
  "rename_objects": [
    {
      "src_name": "string",
      "new_name": "string"
    }
  ]
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» src_dir|body|string| yes |Source directory|
|» rename_objects|body|[object]| yes |none|
|»» src_name|body|string| no |none|
|»» new_name|body|string| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Batch rename completed|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostFsregex_rename"></a>

## POST Regex-based rename

POST /api/fs/regex_rename

Rename files using regular expression pattern matching

> Body Parameters

```json
{
  "src_dir": "/folder",
  "src_name_regex": "^(.*)\\.(txt)$",
  "new_name_regex": "$1_renamed.$2"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» src_dir|body|string| yes |none|
|» src_name_regex|body|string| yes |Source name regex pattern|
|» new_name_regex|body|string| yes |Replacement pattern|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Regex rename completed|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostFsmove"></a>

## POST Move files or directories

POST /api/fs/move

Move one or more files/folders to another location

> Body Parameters

```json
{
  "src_dir": "/source",
  "dst_dir": "/destination",
  "names": [
    "file1.txt",
    "file2.pdf"
  ]
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[FsMoveCopyRequest](#schemafsmovecopyrequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Moved successfully|[ApiResponse](#schemaapiresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsrecursive_move"></a>

## POST Recursive move

POST /api/fs/recursive_move

Move files/folders recursively (preserves directory structure)

> Body Parameters

```json
{
  "src_dir": "/source",
  "dst_dir": "/destination"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» src_dir|body|string| yes |none|
|» dst_dir|body|string| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Recursive move completed|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostFscopy"></a>

## POST Copy files or directories

POST /api/fs/copy

Copy one or more files/folders to another location

> Body Parameters

```json
{
  "src_dir": "/source",
  "dst_dir": "/destination",
  "names": [
    "file1.txt",
    "file2.pdf"
  ]
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[FsMoveCopyRequest](#schemafsmovecopyrequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Copied successfully|[ApiResponse](#schemaapiresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsremove"></a>

## POST Remove files or directories

POST /api/fs/remove

Delete one or more files or folders

> Body Parameters

```json
{
  "dir": "/folder",
  "names": [
    "file1.txt",
    "file2.pdf"
  ]
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|[FsRemoveRequest](#schemafsremoverequest)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Removed successfully|[ApiResponse](#schemaapiresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsremove_empty_directory"></a>

## POST Remove empty directories

POST /api/fs/remove_empty_directory

Recursively remove empty directories

> Body Parameters

```json
{
  "src_dir": "/folder"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» src_dir|body|string| yes |Starting directory path|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Empty directories removed|[ApiResponse](#schemaapiresponse)|

<a id="opIdputFsput"></a>

## PUT Upload file (stream)

PUT /api/fs/put

Upload file using streaming (for large files or programmatic uploads)

> Body Parameters

```yaml
string

```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|File-Path|header|string| yes |Destination file path|
|As-Task|header|boolean| no |Upload as background task (true/false)|
|Overwrite|header|string| no |Allow Overwrite (`true` by default)|
|Last-Modified|header|string| no |File modification time (Unix timestamp in milliseconds)|
|X-File-Md5|header|string| no |MD5 of file|
|X-File-Sha1|header|string| no |SHA1 of file|
|X-File-Sha256|header|string| no |SHA256 of file|
|body|body|string(binary)| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Upload successful|[ApiResponse](#schemaapiresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdputFsform"></a>

## PUT Upload file (form)

PUT /api/fs/form

Upload file using multipart form data (for browser uploads)

> Body Parameters

```yaml
file: ""

```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|File-Path|header|string| yes |Destination file path|
|As-Task|header|boolean| no |Upload as background task (true/false)|
|Overwrite|header|boolean| no |Allow Overwrite (`true` by default)|
|Last-Modified|header|string| no |File modification time (Unix timestamp in milliseconds)|
|X-File-Md5|header|string| no |MD5 of file|
|X-File-Sha1|header|string| no |SHA1 of file|
|X-File-Sha256|header|string| no |SHA256 of file|
|body|body|object| yes |none|
|» file|body|string(binary)| no |File to upload|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Upload successful|[ApiResponse](#schemaapiresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsadd_offline_download"></a>

## POST Add offline download task

POST /api/fs/add_offline_download

Create an offline download task (HTTP/magnet/torrent)

> Body Parameters

```json
{
  "path": "/downloads",
  "urls": [
    "https://example.com/file.zip"
  ],
  "tool": "aria2"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» path|body|string| yes |Destination path to save downloaded files|
|» urls|body|[string]| yes |List of URLs to download (HTTP/HTTPS/magnet)|
|» tool|body|string| no |Preferred download tool (aria2/qbittorrent/transmission)|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Download task created|[ApiResponse](#schemaapiresponse)|
|400|[Bad Request](https://tools.ietf.org/html/rfc7231#section-6.5.1)|Invalid request or offline download not configured|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsarchivedecompress"></a>

## POST Decompress archive

POST /api/fs/archive/decompress

Extract archive file (zip/rar/7z/tar/tar.gz etc.)

> Body Parameters

```json
{
  "src_dir": "/archives",
  "name": "archive.zip",
  "dst_dir": "/extracted"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» src_dir|body|string| yes |Directory containing the archive|
|» name|body|string| yes |Archive filename|
|» dst_dir|body|string| yes |Destination directory for extraction|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Decompression started|[ApiResponse](#schemaapiresponse)|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

<a id="opIdpostFsarchivemeta"></a>

## POST Get archive metadata

POST /api/fs/archive/meta

Retrieve metadata of archive file without extracting

> Body Parameters

```json
{
  "path": "/archive.zip"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» path|body|string| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "format": "zip",
    "encrypted": true,
    "total_files": 0
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Archive metadata retrieved|Inline|

### Responses Data Schema

<a id="opIdpostFsarchivelist"></a>

## POST List archive contents

POST /api/fs/archive/list

List files inside an archive without extracting

> Body Parameters

```json
{
  "path": "/archive.zip",
  "archive_path": "/"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» path|body|string| yes |Path to archive file|
|» archive_path|body|string| no |Path inside archive|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": "",
        "path": "D:\\files\\document.pdf",
        "name": "document.pdf",
        "size": 1024000,
        "is_dir": false,
        "modified": "2025-10-20T15:30:00+08:00",
        "created": "2025-10-20T10:00:00+08:00",
        "sign": "YBgnmykwCXUstXvNGtECaz_12gseXSL03cpqh5rTcGA=:0",
        "thumb": "",
        "type": 4,
        "hashinfo": "null",
        "hash_info": null,
        "mount_details": {}
      }
    ],
    "total": 0
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Archive contents listed|Inline|

### Responses Data Schema

# Public

<a id="opIdgetPublicsettings"></a>

## GET Get public settings

GET /api/public/settings

Retrieve public system settings (no authentication required)

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "property1": "string",
    "property2": "string"
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Public settings retrieved|Inline|

### Responses Data Schema

<a id="opIdgetPublicoffline_download_tools"></a>

## GET Get available offline download tools

GET /api/public/offline_download_tools

List configured offline download tools (aria2, qbittorrent, etc.)

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    "aria2",
    "qbittorrent"
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Available tools retrieved|Inline|

### Responses Data Schema

<a id="opIdgetPublicarchive_extensions"></a>

## GET Get supported archive extensions

GET /api/public/archive_extensions

List archive file extensions that can be extracted

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": [
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".tar.gz"
  ]
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Archive extensions retrieved|Inline|

### Responses Data Schema

# Sharing

<a id="opIdpostSharelist"></a>

## POST List all shares

POST /api/share/list

Get paginated list of file shares created by current user

> Body Parameters

```json
{
  "page": 1,
  "per_page": 30
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|any| yes |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": "string",
        "path": "string",
        "expiration": "2019-08-24T14:15:22Z",
        "created_at": "2019-08-24T14:15:22Z"
      }
    ],
    "total": 0
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Shares list retrieved|Inline|

### Responses Data Schema

<a id="opIdgetShareget"></a>

## GET Get share by ID

GET /api/share/get

Retrieve specific share information

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|id|query|string| yes |Share ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": "string",
    "path": "string",
    "password": "string",
    "expiration": "2019-08-24T14:15:22Z",
    "created_at": "2019-08-24T14:15:22Z"
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Share info retrieved|Inline|

### Responses Data Schema

<a id="opIdpostSharecreate"></a>

## POST Create file share

POST /api/share/create

Create a new shareable link for files/folders

> Body Parameters

```json
{
  "paths": [
    "/document.pdf",
    "/folder"
  ],
  "password": "",
  "expiration": "2025-12-31T23:59:59Z"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» paths|body|[string]| yes |List of file/folder paths to share|
|» password|body|string| no |Optional password protection|
|» expiration|body|string(date-time)| no |Optional expiration date|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": "string",
    "url": "string"
  }
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Share created successfully|Inline|
|403|[Forbidden](https://tools.ietf.org/html/rfc7231#section-6.5.3)|Insufficient permissions|[ErrorResponse](#schemaerrorresponse)|

### Responses Data Schema

HTTP Status Code **403**

|Name|Type|Required|Restrictions|Title|description|
|---|---|---|---|---|---|
|» code|integer|true|none||Error code|
|» message|string|true|none||Error message|
|» data|object¦null|false|none||none|

<a id="opIdpostShareupdate"></a>

## POST Update share

POST /api/share/update

Modify existing share configuration

> Body Parameters

```json
{
  "id": "string",
  "password": "string",
  "expiration": "2019-08-24T14:15:22Z"
}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|body|body|object| yes |none|
|» id|body|string| yes |none|
|» password|body|string| no |none|
|» expiration|body|string(date-time)| no |none|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Share updated|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostSharedelete"></a>

## POST Delete share

POST /api/share/delete

Remove a file share

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|id|query|string| yes |Share ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Share deleted|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostShareenable"></a>

## POST Enable share

POST /api/share/enable

Re-enable a disabled share

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|id|query|string| yes |Share ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Share enabled|[ApiResponse](#schemaapiresponse)|

<a id="opIdpostSharedisable"></a>

## POST Disable share

POST /api/share/disable

Temporarily disable a share

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|id|query|string| yes |Share ID|

> Response Examples

> 200 Response

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|Share disabled|[ApiResponse](#schemaapiresponse)|

# TS版本接口

## GET 文件操作接口

GET /@file/{action}/{method}/{source}

## 接口用途
负责和后端文件系统对接

## 接口示例
### 列出目录

| 示例操作                           | 含义                                          |
| :--------------------------------- | :-------------------------------------------- |
| `/@file/list/uuid/123456?driver=0` | 以`UUID`方式列出`UUID`为`12345`的目录下的文件 |
| `/@file/list/path/path1/sub_path/` | 列出`/path1/sub_path/`路径的目录下的文件      |

### 获取链接

| 示例操作                           | 含义                                      |
| :--------------------------------- | :---------------------------------------- |
| `/@file/link/uuid/123456?driver=0` | 以`UUID`方式获取`UUID`为`12345`的文件地址 |
| `/@file/link/path/path1/file1.mp4` | 获取`/path1/file1.mp4`路径的文件下载URL   |

### 复制文件(夹)

| 示例操作                                                | 含义                                                         |
| :------------------------------------------------------ | :----------------------------------------------------------- |
| `/@file/copy/uuid/123456?driver=0&target=/path/path2/`  | 以`UUID`方式复制`UUID`为`12345`的文件到`/path2/`             |
| `/@file/copy/path/path1/file1.mp4&target=/uuid/789456/` | 复制`/path1/file1.mp4`路径的文件到按`UUID`为`789456`的目录 下 |

### 移动文件(夹)

| 示例操作                                                | 含义                                                         |
| :------------------------------------------------------ | :----------------------------------------------------------- |
| `/@file/move/uuid/123456?driver=0&target=/path/path2/`  | 以`UUID`方式移动`UUID`为`12345`的文件到`/path2/`             |
| `/@file/move/uuid/123456?driver=0&target=/uuid/789456/` | 以`UUID`方式移动`UUID`为`12345`的文件到按`UUID`为`789456`的目录 下 |
| `/@file/move/path/path1/file1.mp4&target=/path/path2/`  | 移动`/path1/file1.mp4`路径的文件到`/path2/`                  |
| `/@file/move/path/path1/file1.mp4&target=/uuid/789456/` | 移动`/path1/file1.mp4`路径的文件到按`UUID`为`789456`的目录 下 |

### 创建文件(夹)

| 示例操作                                                    | 含义                                                      |
| :---------------------------------------------------------- | :-------------------------------------------------------- |
| `/@file/create/uuid/123456?driver=0&target=/path/path2/`    | 以`UUID`方式在`UUID`为`12345`的文件夹下创建`path2`文件夹  |
| `/@file/create/uuid/123456?driver=0&target=/file/file.mp4/` | 以`UUID`方式在`UUID`为`12345`的文件夹下创建`file.mp4`文件 |
| `/@file/create/path/path1/&target=/path/path2/`             | 在`/path1/`路径下创建`path2`文件夹                        |
| `/@file/create/path/path1/&target=/file/file.mp4/`          | 在`/path1/`路径下创建`file.mp4`文件                       |

### 删除文件(夹)

| 示例操作                             | 含义                                  |
| :----------------------------------- | :------------------------------------ |
| `/@file/remove/uuid/123456?driver=0` | 以`UUID`方式删除`UUID`为`12345`的文件 |
| `/@file/remove/path/path1/file1.mp4` | 删除`/path1/file1.mp4`路径的文件      |

### 配置文件(夹)

| 示例操作                                          | 含义                                                         |
| :------------------------------------------------ | :----------------------------------------------------------- |
| `/@file/config/uuid/123456?driver=0&config={...}` | 以`UUID`方式管理`UUID`为`12345`的文件的元信息，管理内容是`{...}` |
| `/@file/config/path/path1/file1.mp4?config={...}` | 管理`/path1/file1.mp4`路径的文件的元信息，管理内容是`{...}`  |

### 分享文件(夹)

| 示例操作                                          | 含义                                                         |
| :------------------------------------------------ | :----------------------------------------------------------- |
| `/@file/shared/uuid/123456?driver=0&config={...}`  | 以`UUID`方式创建/管理分享`UUID`为`12345`的文件，分享配置内容是`{...}` |
| `/@file/shared/path/path1/file1.mp4?config={...}` | 创建/管理分享`/path1/file1.mp4`路径的文件，分享配置内容是`{...}` |

> Body Parameters

```yaml
files: 二进制文件
config: "{...}"

```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|action|path|string| yes |必要：请求操作，列出文件(路径)、获取下载链接、复制文件、移动文件、创建文件(夹)、配置路径、分享管理|
|method|path|string| yes |必要：筛选方法，路径筛选(path)、UUID筛选(uuid)、不筛选(none)、创建文件夹（path）、创建文件（file）|
|source|path|string| yes |可选：筛选信息，指定路径(/../)、UUID(0EOJQGLO)、不筛选(<空>)|
|target|query|string| no |可选：移动、复制目标路径、创建文件的/文件(夹)名|
|driver|query|string| no |可选：当`method`参数为`uuid`时，需要指定驱动UUID|
|config|query|string| no |可选：配置元信息、分享文件、手动解密需要提交内容|
|body|body|object| no |none|
|» files|body|string| no |上传的文件|
|» config|body|string| no |上传的配置|

> Response Examples

> 200 Response

```json
{}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|none|Inline|

### Responses Data Schema

## GET 用户操作接口

GET /@user/{action}/{method}/{select}

## 接口用途
负责用户部分的管理

## 接口示例

### 列出用户

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@user/select/none/`       | 查找并列出所有的用户信息      |
| `/@user/select/uuid/12345/` | 查找并列出`ID12345`的用户信息 |

### 创建用户

| 示例操作                           | 含义                            |
| :--------------------------------- | :------------------------------ |
| `/@user/create/none/?config={...}` | 创建一个用户，并返回用户的 UUID |

### 删除用户

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@user/remove/uuid/12345/` | 删除`ID12345`挂载 |

### 修改用户

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@user/reload/uuid/12345/?config={...}` | 修改`ID12345`用户 |

### 登录用户

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@user/login/none/?config={...}` | 登录用户 |

### 登出用户

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@user/logout/none/` | 登出用户 |

> Body Parameters

```json
{}
```

### Params

|Name|Location|Type|Required|Description|
|---|---|---|---|---|
|action|path|string| yes |用户操作，创建、查找、删除、配置、登录、退出|
|method|path|string| yes |筛选方式，UUID（查找、删除、配置）、不筛选（查找、登录、退出）|
|select|path|string| yes |用户UUID，可以为空|
|config|query|string| no |配置信息，参考Body参数配置|
|body|body|object| no |none|

> Response Examples

> 200 Response

```json
{}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|none|Inline|

### Responses Data Schema

## GET 挂载管理接口

GET /@path/{action}/{method}/{driver}

## 接口用途
负责挂载部分的管理

## 接口示例

### 列出挂载

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@path/select/none/`       | 查找并列出所有的挂载信息      |
| `/@path/select/path/path1/` | 查找并列出`/path1/`的挂载信息 |
| `/@path/select/uuid/12345/` | 查找并列出`ID12345`的挂载信息 |

### 创建挂载

| 示例操作                                 | 含义                                            |
| :--------------------------------------- | :---------------------------------------------- |
| `/@path/create/path/path1/?config={...}` | 创建一个`/path1/`的挂载信息，此路径必须是唯一的 |

### 删除挂载

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@path/remove/path/path1/` | 删除 `/path1/`挂载 |
| `/@path/remove/uuid/12345/` | 删除`ID12345`挂载 |

### 载入挂载

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@path/reload/path/path1/` | 载入 `/path1/`挂载 |
| `/@path/reload/uuid/12345/` | 载入`ID12345`挂载 |

### 修改挂载

| 示例操作                    | 含义                          |
| :-------------------------- | :---------------------------- |
| `/@path/reload/path/path1/?config={...}` | 修改 `/path1/`挂载 |
| `/@path/reload/uuid/12345/?config={...}` | 修改`ID12345`挂载 |

> Body Parameters

```json
{
  "mount_path": "string",
  "mount_type": "string",
  "is_enabled": true,
  "drive_conf": "string",
  "cache_time": 0
}
```

### Params

|Name|Location|Type|Required|Title|Description|
|---|---|---|---|---|---|
|action|path|string| yes ||必要：驱动操作，创建挂载、删除挂载、载入驱动、查找驱动、配置驱动|
|method|path|string| yes ||必要：筛选方式，路径path、编号uuid、不筛选none（仅限查找时使用）|
|driver|path|string| yes ||必要：筛选内容，路径则使用路径定位、uuid则使用uuid定位|
|config|query|string| no ||可选：驱动配置，创建、修改时需要（GET时才需要，POST参考BODY部分）|
|body|body|object| no ||none|
|» mount_path|body|string| yes | 挂载路径|挂载驱动的唯一路径|
|» mount_type|body|string| yes | 驱动类型|驱动类型名称|
|» is_enabled|body|boolean| yes | 是否启用|是否启用挂载|
|» drive_conf|body|string| yes | 配置信息|挂载配置信息|
|» cache_time|body|integer| yes | 缓存时间|全局缓存时间|

> Response Examples

> 200 Response

```json
{}
```

### Responses

|HTTP Status Code |Meaning|Description|Data schema|
|---|---|---|---|
|200|[OK](https://tools.ietf.org/html/rfc7231#section-6.3.1)|none|Inline|

### Responses Data Schema

# Data Schema

<h2 id="tocS_ApiResponse">ApiResponse</h2>

<a id="schemaapiresponse"></a>
<a id="schema_ApiResponse"></a>
<a id="tocSapiresponse"></a>
<a id="tocsapiresponse"></a>

```json
{
  "code": 200,
  "message": "success",
  "data": null
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|code|integer|true|none||HTTP status code|
|message|string|true|none||Response message|
|data|any|false|none||Response payload (type varies by endpoint)|

<h2 id="tocS_ErrorResponse">ErrorResponse</h2>

<a id="schemaerrorresponse"></a>
<a id="schema_ErrorResponse"></a>
<a id="tocSerrorresponse"></a>
<a id="tocserrorresponse"></a>

```json
{
  "code": 400,
  "message": "Invalid request parameters",
  "data": null
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|code|integer|true|none||Error code|
|message|string|true|none||Error message|
|data|object¦null|false|none||none|

<h2 id="tocS_PageReq">PageReq</h2>

<a id="schemapagereq"></a>
<a id="schema_PageReq"></a>
<a id="tocSpagereq"></a>
<a id="tocspagereq"></a>

```json
{
  "page": 1,
  "per_page": 30
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|page|integer|false|none||Page number (1-indexed)|
|per_page|integer|false|none||Items per page|

<h2 id="tocS_Pagination">Pagination</h2>

<a id="schemapagination"></a>
<a id="schema_Pagination"></a>
<a id="tocSpagination"></a>
<a id="tocspagination"></a>

```json
{
  "total": 100,
  "page": 1,
  "per_page": 30
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|total|integer|false|none||Total number of items|
|page|integer|false|none||Current page number|
|per_page|integer|false|none||Items per page|

<h2 id="tocS_User">User</h2>

<a id="schemauser"></a>
<a id="schema_User"></a>
<a id="tocSuser"></a>
<a id="tocsuser"></a>

```json
{
  "id": 1,
  "username": "admin",
  "password": "",
  "base_path": "/",
  "role": 2,
  "disabled": false,
  "permission": 29183,
  "sso_id": "",
  "otp": false
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|id|integer|false|none||User unique ID|
|username|string|false|none||Username|
|password|string|false|none||Password (empty on read)|
|base_path|string|false|none||User's base path|
|role|integer|false|none||User role: 0=General, 1=Guest, 2=Admin|
|disabled|boolean|false|none||Whether the user is disabled|
|permission|integer(int32)|false|none||Permission (in bit flags):<br />- Bit 0: Can see hidden files<br />- Bit 1: Can access without password<br />- Bit 2: Can add offline download tasks<br />- Bit 3: Can mkdir and upload<br />- Bit 4: Can rename<br />- Bit 5: Can move<br />- Bit 6: Can copy<br />- Bit 7: Can remove<br />- Bit 8: WebDAV read<br />- Bit 9: WebDAV write<br />- Bit 10: FTP/SFTP login and read<br />- Bit 11: FTP/SFTP write<br />- Bit 12: Can read archives<br />- Bit 13: Can decompress archives<br />- Bit 14: Can share|
|sso_id|string|false|none||SSO platform ID|
|otp|boolean|false|none||Whether 2FA is enabled|

#### Enum

|Name|Value|
|---|---|
|role|0|
|role|1|
|role|2|

<h2 id="tocS_LoginRequest">LoginRequest</h2>

<a id="schemaloginrequest"></a>
<a id="schema_LoginRequest"></a>
<a id="tocSloginrequest"></a>
<a id="tocsloginrequest"></a>

```json
{
  "username": "admin",
  "password": "my password",
  "otp_code": "123456"
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|username|string|true|none||none|
|password|string(password)|true|none||none|
|otp_code|string|false|none||Two-factor authentication code (if 2FA is enabled)|

<h2 id="tocS_LoginResponse">LoginResponse</h2>

<a id="schemaloginresponse"></a>
<a id="schema_LoginResponse"></a>
<a id="tocSloginresponse"></a>
<a id="tocsloginresponse"></a>

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}

```

### Attribute

allOf

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|[ApiResponse](#schemaapiresponse)|false|none||none|

and

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|object|false|none||none|
|» data|object|false|none||none|
|»» token|string|false|none||JWT authentication token|

<h2 id="tocS_UserResponse">UserResponse</h2>

<a id="schemauserresponse"></a>
<a id="schema_UserResponse"></a>
<a id="tocSuserresponse"></a>
<a id="tocsuserresponse"></a>

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": 1,
    "username": "admin",
    "password": "",
    "base_path": "/",
    "role": 2,
    "disabled": false,
    "permission": 29183,
    "sso_id": "",
    "otp": false
  }
}

```

### Attribute

allOf

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|[ApiResponse](#schemaapiresponse)|false|none||none|

and

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|object|false|none||none|
|» data|[User](#schemauser)|false|none||none|

<h2 id="tocS_UsersListResponse">UsersListResponse</h2>

<a id="schemauserslistresponse"></a>
<a id="schema_UsersListResponse"></a>
<a id="tocSuserslistresponse"></a>
<a id="tocsuserslistresponse"></a>

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": 1,
        "username": "admin",
        "password": "",
        "base_path": "/",
        "role": 2,
        "disabled": false,
        "permission": 29183,
        "sso_id": "",
        "otp": false
      }
    ],
    "total": 5
  }
}

```

### Attribute

allOf

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|[ApiResponse](#schemaapiresponse)|false|none||none|

and

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|object|false|none||none|
|» data|object|false|none||none|
|»» content|[[User](#schemauser)]|false|none||none|
|»» total|integer|false|none||none|

<h2 id="tocS_FsObject">FsObject</h2>

<a id="schemafsobject"></a>
<a id="schema_FsObject"></a>
<a id="tocSfsobject"></a>
<a id="tocsfsobject"></a>

```json
{
  "id": "",
  "path": "D:\\files\\document.pdf",
  "name": "document.pdf",
  "size": 1024000,
  "is_dir": false,
  "modified": "2025-10-20T15:30:00+08:00",
  "created": "2025-10-20T10:00:00+08:00",
  "sign": "YBgnmykwCXUstXvNGtECaz_12gseXSL03cpqh5rTcGA=:0",
  "thumb": "",
  "type": 4,
  "hashinfo": "null",
  "hash_info": null,
  "mount_details": {
    "driver_name": "Local",
    "total_space": 1000000000000,
    "free_space": 500000000000
  }
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|id|string|false|none||Object ID (may be empty for local storage)|
|path|string|false|none||Full system path|
|name|string|false|none||File or directory name|
|size|integer(int64)|false|none||File size in bytes (0 for directories)|
|is_dir|boolean|false|none||Whether this is a directory|
|modified|string(date-time)|false|none||Last modified time|
|created|string(date-time)|false|none||Creation time|
|sign|string|false|none||Signature for download authentication|
|thumb|string|false|none||Thumbnail URL (if available)|
|type|integer|false|none||File type:<br />0=Unknown, 1=Folder, 2=Video, 3=Audio, 4=Text, 5=Image|
|hashinfo|string|false|none||Hash information (JSON string or "null")|
|hash_info|object¦null|false|none||Parsed hash information|
|» **additionalProperties**|string|false|none||none|
|mount_details|[StorageDetails](#schemastoragedetails)|false|none||none|

<h2 id="tocS_FsListRequest">FsListRequest</h2>

<a id="schemafslistrequest"></a>
<a id="schema_FsListRequest"></a>
<a id="tocSfslistrequest"></a>
<a id="tocsfslistrequest"></a>

```json
{
  "path": "/",
  "password": "",
  "refresh": false,
  "page": 1,
  "per_page": 30
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|path|string|false|none||Path to list|
|password|string|false|none||Password for protected paths|
|refresh|boolean|false|none||Force refresh cache|
|page|integer|false|none||none|
|per_page|integer|false|none||none|

<h2 id="tocS_FsListResponse">FsListResponse</h2>

<a id="schemafslistresponse"></a>
<a id="schema_FsListResponse"></a>
<a id="tocSfslistresponse"></a>
<a id="tocsfslistresponse"></a>

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": "",
        "path": "D:\\files\\document.pdf",
        "name": "document.pdf",
        "size": 1024000,
        "is_dir": false,
        "modified": "2025-10-20T15:30:00+08:00",
        "created": "2025-10-20T10:00:00+08:00",
        "sign": "YBgnmykwCXUstXvNGtECaz_12gseXSL03cpqh5rTcGA=:0",
        "thumb": "",
        "type": 4,
        "hashinfo": "null",
        "hash_info": null,
        "mount_details": {}
      }
    ],
    "total": 14,
    "readme": "",
    "header": "",
    "write": true,
    "provider": "Local"
  }
}

```

### Attribute

allOf

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|[ApiResponse](#schemaapiresponse)|false|none||none|

and

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|object|false|none||none|
|» data|object|false|none||none|
|»» content|[[FsObject](#schemafsobject)]|false|none||none|
|»» total|integer|false|none||Total number of items|
|»» readme|string|false|none||README content (if exists)|
|»» header|string|false|none||Header content|
|»» write|boolean|false|none||Whether current user has write permission|
|»» provider|string|false|none||Storage provider name|

<h2 id="tocS_FsGetRequest">FsGetRequest</h2>

<a id="schemafsgetrequest"></a>
<a id="schema_FsGetRequest"></a>
<a id="tocSfsgetrequest"></a>
<a id="tocsfsgetrequest"></a>

```json
{
  "path": "/document.pdf",
  "password": ""
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|path|string|true|none||File or directory path|
|password|string|false|none||Password for protected paths|

<h2 id="tocS_FsGetResponse">FsGetResponse</h2>

<a id="schemafsgetresponse"></a>
<a id="schema_FsGetResponse"></a>
<a id="tocSfsgetresponse"></a>
<a id="tocsfsgetresponse"></a>

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": "",
    "path": "D:\\files\\document.pdf",
    "name": "document.pdf",
    "size": 1024000,
    "is_dir": false,
    "modified": "2025-10-20T15:30:00+08:00",
    "created": "2025-10-20T10:00:00+08:00",
    "sign": "YBgnmykwCXUstXvNGtECaz_12gseXSL03cpqh5rTcGA=:0",
    "thumb": "",
    "type": 4,
    "hashinfo": "null",
    "hash_info": null,
    "mount_details": {
      "driver_name": "Local",
      "total_space": 1000000000000,
      "free_space": 500000000000
    }
  }
}

```

### Attribute

allOf

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|[ApiResponse](#schemaapiresponse)|false|none||none|

and

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|object|false|none||none|
|» data|[FsObject](#schemafsobject)|false|none||none|

<h2 id="tocS_FsMkdirRequest">FsMkdirRequest</h2>

<a id="schemafsmkdirrequest"></a>
<a id="schema_FsMkdirRequest"></a>
<a id="tocSfsmkdirrequest"></a>
<a id="tocsfsmkdirrequest"></a>

```json
{
  "path": "/newfolder"
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|path|string|true|none||Path where to create directory|

<h2 id="tocS_FsRenameRequest">FsRenameRequest</h2>

<a id="schemafsrenamerequest"></a>
<a id="schema_FsRenameRequest"></a>
<a id="tocSfsrenamerequest"></a>
<a id="tocsfsrenamerequest"></a>

```json
{
  "path": "/oldname.txt",
  "name": "newname.txt"
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|path|string|true|none||Current file/folder path|
|name|string|true|none||New name|

<h2 id="tocS_FsMoveCopyRequest">FsMoveCopyRequest</h2>

<a id="schemafsmovecopyrequest"></a>
<a id="schema_FsMoveCopyRequest"></a>
<a id="tocSfsmovecopyrequest"></a>
<a id="tocsfsmovecopyrequest"></a>

```json
{
  "src_dir": "/source",
  "dst_dir": "/destination",
  "names": [
    "file1.txt",
    "file2.pdf"
  ]
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|src_dir|string|true|none||Source directory path|
|dst_dir|string|true|none||Destination directory path|
|names|[string]|true|none||List of file/folder names to move/copy|

<h2 id="tocS_FsRemoveRequest">FsRemoveRequest</h2>

<a id="schemafsremoverequest"></a>
<a id="schema_FsRemoveRequest"></a>
<a id="tocSfsremoverequest"></a>
<a id="tocsfsremoverequest"></a>

```json
{
  "dir": "/folder",
  "names": [
    "file1.txt",
    "file2.pdf"
  ]
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|dir|string|true|none||Directory containing files to remove|
|names|[string]|true|none||List of file/folder names to remove|

<h2 id="tocS_StorageDetails">StorageDetails</h2>

<a id="schemastoragedetails"></a>
<a id="schema_StorageDetails"></a>
<a id="tocSstoragedetails"></a>
<a id="tocsstoragedetails"></a>

```json
{
  "driver_name": "Local",
  "total_space": 1000000000000,
  "free_space": 500000000000
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|driver_name|string|false|none||Storage driver name|
|total_space|integer(int64)|false|none||Total storage space in bytes|
|free_space|integer(int64)|false|none||Free storage space in bytes|

<h2 id="tocS_Storage">Storage</h2>

<a id="schemastorage"></a>
<a id="schema_Storage"></a>
<a id="tocSstorage"></a>
<a id="tocsstorage"></a>

```json
{
  "id": 1,
  "mount_path": "/local",
  "order": 0,
  "driver": "Local",
  "cache_expiration": 30,
  "status": "work",
  "addition": "{\"root_folder_path\":\"D:\\\\files\"}",
  "remark": "Local storage",
  "modified": "2019-08-24T14:15:22Z",
  "disabled": false,
  "disable_index": false,
  "enable_sign": false,
  "order_by": "name",
  "order_direction": "asc",
  "extract_folder": "front",
  "web_proxy": false,
  "webdav_policy": "native_proxy",
  "down_proxy_url": ""
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|id|integer|false|none||none|
|mount_path|string|false|none||Path where storage is mounted|
|order|integer|false|none||Display order|
|driver|string|false|none||Storage driver name|
|cache_expiration|integer|false|none||Cache expiration time in minutes|
|status|string|false|none||Storage status|
|addition|string|false|none||Driver-specific configuration (JSON string)|
|remark|string|false|none||Storage description/notes|
|modified|string(date-time)|false|none||none|
|disabled|boolean|false|none||none|
|disable_index|boolean|false|none||Disable search indexing for this storage|
|enable_sign|boolean|false|none||Enable signature verification for downloads|
|order_by|string|false|none||Default sort field|
|order_direction|string|false|none||none|
|extract_folder|string|false|none||Extract folder behavior|
|web_proxy|boolean|false|none||none|
|webdav_policy|string|false|none||none|
|down_proxy_url|string|false|none||Proxy URL for downloads|

#### Enum

|Name|Value|
|---|---|
|status|work|
|status|error|
|status|disabled|
|order_direction|asc|
|order_direction|desc|
|webdav_policy|native_proxy|
|webdav_policy|302_redirect|
|webdav_policy|use_proxy_url|

<h2 id="tocS_DriverInfo">DriverInfo</h2>

<a id="schemadriverinfo"></a>
<a id="schema_DriverInfo"></a>
<a id="tocSdriverinfo"></a>
<a id="tocsdriverinfo"></a>

```json
{
  "name": "Local",
  "config": {}
}

```

### Attribute

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|name|string|false|none||none|
|config|object|false|none||Driver configuration schema|

<h2 id="tocS_StorageResponse">StorageResponse</h2>

<a id="schemastorageresponse"></a>
<a id="schema_StorageResponse"></a>
<a id="tocSstorageresponse"></a>
<a id="tocsstorageresponse"></a>

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": 1,
    "mount_path": "/local",
    "order": 0,
    "driver": "Local",
    "cache_expiration": 30,
    "status": "work",
    "addition": "{\"root_folder_path\":\"D:\\\\files\"}",
    "remark": "Local storage",
    "modified": "2019-08-24T14:15:22Z",
    "disabled": false,
    "disable_index": false,
    "enable_sign": false,
    "order_by": "name",
    "order_direction": "asc",
    "extract_folder": "front",
    "web_proxy": false,
    "webdav_policy": "native_proxy",
    "down_proxy_url": ""
  }
}

```

### Attribute

allOf

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|[ApiResponse](#schemaapiresponse)|false|none||none|

and

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|object|false|none||none|
|» data|[Storage](#schemastorage)|false|none||none|

<h2 id="tocS_StoragesListResponse">StoragesListResponse</h2>

<a id="schemastorageslistresponse"></a>
<a id="schema_StoragesListResponse"></a>
<a id="tocSstorageslistresponse"></a>
<a id="tocsstorageslistresponse"></a>

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "content": [
      {
        "id": 1,
        "mount_path": "/local",
        "order": 0,
        "driver": "Local",
        "cache_expiration": 30,
        "status": "work",
        "addition": "{\"root_folder_path\":\"D:\\\\files\"}",
        "remark": "Local storage",
        "modified": "2019-08-24T14:15:22Z",
        "disabled": false,
        "disable_index": false,
        "enable_sign": false,
        "order_by": "name",
        "order_direction": "asc",
        "extract_folder": "front",
        "web_proxy": false,
        "webdav_policy": "native_proxy",
        "down_proxy_url": ""
      }
    ],
    "total": 0
  }
}

```

### Attribute

allOf

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|[ApiResponse](#schemaapiresponse)|false|none||none|

and

|Name|Type|Required|Restrictions|Title|Description|
|---|---|---|---|---|---|
|*anonymous*|object|false|none||none|
|» data|object|false|none||none|
|»» content|[[Storage](#schemastorage)]|false|none||none|
|»» total|integer|false|none||none|

