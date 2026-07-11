> ## Documentation Index
> Fetch the complete documentation index at: https://developer.themoviedb.org/llms.txt
> Use this file to discover all available pages before exploring further.

# Alternative Titles

Get the alternative titles for a movie.

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
    "/3/movie/{movie_id}/alternative_titles": {
      "get": {
        "summary": "Alternative Titles",
        "description": "Get the alternative titles for a movie.",
        "operationId": "movie-alternative-titles",
        "parameters": [
          {
            "name": "movie_id",
            "in": "path",
            "schema": {
              "type": "integer",
              "format": "int32"
            },
            "required": true
          },
          {
            "name": "country",
            "in": "query",
            "description": "specify a ISO-3166-1 value to filter the results",
            "schema": {
              "type": "string"
            }
          }
        ],
        "responses": {
          "200": {
            "description": "200",
            "content": {
              "application/json": {
                "examples": {
                  "Result": {
                    "value": "{\"id\":550,\"titles\":[{\"iso_3166_1\":\"RS\",\"title\":\"Borilački klub\",\"type\":\"\"},{\"iso_3166_1\":\"IL\",\"title\":\"Mo'adon Krav\",\"type\":\"romanization\"},{\"iso_3166_1\":\"RU\",\"title\":\"Boytsovskiy klub\",\"type\":\"\"},{\"iso_3166_1\":\"BG\",\"title\":\"Boen klub\",\"type\":\"\"},{\"iso_3166_1\":\"GR\",\"title\":\"Kláb máchis\",\"type\":\"\"},{\"iso_3166_1\":\"UA\",\"title\":\"Biytsivsʹkyy klub\",\"type\":\"\"},{\"iso_3166_1\":\"TR\",\"title\":\"Dövüş Klubü\",\"type\":\"\"},{\"iso_3166_1\":\"MX\",\"title\":\"El Club de la Pelea\",\"type\":\"Hispanoamérica\"},{\"iso_3166_1\":\"KR\",\"title\":\"파이트 클럽\",\"type\":\"\"},{\"iso_3166_1\":\"LV\",\"title\":\"Cīņas klubs\",\"type\":\"\"},{\"iso_3166_1\":\"IR\",\"title\":\"باشگاه مشت زنی\",\"type\":\"\"},{\"iso_3166_1\":\"PL\",\"title\":\"Podziemny krąg\",\"type\":\"\"}]}"
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "id": {
                      "type": "integer",
                      "example": 550,
                      "default": 0
                    },
                    "titles": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "iso_3166_1": {
                            "type": "string",
                            "example": "RS"
                          },
                          "title": {
                            "type": "string",
                            "example": "Borilački klub"
                          },
                          "type": {
                            "type": "string",
                            "example": ""
                          }
                        }
                      }
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