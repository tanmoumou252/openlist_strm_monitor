# List all storages (Admin)

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/admin/storage/list:
    get:
      summary: List all storages (Admin)
      deprecated: false
      description: Get list of all configured storage mounts
      operationId: getAdminstoragelist
      tags:
        - Admin
        - Admin
        - Storage
      parameters:
        - name: page
          in: query
          description: ''
          required: false
          schema:
            type: integer
            default: 1
        - name: per_page
          in: query
          description: ''
          required: false
          schema:
            type: integer
            default: 30
      responses:
        '200':
          description: Storages list retrieved
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/StoragesListResponse'
          headers: {}
          x-apifox-name: 成功
        '403':
          description: Forbidden - Admin required
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
              - id: bKCPNoL1g9MahzBvuKxVe
                schemeIds:
                  - BearerAuth
            required: true
            use:
              id: bKCPNoL1g9MahzBvuKxVe
            scopes:
              bKCPNoL1g9MahzBvuKxVe:
                BearerAuth: []
      x-apifox-folder: Admin
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/7048156/apis/api-364155706-run
components:
  schemas:
    StoragesListResponse:
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
                    $ref: '#/components/schemas/Storage'
                total:
                  type: integer
              x-apifox-orders:
                - content
                - total
              x-apifox-ignore-properties: []
          x-apifox-orders:
            - data
          x-apifox-ignore-properties: []
      x-apifox-folder: ''
    Storage:
      type: object
      properties:
        id:
          type: integer
          examples:
            - 1
        mount_path:
          type: string
          description: Path where storage is mounted
          examples:
            - /local
        order:
          type: integer
          description: Display order
          examples:
            - 0
        driver:
          type: string
          description: Storage driver name
          examples:
            - Local
        cache_expiration:
          type: integer
          description: Cache expiration time in minutes
          examples:
            - 30
        status:
          type: string
          description: Storage status
          enum:
            - work
            - error
            - disabled
          examples:
            - work
        addition:
          type: string
          description: Driver-specific configuration (JSON string)
          examples:
            - '{"root_folder_path":"D:\\files"}'
        remark:
          type: string
          description: Storage description/notes
          examples:
            - Local storage
        modified:
          type: string
          format: date-time
        disabled:
          type: boolean
          examples:
            - false
        disable_index:
          type: boolean
          description: Disable search indexing for this storage
          examples:
            - false
        enable_sign:
          type: boolean
          description: Enable signature verification for downloads
          examples:
            - false
        order_by:
          type: string
          description: Default sort field
          examples:
            - name
        order_direction:
          type: string
          enum:
            - asc
            - desc
          examples:
            - asc
        extract_folder:
          type: string
          description: Extract folder behavior
          examples:
            - front
        web_proxy:
          type: boolean
          examples:
            - false
        webdav_policy:
          type: string
          enum:
            - native_proxy
            - 302_redirect
            - use_proxy_url
          examples:
            - native_proxy
        down_proxy_url:
          type: string
          description: Proxy URL for downloads
          examples:
            - ''
      x-apifox-orders:
        - id
        - mount_path
        - order
        - driver
        - cache_expiration
        - status
        - addition
        - remark
        - modified
        - disabled
        - disable_index
        - enable_sign
        - order_by
        - order_direction
        - extract_folder
        - web_proxy
        - webdav_policy
        - down_proxy_url
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

# Get storage by ID (Admin)

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/admin/storage/get:
    get:
      summary: Get storage by ID (Admin)
      deprecated: false
      description: Retrieve specific storage configuration
      operationId: getAdminstorageget
      tags:
        - Admin
        - Admin
        - Storage
      parameters:
        - name: id
          in: query
          description: ''
          required: true
          schema:
            type: integer
      responses:
        '200':
          description: Storage info retrieved
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/StorageResponse'
          headers: {}
          x-apifox-name: 成功
        '404':
          description: Storage not found
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
          headers: {}
          x-apifox-name: 未找到
      security:
        - BearerAuth: []
          x-apifox:
            schemeGroups:
              - id: nIbYYycTCQtGRhSI_Ag8Z
                schemeIds:
                  - BearerAuth
            required: true
            use:
              id: nIbYYycTCQtGRhSI_Ag8Z
            scopes:
              nIbYYycTCQtGRhSI_Ag8Z:
                BearerAuth: []
      x-apifox-folder: Admin
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/7048156/apis/api-364155707-run
components:
  schemas:
    StorageResponse:
      allOf:
        - $ref: '#/components/schemas/ApiResponse'
        - type: object
          properties:
            data:
              $ref: '#/components/schemas/Storage'
          x-apifox-orders:
            - data
          x-apifox-ignore-properties: []
      x-apifox-folder: ''
    Storage:
      type: object
      properties:
        id:
          type: integer
          examples:
            - 1
        mount_path:
          type: string
          description: Path where storage is mounted
          examples:
            - /local
        order:
          type: integer
          description: Display order
          examples:
            - 0
        driver:
          type: string
          description: Storage driver name
          examples:
            - Local
        cache_expiration:
          type: integer
          description: Cache expiration time in minutes
          examples:
            - 30
        status:
          type: string
          description: Storage status
          enum:
            - work
            - error
            - disabled
          examples:
            - work
        addition:
          type: string
          description: Driver-specific configuration (JSON string)
          examples:
            - '{"root_folder_path":"D:\\files"}'
        remark:
          type: string
          description: Storage description/notes
          examples:
            - Local storage
        modified:
          type: string
          format: date-time
        disabled:
          type: boolean
          examples:
            - false
        disable_index:
          type: boolean
          description: Disable search indexing for this storage
          examples:
            - false
        enable_sign:
          type: boolean
          description: Enable signature verification for downloads
          examples:
            - false
        order_by:
          type: string
          description: Default sort field
          examples:
            - name
        order_direction:
          type: string
          enum:
            - asc
            - desc
          examples:
            - asc
        extract_folder:
          type: string
          description: Extract folder behavior
          examples:
            - front
        web_proxy:
          type: boolean
          examples:
            - false
        webdav_policy:
          type: string
          enum:
            - native_proxy
            - 302_redirect
            - use_proxy_url
          examples:
            - native_proxy
        down_proxy_url:
          type: string
          description: Proxy URL for downloads
          examples:
            - ''
      x-apifox-orders:
        - id
        - mount_path
        - order
        - driver
        - cache_expiration
        - status
        - addition
        - remark
        - modified
        - disabled
        - disable_index
        - enable_sign
        - order_by
        - order_direction
        - extract_folder
        - web_proxy
        - webdav_policy
        - down_proxy_url
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
