> ## Documentation Index
> Fetch the complete documentation index at: https://developer.themoviedb.org/llms.txt
> Use this file to discover all available pages before exploring further.

# TV

Search for TV shows by their original, translated and also known as names.

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
    "/3/search/tv": {
      "get": {
        "summary": "TV",
        "description": "Search for TV shows by their original, translated and also known as names.",
        "operationId": "search-tv",
        "parameters": [
          {
            "name": "query",
            "in": "query",
            "required": true,
            "schema": {
              "type": "string"
            }
          },
          {
            "name": "first_air_date_year",
            "in": "query",
            "description": "Search only the first air date. Valid values are: 1000..9999",
            "schema": {
              "type": "integer",
              "format": "int32"
            }
          },
          {
            "name": "include_adult",
            "in": "query",
            "schema": {
              "type": "boolean",
              "default": false
            }
          },
          {
            "name": "language",
            "in": "query",
            "schema": {
              "type": "string",
              "default": "en-US"
            }
          },
          {
            "name": "page",
            "in": "query",
            "schema": {
              "type": "integer",
              "format": "int32",
              "default": 1
            }
          },
          {
            "name": "year",
            "in": "query",
            "description": "Search the first air date and all episode air dates. Valid values are: 1000..9999",
            "schema": {
              "type": "integer",
              "format": "int32"
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
                    "value": "{\"page\":1,\"results\":[{\"adult\":false,\"backdrop_path\":\"/bsNm9z2TJfe0WO3RedPGWQ8mG1X.jpg\",\"genre_ids\":[18,80],\"id\":1396,\"origin_country\":[\"US\"],\"original_language\":\"en\",\"original_name\":\"Breaking Bad\",\"overview\":\"When Walter White, a New Mexico chemistry teacher, is diagnosed with Stage III cancer and given a prognosis of only two years left to live. He becomes filled with a sense of fearlessness and an unrelenting desire to secure his family's financial future at any cost as he enters the dangerous world of drugs and crime.\",\"popularity\":298.884,\"poster_path\":\"/ggFHVNu6YYI5L9pCfOacjizRGt.jpg\",\"first_air_date\":\"2008-01-20\",\"name\":\"Breaking Bad\",\"vote_average\":8.879,\"vote_count\":11536}],\"total_pages\":1,\"total_results\":1}"
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "page": {
                      "type": "integer",
                      "example": 1,
                      "default": 0
                    },
                    "results": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "adult": {
                            "type": "boolean",
                            "example": false,
                            "default": true
                          },
                          "backdrop_path": {
                            "type": "string",
                            "example": "/bsNm9z2TJfe0WO3RedPGWQ8mG1X.jpg"
                          },
                          "genre_ids": {
                            "type": "array",
                            "items": {
                              "type": "integer",
                              "example": 18,
                              "default": 0
                            }
                          },
                          "id": {
                            "type": "integer",
                            "example": 1396,
                            "default": 0
                          },
                          "origin_country": {
                            "type": "array",
                            "items": {
                              "type": "string",
                              "example": "US"
                            }
                          },
                          "original_language": {
                            "type": "string",
                            "example": "en"
                          },
                          "original_name": {
                            "type": "string",
                            "example": "Breaking Bad"
                          },
                          "overview": {
                            "type": "string",
                            "example": "When Walter White, a New Mexico chemistry teacher, is diagnosed with Stage III cancer and given a prognosis of only two years left to live. He becomes filled with a sense of fearlessness and an unrelenting desire to secure his family's financial future at any cost as he enters the dangerous world of drugs and crime."
                          },
                          "popularity": {
                            "type": "number",
                            "example": 298.884,
                            "default": 0
                          },
                          "poster_path": {
                            "type": "string",
                            "example": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg"
                          },
                          "first_air_date": {
                            "type": "string",
                            "example": "2008-01-20"
                          },
                          "name": {
                            "type": "string",
                            "example": "Breaking Bad"
                          },
                          "vote_average": {
                            "type": "number",
                            "example": 8.879,
                            "default": 0
                          },
                          "vote_count": {
                            "type": "integer",
                            "example": 11536,
                            "default": 0
                          }
                        }
                      }
                    },
                    "total_pages": {
                      "type": "integer",
                      "example": 1,
                      "default": 0
                    },
                    "total_results": {
                      "type": "integer",
                      "example": 1,
                      "default": 0
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