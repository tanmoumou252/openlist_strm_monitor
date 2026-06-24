> ## Documentation Index
> Fetch the complete documentation index at: https://developer.themoviedb.org/llms.txt
> Use this file to discover all available pages before exploring further.

# Authentication

Set up the welcome page for your API to help users make their first call.

# OpenAPI definition

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "tmdb-api",
    "version": "3"
  },
  "servers": [
    {
      "url": "https://api.themoviedb.org"
    }
  ],
  "components": {
    "securitySchemes": {
      "sec0": {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization",
        "x-bearer-format": "bearer"
      }
    }
  },
  "security": [
    {
      "sec0": []
    }
  ],
  "paths": {
    "/3/authentication": {
      "get": {
        "summary": "Validate Key",
        "description": "Test your API Key to see if it's valid.",
        "operationId": "authentication-validate-key",
        "responses": {
          "200": {
            "description": "200",
            "content": {
              "application/json": {
                "examples": {
                  "Result": {
                    "value": "{\"success\":true,\"status_code\":1,\"status_message\":\"Success.\"}"
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "success": {
                      "type": "boolean",
                      "example": true,
                      "default": true
                    },
                    "status_code": {
                      "type": "integer",
                      "example": 1,
                      "default": 0
                    },
                    "status_message": {
                      "type": "string",
                      "example": "Success."
                    }
                  }
                }
              }
            }
          },
          "401": {
            "description": "401",
            "content": {
              "application/json": {
                "examples": {
                  "Result": {
                    "value": "{\"status_code\":7,\"status_message\":\"Invalid API key: You must be granted a valid key.\",\"success\":false}"
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "status_code": {
                      "type": "integer",
                      "example": 7,
                      "default": 0
                    },
                    "status_message": {
                      "type": "string",
                      "example": "Invalid API key: You must be granted a valid key."
                    },
                    "success": {
                      "type": "boolean",
                      "example": false,
                      "default": true
                    }
                  }
                }
              }
            }
          }
        },
        "deprecated": false
      }
    }
  },
  "x-readme": {
    "headers": [],
    "explorer-enabled": true,
    "proxy-enabled": true
  },
  "x-readme-fauxas": true
}
```