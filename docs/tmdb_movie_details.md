> ## Documentation Index
> Fetch the complete documentation index at: https://developer.themoviedb.org/llms.txt
> Use this file to discover all available pages before exploring further.

# Details

Get the top level details of a movie by ID.

## Append To Response

This method supports using `append_to_response`. Read more about this [here](https://developer.themoviedb.org/docs/append-to-response).

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
    "/3/movie/{movie_id}": {
      "get": {
        "summary": "Details",
        "description": "Get the top level details of a movie by ID.",
        "operationId": "movie-details",
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
            "name": "append_to_response",
            "in": "query",
            "description": "comma separated list of endpoints within this namespace, 20 items max",
            "schema": {
              "type": "string"
            }
          },
          {
            "name": "language",
            "in": "query",
            "schema": {
              "type": "string",
              "default": "en-US"
            }
          }
        ],
        "responses": {
          "200": {
            "description": "200",
            "content": {
              "application/json": {
                "examples": {
                  "OK": {
                    "summary": "OK",
                    "value": {
                      "adult": false,
                      "backdrop_path": "/2w4xG178RpB4MDAIfTkqAuSJzec.jpg",
                      "belongs_to_collection": {
                        "id": 10,
                        "name": "Star Wars Collection",
                        "poster_path": "/pWVLFh4OuejTpUaDQbB1C4zoS2p.jpg",
                        "backdrop_path": "/iY2ujEY2m68OTTlPFTiHub9joHS.jpg"
                      },
                      "budget": 11000000,
                      "genres": [
                        {
                          "id": 12,
                          "name": "Adventure"
                        },
                        {
                          "id": 28,
                          "name": "Action"
                        },
                        {
                          "id": 878,
                          "name": "Science Fiction"
                        }
                      ],
                      "homepage": "http://www.starwars.com/films/star-wars-episode-iv-a-new-hope",
                      "id": 11,
                      "imdb_id": "tt0076759",
                      "origin_country": [
                        "US"
                      ],
                      "original_language": "en",
                      "original_title": "Star Wars",
                      "overview": "Princess Leia is captured and held hostage by the evil Imperial forces in their effort to take over the galactic Empire. Venturesome Luke Skywalker and dashing captain Han Solo team together with the loveable robot duo R2-D2 and C-3PO to rescue the beautiful princess and restore peace and justice in the Empire.",
                      "popularity": 20.6912,
                      "poster_path": "/6FfCtAuVAW8XJjZ7eWeLibRLWTw.jpg",
                      "production_companies": [
                        {
                          "id": 1,
                          "logo_path": "/tlVSws0RvvtPBwViUyOFAO0vcQS.png",
                          "name": "Lucasfilm Ltd.",
                          "origin_country": "US"
                        },
                        {
                          "id": 25,
                          "logo_path": "/qZCc1lty5FzX30aOCVRBLzaVmcp.png",
                          "name": "20th Century Fox",
                          "origin_country": "US"
                        }
                      ],
                      "production_countries": [
                        {
                          "iso_3166_1": "US",
                          "name": "United States of America"
                        }
                      ],
                      "release_date": "1977-05-25",
                      "revenue": 775398007,
                      "runtime": 121,
                      "spoken_languages": [
                        {
                          "english_name": "English",
                          "iso_639_1": "en",
                          "name": "English"
                        }
                      ],
                      "status": "Released",
                      "tagline": "A long time ago in a galaxy far, far away...",
                      "title": "Star Wars",
                      "video": false,
                      "vote_average": 8.2,
                      "vote_count": 22061
                    }
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "adult": {
                      "type": "boolean",
                      "example": false,
                      "default": "false"
                    },
                    "backdrop_path": {
                      "type": "string",
                      "example": "/2w4xG178RpB4MDAIfTkqAuSJzec.jpg"
                    },
                    "belongs_to_collection": {
                      "type": "object",
                      "properties": {
                        "id": {
                          "type": "integer",
                          "example": 10,
                          "default": 0
                        },
                        "name": {
                          "type": "string",
                          "example": "Star Wars Collection"
                        },
                        "poster_path": {
                          "type": "string",
                          "example": "/pWVLFh4OuejTpUaDQbB1C4zoS2p.jpg"
                        },
                        "backdrop_path": {
                          "type": "string",
                          "example": "/iY2ujEY2m68OTTlPFTiHub9joHS.jpg"
                        }
                      }
                    },
                    "budget": {
                      "type": "integer",
                      "example": 11000000,
                      "default": 0
                    },
                    "genres": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "id": {
                            "type": "integer",
                            "example": 12,
                            "default": 0
                          },
                          "name": {
                            "type": "string",
                            "example": "Adventure"
                          }
                        }
                      }
                    },
                    "homepage": {
                      "type": "string",
                      "example": "http://www.starwars.com/films/star-wars-episode-iv-a-new-hope"
                    },
                    "id": {
                      "type": "integer",
                      "example": 11,
                      "default": 0
                    },
                    "imdb_id": {
                      "type": "string",
                      "example": "tt0076759"
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
                    "original_title": {
                      "type": "string",
                      "example": "Star Wars"
                    },
                    "overview": {
                      "type": "string",
                      "example": "Princess Leia is captured and held hostage by the evil Imperial forces in their effort to take over the galactic Empire. Venturesome Luke Skywalker and dashing captain Han Solo team together with the loveable robot duo R2-D2 and C-3PO to rescue the beautiful princess and restore peace and justice in the Empire."
                    },
                    "popularity": {
                      "type": "number",
                      "example": 20.6912,
                      "default": 0
                    },
                    "poster_path": {
                      "type": "string",
                      "example": "/6FfCtAuVAW8XJjZ7eWeLibRLWTw.jpg"
                    },
                    "production_companies": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "id": {
                            "type": "integer",
                            "example": 1,
                            "default": 0
                          },
                          "logo_path": {
                            "type": "string",
                            "example": "/tlVSws0RvvtPBwViUyOFAO0vcQS.png"
                          },
                          "name": {
                            "type": "string",
                            "example": "Lucasfilm Ltd."
                          },
                          "origin_country": {
                            "type": "string",
                            "example": "US"
                          }
                        }
                      }
                    },
                    "production_countries": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "iso_3166_1": {
                            "type": "string",
                            "example": "US"
                          },
                          "name": {
                            "type": "string",
                            "example": "United States of America"
                          }
                        }
                      }
                    },
                    "release_date": {
                      "type": "string",
                      "example": "1977-05-25"
                    },
                    "revenue": {
                      "type": "integer",
                      "example": 775398007,
                      "default": 0
                    },
                    "runtime": {
                      "type": "integer",
                      "example": 121,
                      "default": 0
                    },
                    "spoken_languages": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "english_name": {
                            "type": "string",
                            "example": "English"
                          },
                          "iso_639_1": {
                            "type": "string",
                            "example": "en"
                          },
                          "name": {
                            "type": "string",
                            "example": "English"
                          }
                        }
                      }
                    },
                    "status": {
                      "type": "string",
                      "example": "Released"
                    },
                    "tagline": {
                      "type": "string",
                      "example": "A long time ago in a galaxy far, far away..."
                    },
                    "title": {
                      "type": "string",
                      "example": "Star Wars"
                    },
                    "video": {
                      "type": "boolean",
                      "example": false,
                      "default": "false"
                    },
                    "vote_average": {
                      "type": "number",
                      "example": 8.2,
                      "default": 0
                    },
                    "vote_count": {
                      "type": "integer",
                      "example": 22061,
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