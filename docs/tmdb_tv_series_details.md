> ## Documentation Index
> Fetch the complete documentation index at: https://developer.themoviedb.org/llms.txt
> Use this file to discover all available pages before exploring further.

# Details

Get the details of a TV show.

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
    "/3/tv/{series_id}": {
      "get": {
        "summary": "Details",
        "description": "Get the details of a TV show.",
        "operationId": "tv-series-details",
        "parameters": [
          {
            "name": "series_id",
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
                  "Result": {
                    "value": "{\n  \"adult\": false,\n  \"backdrop_path\": \"/6LWy0jvMpmjoS9fojNgHIKoWL05.jpg\",\n  \"created_by\": [\n    {\n      \"id\": 9813,\n      \"credit_id\": \"5256c8c219c2956ff604858a\",\n      \"name\": \"David Benioff\",\n      \"gender\": 2,\n      \"profile_path\": \"/xvNN5huL0X8yJ7h3IZfGG4O2zBD.jpg\"\n    },\n    {\n      \"id\": 228068,\n      \"credit_id\": \"552e611e9251413fea000901\",\n      \"name\": \"D.B. Weiss\",\n      \"gender\": 2,\n      \"profile_path\": \"/2RMejaT793U9KRk2IEbFfteQntE.jpg\"\n    }\n  ],\n  \"episode_run_time\": [\n    60\n  ],\n  \"first_air_date\": \"2011-04-17\",\n  \"genres\": [\n    {\n      \"id\": 10765,\n      \"name\": \"Sci-Fi & Fantasy\"\n    },\n    {\n      \"id\": 18,\n      \"name\": \"Drama\"\n    },\n    {\n      \"id\": 10759,\n      \"name\": \"Action & Adventure\"\n    }\n  ],\n  \"homepage\": \"http://www.hbo.com/game-of-thrones\",\n  \"id\": 1399,\n  \"in_production\": false,\n  \"languages\": [\n    \"en\"\n  ],\n  \"last_air_date\": \"2019-05-19\",\n  \"last_episode_to_air\": {\n    \"id\": 1551830,\n    \"name\": \"The Iron Throne\",\n    \"overview\": \"In the aftermath of the devastating attack on King's Landing, Daenerys must face the survivors.\",\n    \"vote_average\": 4.809,\n    \"vote_count\": 241,\n    \"air_date\": \"2019-05-19\",\n    \"episode_number\": 6,\n    \"production_code\": \"806\",\n    \"runtime\": 80,\n    \"season_number\": 8,\n    \"show_id\": 1399,\n    \"still_path\": \"/zBi2O5EJfgTS6Ae0HdAYLm9o2nf.jpg\"\n  },\n  \"name\": \"Game of Thrones\",\n  \"next_episode_to_air\": null,\n  \"networks\": [\n    {\n      \"id\": 49,\n      \"logo_path\": \"/tuomPhY2UtuPTqqFnKMVHvSb724.png\",\n      \"name\": \"HBO\",\n      \"origin_country\": \"US\"\n    }\n  ],\n  \"number_of_episodes\": 73,\n  \"number_of_seasons\": 8,\n  \"origin_country\": [\n    \"US\"\n  ],\n  \"original_language\": \"en\",\n  \"original_name\": \"Game of Thrones\",\n  \"overview\": \"Seven noble families fight for control of the mythical land of Westeros. Friction between the houses leads to full-scale war. All while a very ancient evil awakens in the farthest north. Amidst the war, a neglected military order of misfits, the Night's Watch, is all that stands between the realms of men and icy horrors beyond.\",\n  \"popularity\": 346.098,\n  \"poster_path\": \"/1XS1oqL89opfnbLl8WnZY1O1uJx.jpg\",\n  \"production_companies\": [\n    {\n      \"id\": 76043,\n      \"logo_path\": \"/9RO2vbQ67otPrBLXCaC8UMp3Qat.png\",\n      \"name\": \"Revolution Sun Studios\",\n      \"origin_country\": \"US\"\n    },\n    {\n      \"id\": 12525,\n      \"logo_path\": null,\n      \"name\": \"Television 360\",\n      \"origin_country\": \"\"\n    },\n    {\n      \"id\": 5820,\n      \"logo_path\": null,\n      \"name\": \"Generator Entertainment\",\n      \"origin_country\": \"GB\"\n    },\n    {\n      \"id\": 12526,\n      \"logo_path\": null,\n      \"name\": \"Bighead Littlehead\",\n      \"origin_country\": \"\"\n    }\n  ],\n  \"production_countries\": [\n    {\n      \"iso_3166_1\": \"GB\",\n      \"name\": \"United Kingdom\"\n    },\n    {\n      \"iso_3166_1\": \"US\",\n      \"name\": \"United States of America\"\n    }\n  ],\n  \"seasons\": [\n    {\n      \"air_date\": \"2010-12-05\",\n      \"episode_count\": 272,\n      \"id\": 3627,\n      \"name\": \"Specials\",\n      \"overview\": \"\",\n      \"poster_path\": \"/kMTcwNRfFKCZ0O2OaBZS0nZ2AIe.jpg\",\n      \"season_number\": 0,\n      \"vote_average\": 0\n    },\n    {\n      \"air_date\": \"2011-04-17\",\n      \"episode_count\": 10,\n      \"id\": 3624,\n      \"name\": \"Season 1\",\n      \"overview\": \"Trouble is brewing in the Seven Kingdoms of Westeros. For the driven inhabitants of this visionary world, control of Westeros' Iron Throne holds the lure of great power. But in a land where the seasons can last a lifetime, winter is coming...and beyond the Great Wall that protects them, an ancient evil has returned. In Season One, the story centers on three primary areas: the Stark and the Lannister families, whose designs on controlling the throne threaten a tenuous peace; the dragon princess Daenerys, heir to the former dynasty, who waits just over the Narrow Sea with her malevolent brother Viserys; and the Great Wall--a massive barrier of ice where a forgotten danger is stirring.\",\n      \"poster_path\": \"/wgfKiqzuMrFIkU1M68DDDY8kGC1.jpg\",\n      \"season_number\": 1,\n      \"vote_average\": 8.3\n    },\n    {\n      \"air_date\": \"2012-04-01\",\n      \"episode_count\": 10,\n      \"id\": 3625,\n      \"name\": \"Season 2\",\n      \"overview\": \"The cold winds of winter are rising in Westeros...war is coming...and five kings continue their savage quest for control of the all-powerful Iron Throne. With winter fast approaching, the coveted Iron Throne is occupied by the cruel Joffrey, counseled by his conniving mother Cersei and uncle Tyrion. But the Lannister hold on the Throne is under assault on many fronts. Meanwhile, a new leader is rising among the wildings outside the Great Wall, adding new perils for Jon Snow and the order of the Night's Watch.\",\n      \"poster_path\": \"/9xfNkPwDOqyeUvfNhs1XlWA0esP.jpg\",\n      \"season_number\": 2,\n      \"vote_average\": 8.2\n    },\n    {\n      \"air_date\": \"2013-03-31\",\n      \"episode_count\": 10,\n      \"id\": 3626,\n      \"name\": \"Season 3\",\n      \"overview\": \"Duplicity and treachery...nobility and honor...conquest and triumph...and, of course, dragons. In Season 3, family and loyalty are the overarching themes as many critical storylines from the first two seasons come to a brutal head. Meanwhile, the Lannisters maintain their hold on King's Landing, though stirrings in the North threaten to alter the balance of power; Robb Stark, King of the North, faces a major calamity as he tries to build on his victories; a massive army of wildlings led by Mance Rayder march for the Wall; and Daenerys Targaryen--reunited with her dragons--attempts to raise an army in her quest for the Iron Throne.\",\n      \"poster_path\": \"/5MkZjRnCKiIGn3bkXrXfndEzqOU.jpg\",\n      \"season_number\": 3,\n      \"vote_average\": 8.2\n    },\n    {\n      \"air_date\": \"2014-04-06\",\n      \"episode_count\": 10,\n      \"id\": 3628,\n      \"name\": \"Season 4\",\n      \"overview\": \"The War of the Five Kings is drawing to a close, but new intrigues and plots are in motion, and the surviving factions must contend with enemies not only outside their ranks, but within.\",\n      \"poster_path\": \"/jXIMScXE4J4EVHUba1JgxZnWbo4.jpg\",\n      \"season_number\": 4,\n      \"vote_average\": 8.4\n    },\n    {\n      \"air_date\": \"2015-04-12\",\n      \"episode_count\": 10,\n      \"id\": 62090,\n      \"name\": \"Season 5\",\n      \"overview\": \"The War of the Five Kings, once thought to be drawing to a close, is instead entering a new and more chaotic phase. Westeros is on the brink of collapse, and many are seizing what they can while the realm implodes, like a corpse making a feast for crows.\",\n      \"poster_path\": \"/7Q1Hy1AHxAzA2lsmzEMBvuWTX0x.jpg\",\n      \"season_number\": 5,\n      \"vote_average\": 8.2\n    },\n    {\n      \"air_date\": \"2016-04-24\",\n      \"episode_count\": 10,\n      \"id\": 71881,\n      \"name\": \"Season 6\",\n      \"overview\": \"Following the shocking developments at the conclusion of season five, survivors from all parts of Westeros and Essos regroup to press forward, inexorably, towards their uncertain individual fates. Familiar faces will forge new alliances to bolster their strategic chances at survival, while new characters will emerge to challenge the balance of power in the east, west, north and south.\",\n      \"poster_path\": \"/p1udLh0gfqyZFmXBGa393gk8go5.jpg\",\n      \"season_number\": 6,\n      \"vote_average\": 8.3\n    },\n    {\n      \"air_date\": \"2017-07-16\",\n      \"episode_count\": 7,\n      \"id\": 81266,\n      \"name\": \"Season 7\",\n      \"overview\": \"The long winter is here. And with it comes a convergence of armies and attitudes that have been brewing for years.\",\n      \"poster_path\": \"/oX51n32QyHeFP5kErksemJsJljL.jpg\",\n      \"season_number\": 7,\n      \"vote_average\": 8.2\n    },\n    {\n      \"air_date\": \"2019-04-14\",\n      \"episode_count\": 6,\n      \"id\": 107971,\n      \"name\": \"Season 8\",\n      \"overview\": \"The Great War has come, the Wall has fallen and the Night King's army of the dead marches towards Westeros. The end is here, but who will take the Iron Throne?\",\n      \"poster_path\": \"/3OcQhbrecf4F4pYss2gSirTGPvD.jpg\",\n      \"season_number\": 8,\n      \"vote_average\": 6.5\n    }\n  ],\n  \"spoken_languages\": [\n    {\n      \"english_name\": \"English\",\n      \"iso_639_1\": \"en\",\n      \"name\": \"English\"\n    }\n  ],\n  \"status\": \"Ended\",\n  \"tagline\": \"Winter Is Coming\",\n  \"type\": \"Scripted\",\n  \"vote_average\": 8.438,\n  \"vote_count\": 21390\n}"
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "adult": {
                      "type": "boolean",
                      "example": false,
                      "default": true
                    },
                    "backdrop_path": {
                      "type": "string",
                      "example": "/6LWy0jvMpmjoS9fojNgHIKoWL05.jpg"
                    },
                    "created_by": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "id": {
                            "type": "integer",
                            "example": 9813,
                            "default": 0
                          },
                          "credit_id": {
                            "type": "string",
                            "example": "5256c8c219c2956ff604858a"
                          },
                          "name": {
                            "type": "string",
                            "example": "David Benioff"
                          },
                          "gender": {
                            "type": "integer",
                            "example": 2,
                            "default": 0
                          },
                          "profile_path": {
                            "type": "string",
                            "example": "/xvNN5huL0X8yJ7h3IZfGG4O2zBD.jpg"
                          }
                        }
                      }
                    },
                    "episode_run_time": {
                      "type": "array",
                      "items": {
                        "type": "integer",
                        "example": 60,
                        "default": 0
                      }
                    },
                    "first_air_date": {
                      "type": "string",
                      "example": "2011-04-17"
                    },
                    "genres": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "id": {
                            "type": "integer",
                            "example": 10765,
                            "default": 0
                          },
                          "name": {
                            "type": "string",
                            "example": "Sci-Fi & Fantasy"
                          }
                        }
                      }
                    },
                    "homepage": {
                      "type": "string",
                      "example": "http://www.hbo.com/game-of-thrones"
                    },
                    "id": {
                      "type": "integer",
                      "example": 1399,
                      "default": 0
                    },
                    "in_production": {
                      "type": "boolean",
                      "example": false,
                      "default": true
                    },
                    "languages": {
                      "type": "array",
                      "items": {
                        "type": "string",
                        "example": "en"
                      }
                    },
                    "last_air_date": {
                      "type": "string",
                      "example": "2019-05-19"
                    },
                    "last_episode_to_air": {
                      "type": "object",
                      "properties": {
                        "id": {
                          "type": "integer",
                          "example": 1551830,
                          "default": 0
                        },
                        "name": {
                          "type": "string",
                          "example": "The Iron Throne"
                        },
                        "overview": {
                          "type": "string",
                          "example": "In the aftermath of the devastating attack on King's Landing, Daenerys must face the survivors."
                        },
                        "vote_average": {
                          "type": "number",
                          "example": 4.809,
                          "default": 0
                        },
                        "vote_count": {
                          "type": "integer",
                          "example": 241,
                          "default": 0
                        },
                        "air_date": {
                          "type": "string",
                          "example": "2019-05-19"
                        },
                        "episode_number": {
                          "type": "integer",
                          "example": 6,
                          "default": 0
                        },
                        "production_code": {
                          "type": "string",
                          "example": "806"
                        },
                        "runtime": {
                          "type": "integer",
                          "example": 80,
                          "default": 0
                        },
                        "season_number": {
                          "type": "integer",
                          "example": 8,
                          "default": 0
                        },
                        "show_id": {
                          "type": "integer",
                          "example": 1399,
                          "default": 0
                        },
                        "still_path": {
                          "type": "string",
                          "example": "/zBi2O5EJfgTS6Ae0HdAYLm9o2nf.jpg"
                        }
                      }
                    },
                    "name": {
                      "type": "string",
                      "example": "Game of Thrones"
                    },
                    "next_episode_to_air": {},
                    "networks": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "id": {
                            "type": "integer",
                            "example": 49,
                            "default": 0
                          },
                          "logo_path": {
                            "type": "string",
                            "example": "/tuomPhY2UtuPTqqFnKMVHvSb724.png"
                          },
                          "name": {
                            "type": "string",
                            "example": "HBO"
                          },
                          "origin_country": {
                            "type": "string",
                            "example": "US"
                          }
                        }
                      }
                    },
                    "number_of_episodes": {
                      "type": "integer",
                      "example": 73,
                      "default": 0
                    },
                    "number_of_seasons": {
                      "type": "integer",
                      "example": 8,
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
                      "example": "Game of Thrones"
                    },
                    "overview": {
                      "type": "string",
                      "example": "Seven noble families fight for control of the mythical land of Westeros. Friction between the houses leads to full-scale war. All while a very ancient evil awakens in the farthest north. Amidst the war, a neglected military order of misfits, the Night's Watch, is all that stands between the realms of men and icy horrors beyond."
                    },
                    "popularity": {
                      "type": "number",
                      "example": 346.098,
                      "default": 0
                    },
                    "poster_path": {
                      "type": "string",
                      "example": "/1XS1oqL89opfnbLl8WnZY1O1uJx.jpg"
                    },
                    "production_companies": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "id": {
                            "type": "integer",
                            "example": 76043,
                            "default": 0
                          },
                          "logo_path": {
                            "type": "string",
                            "example": "/9RO2vbQ67otPrBLXCaC8UMp3Qat.png"
                          },
                          "name": {
                            "type": "string",
                            "example": "Revolution Sun Studios"
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
                            "example": "GB"
                          },
                          "name": {
                            "type": "string",
                            "example": "United Kingdom"
                          }
                        }
                      }
                    },
                    "seasons": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "air_date": {
                            "type": "string",
                            "example": "2010-12-05"
                          },
                          "episode_count": {
                            "type": "integer",
                            "example": 272,
                            "default": 0
                          },
                          "id": {
                            "type": "integer",
                            "example": 3627,
                            "default": 0
                          },
                          "name": {
                            "type": "string",
                            "example": "Specials"
                          },
                          "overview": {
                            "type": "string",
                            "example": ""
                          },
                          "poster_path": {
                            "type": "string",
                            "example": "/kMTcwNRfFKCZ0O2OaBZS0nZ2AIe.jpg"
                          },
                          "season_number": {
                            "type": "integer",
                            "example": 0,
                            "default": 0
                          },
                          "vote_average": {
                            "type": "integer",
                            "example": 0,
                            "default": 0
                          }
                        }
                      }
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
                      "example": "Ended"
                    },
                    "tagline": {
                      "type": "string",
                      "example": "Winter Is Coming"
                    },
                    "type": {
                      "type": "string",
                      "example": "Scripted"
                    },
                    "vote_average": {
                      "type": "number",
                      "example": 8.438,
                      "default": 0
                    },
                    "vote_count": {
                      "type": "integer",
                      "example": 21390,
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