# List directory contents

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/fs/list:
    post:
      summary: List directory contents
      deprecated: false
      description: Get list of files and directories at specified path with pagination
      operationId: postFslist
      tags:
        - File System
        - File System
      parameters: []
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/FsListRequest'
      responses:
        '200':
          description: Directory listing retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/FsListResponse'
          headers: {}
          x-apifox-name: 成功
        '401':
          description: Unauthorized
          content:
            application/json:
              schema: &ref_0
                $ref: '#/components/schemas/ErrorResponse'
          headers: {}
          x-apifox-name: 未认证
        '403':
          description: Forbidden - insufficient permissions or wrong password
          content:
            application/json:
              schema: *ref_0
          headers: {}
          x-apifox-name: 权限不足
        '404':
          description: Path not found
          content:
            application/json:
              schema: *ref_0
          headers: {}
          x-apifox-name: 未找到
      security:
        - BearerAuth: []
          x-apifox:
            schemeGroups:
              - id: AoqnbnJL_7kpr6cQpaKVD
                schemeIds:
                  - BearerAuth
            required: true
            use:
              id: AoqnbnJL_7kpr6cQpaKVD
            scopes:
              AoqnbnJL_7kpr6cQpaKVD:
                BearerAuth: []
      x-apifox-folder: File System
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/7048156/apis/api-364155732-run
components:
  schemas:
    FsListRequest:
      type: object
      properties:
        path:
          type: string
          description: Path to list
          default: /
          examples:
            - /
        password:
          type: string
          description: Password for protected paths
          examples:
            - ''
        refresh:
          type: boolean
          description: Force refresh cache
          default: false
        page:
          type: integer
          minimum: 1
          default: 1
        per_page:
          type: integer
          minimum: 1
          maximum: 100
          default: 30
      x-apifox-orders:
        - path
        - password
        - refresh
        - page
        - per_page
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    FsListResponse:
      allOf:
        - $ref: '#/components/schemas/ApiResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                content:
                  type: array
                  items:
                    $ref: '#/components/schemas/FsObject'
                total:
                  type: integer
                  description: Total number of items
                  examples:
                    - 14
                readme:
                  type: string
                  description: README content (if exists)
                  examples:
                    - ''
                header:
                  type: string
                  description: Header content
                  examples:
                    - ''
                write:
                  type: boolean
                  description: Whether current user has write permission
                  examples:
                    - true
                provider:
                  type: string
                  description: Storage provider name
                  examples:
                    - Local
              x-apifox-orders:
                - content
                - total
                - readme
                - header
                - write
                - provider
              x-apifox-ignore-properties: []
          x-apifox-orders:
            - data
          x-apifox-ignore-properties: []
      x-apifox-folder: ''
    FsObject:
      type: object
      properties:
        id:
          type: string
          description: Object ID (may be empty for local storage)
          examples:
            - ''
        path:
          type: string
          description: Full system path
          examples:
            - D:\files\document.pdf
        name:
          type: string
          description: File or directory name
          examples:
            - document.pdf
        size:
          type: integer
          format: int64
          description: File size in bytes (0 for directories)
          examples:
            - 1024000
        is_dir:
          type: boolean
          description: Whether this is a directory
          examples:
            - false
        modified:
          type: string
          format: date-time
          description: Last modified time
          examples:
            - '2025-10-20T15:30:00+08:00'
        created:
          type: string
          format: date-time
          description: Creation time
          examples:
            - '2025-10-20T10:00:00+08:00'
        sign:
          type: string
          description: Signature for download authentication
          examples:
            - YBgnmykwCXUstXvNGtECaz_12gseXSL03cpqh5rTcGA=:0
        thumb:
          type: string
          description: Thumbnail URL (if available)
          examples:
            - ''
        type:
          type: integer
          description: |
            File type:
            0=Unknown, 1=Folder, 2=Video, 3=Audio, 4=Text, 5=Image
          examples:
            - 4
        hashinfo:
          type: string
          description: Hash information (JSON string or "null")
          examples:
            - 'null'
        hash_info:
          type: object
          additionalProperties:
            type: string
          description: Parsed hash information
          x-apifox-orders: []
          examples:
            - null
          properties: {}
          x-apifox-ignore-properties: []
          nullable: true
        mount_details:
          $ref: '#/components/schemas/StorageDetails'
      x-apifox-orders:
        - id
        - path
        - name
        - size
        - is_dir
        - modified
        - created
        - sign
        - thumb
        - type
        - hashinfo
        - hash_info
        - mount_details
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    StorageDetails:
      type: object
      properties:
        driver_name:
          type: string
          description: Storage driver name
          examples:
            - Local
        total_space:
          type: integer
          format: int64
          description: Total storage space in bytes
          examples:
            - 1000000000000
        free_space:
          type: integer
          format: int64
          description: Free storage space in bytes
          examples:
            - 500000000000
      x-apifox-orders:
        - driver_name
        - total_space
        - free_space
      x-apifox-ignore-properties: []
      nullable: true
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
