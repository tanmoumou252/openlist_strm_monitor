# User login

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/auth/login:
    post:
      summary: User login
      deprecated: false
      description: Authenticate user with username and password, returns JWT token
      operationId: postAuthlogin
      tags:
        - Authentication
        - Authentication
      parameters: []
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/LoginRequest'
      responses:
        '200':
          description: Login successful
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/LoginResponse'
          headers: {}
          x-apifox-name: 成功
        '400':
          description: Invalid request or wrong credentials
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
          headers: {}
          x-apifox-name: 请求有误
      security: []
      x-apifox-folder: Authentication
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/7048156/apis/api-364155678-run
components:
  schemas:
    LoginRequest:
      type: object
      required:
        - username
        - password
      properties:
        username:
          type: string
          examples:
            - admin
        password:
          type: string
          format: password
          examples:
            - my password
        otp_code:
          type: string
          description: Two-factor authentication code (if 2FA is enabled)
          examples:
            - '123456'
      x-apifox-orders:
        - username
        - password
        - otp_code
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    LoginResponse:
      allOf:
        - $ref: '#/components/schemas/ApiResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                token:
                  type: string
                  description: JWT authentication token
                  examples:
                    - eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
              x-apifox-orders:
                - token
              x-apifox-ignore-properties: []
          x-apifox-orders:
            - data
          x-apifox-ignore-properties: []
      x-apifox-folder: ''
    ApiResponse:
      type: object
      required:
        - code
        - message
      properties:
        code:
          type: integer
          description: HTTP status code
          examples:
            - 200
        message:
          type: string
          description: Response message
          examples:
            - success
        data:
          type: string
      x-apifox-orders:
        - code
        - message
        - data
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    ErrorResponse:
      type: object
      required:
        - code
        - message
      properties:
        code:
          type: integer
          description: Error code
          examples:
            - 400
        message:
          type: string
          description: Error message
          examples:
            - Invalid request parameters
        data:
          type: object
          x-apifox-orders: []
          examples:
            - null
          properties: {}
          x-apifox-ignore-properties: []
          nullable: true
      x-apifox-orders:
        - code
        - message
        - data
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
  securitySchemes:
    BearerAuth:
      type: jwt
      scheme: bearer
      bearerFormat: JWT
      description: >
        JWT token obtained from login endpoint.

        Include the token directly (without "Bearer" prefix) in Authorization
        header.
      x-apifox:
        addTokenTo: header
        headerPrefix: ''
servers: []
security: []

```
