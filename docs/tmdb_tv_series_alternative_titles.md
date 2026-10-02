> ## Documentation Index
> Fetch the complete documentation index at: https://developer.themoviedb.org/llms.txt
> Use this file to discover all available pages before exploring further.

# Alternative Titles

Get the alternative titles that have been added to a TV show.

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
    "/3/tv/{series_id}/alternative_titles": {
      "get": {
        "summary": "Alternative Titles",
        "description": "Get the alternative titles that have been added to a TV show.",
        "operationId": "tv-series-alternative-titles",
        "parameters": [
          {
            "name": "series_id",
            "in": "path",
            "schema": {
              "type": "integer",
              "format": "int32"
            },
            "required": true
          }
        ],
        "responses": {
          "200": {
            "description": "200",
            "content": {
              "application/json": {
                "examples": {
                  "Result": {
                    "value": "{\"id\":1399,\"results\":[{\"iso_3166_1\":\"AL\",\"title\":\"Froni i shpatave\",\"type\":\"\"},{\"iso_3166_1\":\"AR\",\"title\":\"El Juego de Tronos\",\"type\":\"\"},{\"iso_3166_1\":\"BR\",\"title\":\"A Guerra dos Tronos\",\"type\":\"\"},{\"iso_3166_1\":\"CN\",\"title\":\"权利的游戏\",\"type\":\"\"},{\"iso_3166_1\":\"CN\",\"title\":\"權力的遊戲\",\"type\":\"\"},{\"iso_3166_1\":\"DE\",\"title\":\"Game of Thrones: Das Lied von Eis und Feuer\",\"type\":\"\"},{\"iso_3166_1\":\"DE\",\"title\":\"Paihnidi tou stemmatos\",\"type\":\"\"},{\"iso_3166_1\":\"FR\",\"title\":\"Le Throne de fer\",\"type\":\"\"},{\"iso_3166_1\":\"FR\",\"title\":\"Game of Thrones - Le trône de fer\",\"type\":\"\"},{\"iso_3166_1\":\"GE\",\"title\":\"სატახტოთა თამაში\",\"type\":\"\"},{\"iso_3166_1\":\"GR\",\"title\":\"Παιχνίδι Του Στέμματος\",\"type\":\"\"},{\"iso_3166_1\":\"HK\",\"title\":\"權力遊戲\",\"type\":\"\"},{\"iso_3166_1\":\"IR\",\"title\":\"Baziye tajo takht\",\"type\":\"romanization\"},{\"iso_3166_1\":\"IR\",\"title\":\"بازی تاج و تخت\",\"type\":\"\"},{\"iso_3166_1\":\"IR\",\"title\":\"گیم آف ترونز\",\"type\":\"\"},{\"iso_3166_1\":\"KR\",\"title\":\"왕좌의 게임\",\"type\":\"\"},{\"iso_3166_1\":\"LT\",\"title\":\"Sostų žaidimas\",\"type\":\"\"},{\"iso_3166_1\":\"LV\",\"title\":\"Troņu spēle\",\"type\":\"\"},{\"iso_3166_1\":\"MK\",\"title\":\"Игра на тронови\",\"type\":\"\"},{\"iso_3166_1\":\"PL\",\"title\":\"Gra o tron\",\"type\":\"\"},{\"iso_3166_1\":\"SI\",\"title\":\"Igra prestolov\",\"type\":\"\"},{\"iso_3166_1\":\"TH\",\"title\":\"มหาศึกชิงบัลลังก์\",\"type\":\"\"},{\"iso_3166_1\":\"TR\",\"title\":\"Taht Oyunları\",\"type\":\"\"},{\"iso_3166_1\":\"US\",\"title\":\"A Song of Ice and Fire\",\"type\":\"working title\"},{\"iso_3166_1\":\"US\",\"title\":\"GoT\",\"type\":\"common abbreviation\"},{\"iso_3166_1\":\"US\",\"title\":\"Game of Thrones .jpg\",\"type\":\"Alternative title\"},{\"iso_3166_1\":\"UZ\",\"title\":\"Taxtlar o'yini\",\"type\":\"\"},{\"iso_3166_1\":\"UZ\",\"title\":\"Taxt o'yinlari\",\"type\":\"\"}]}"
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "id": {
                      "type": "integer",
                      "example": 1399,
                      "default": 0
                    },
                    "results": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "iso_3166_1": {
                            "type": "string",
                            "example": "AL"
                          },
                          "title": {
                            "type": "string",
                            "example": "Froni i shpatave"
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