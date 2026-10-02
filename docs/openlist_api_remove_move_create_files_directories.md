# Create directory

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/fs/mkdir:
    post:
      summary: Create directory
      deprecated: false
      description: Create a new directory at specified path
      operationId: postFsmkdir
      tags:
        - File System
        - File System
      parameters: []
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/FsMkdirRequest'
      responses:
        '200':
          description: Directory created successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ApiResponse'
          headers: {}
          x-apifox-name: 成功
        '400':
          description: Invalid path or directory already exists
          content:
            application/json:
              schema: &ref_0
                $ref: '#/components/schemas/ErrorResponse'
          headers: {}
          x-apifox-name: 请求有误
        '403':
          description: Insufficient permissions
          content:
            application/json:
              schema: *ref_0
          headers: {}
          x-apifox-name: 权限不足
      security:
        - BearerAuth: []
          x-apifox:
            schemeGroups:
              - id: oBuYLEveOXsw4o6nndr65
                schemeIds:
                  - BearerAuth
            required: true
            use:
              id: oBuYLEveOXsw4o6nndr65
            scopes:
              oBuYLEveOXsw4o6nndr65:
                BearerAuth: []
      x-apifox-folder: File System
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/7048156/apis/api-364155737-run
components:
  schemas:
    FsMkdirRequest:
      type: object
      required:
        - path
      properties:
        path:
          type: string
          description: Path where to create directory
          examples:
            - /newfolder
      x-apifox-orders:
        - path
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


# Move files or directories

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/fs/move:
    post:
      summary: Move files or directories
      deprecated: false
      description: Move one or more files/folders to another location
      operationId: postFsmove
      tags:
        - File System
        - File System
      parameters: []
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/FsMoveCopyRequest'
      responses:
        '200':
          description: Moved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ApiResponse'
          headers: {}
          x-apifox-name: 成功
        '403':
          description: Insufficient permissions
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
          headers: {}
          x-apifox-name: 权限不足
      security:
        - BearerAuth: []
          x-apifox:
            schemeGroups:
              - id: RTpY2XCbRzYfjFJiNiYif
                schemeIds:
                  - BearerAuth
            required: true
            use:
              id: RTpY2XCbRzYfjFJiNiYif
            scopes:
              RTpY2XCbRzYfjFJiNiYif:
                BearerAuth: []
      x-apifox-folder: File System
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/7048156/apis/api-364155741-run
components:
  schemas:
    FsMoveCopyRequest:
      type: object
      required:
        - src_dir
        - dst_dir
        - names
      properties:
        src_dir:
          type: string
          description: Source directory path
          examples:
            - /source
        dst_dir:
          type: string
          description: Destination directory path
          examples:
            - /destination
        names:
          type: array
          items:
            type: string
          description: List of file/folder names to move/copy
          examples:
            - - file1.txt
              - file2.pdf
      x-apifox-orders:
        - src_dir
        - dst_dir
        - names
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

# Remove files or directories

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/fs/remove:
    post:
      summary: Remove files or directories
      deprecated: false
      description: Delete one or more files or folders
      operationId: postFsremove
      tags:
        - File System
        - File System
      parameters: []
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/FsRemoveRequest'
      responses:
        '200':
          description: Removed successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ApiResponse'
          headers: {}
          x-apifox-name: 成功
        '403':
          description: Insufficient permissions
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
          headers: {}
          x-apifox-name: 权限不足
      security:
        - BearerAuth: []
          x-apifox:
            schemeGroups:
              - id: MDjbubItUVHl3prVXrU3W
                schemeIds:
                  - BearerAuth
            required: true
            use:
              id: MDjbubItUVHl3prVXrU3W
            scopes:
              MDjbubItUVHl3prVXrU3W:
                BearerAuth: []
      x-apifox-folder: File System
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/7048156/apis/api-364155744-run
components:
  schemas:
    FsRemoveRequest:
      type: object
      required:
        - dir
        - names
      properties:
        dir:
          type: string
          description: Directory containing files to remove
          examples:
            - /folder
        names:
          type: array
          items:
            type: string
          description: List of file/folder names to remove
          examples:
            - - file1.txt
              - file2.pdf
      x-apifox-orders:
        - dir
        - names
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
