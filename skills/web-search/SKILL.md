---
name: web-search
description: USE FOR web search. Returns ranked results with snippets, URLs, thumbnails. Supports freshness filters, SafeSearch, Goggles for custom ranking, pagination. Primary search endpoint.
---

# Web Search

> **Requires API Key**: Get one at https://api.search.brave.com
>
> **Plan**: Included in the **Search** plan. See https://api-dashboard.search.brave.com/app/subscriptions/subscribe

## Quick Start (cURL)

### Basic Search
```bash
curl -s "https://api.search.brave.com/res/v1/web/search?q=python+web+frameworks" \
  -H "Accept: application/json" \
  -H "X-Subscription-Token: ${BRAVE_SEARCH_API_KEY}"
```

### With Parameters
```bash
curl -s "https://api.search.brave.com/res/v1/web/search" \
  -H "Accept: application/json" \
  -H "X-Subscription-Token: ${BRAVE_SEARCH_API_KEY}" \
  -G \
  --data-urlencode "q=rust programming tutorials" \
  --data-urlencode "country=US" \
  --data-urlencode "search_lang=en" \
  --data-urlencode "count=10" \
  --data-urlencode "safesearch=moderate" \
  --data-urlencode "freshness=pm"
```

## Endpoint

```http
GET https://api.search.brave.com/res/v1/web/search
POST https://api.search.brave.com/res/v1/web/search
```

**Note**: Both GET and POST methods are supported. POST is useful for long queries or complex Goggles.

**Authentication**: `X-Subscription-Token: <API_KEY>` header

**Optional Headers**:
- `Accept-Encoding: gzip` — Enable gzip compression

## When to Use Web Search

| Feature | Web Search (this) | LLM Context (`llm-context`) | Answers (`answers`) |
|--|--|--|--|
| Output | Structured results (links, snippets, metadata) | Pre-extracted page content for LLMs | End-to-end AI answers with citations |
| Result types | Web, news, videos, discussions, FAQ, infobox, locations, rich | Extracted text chunks, tables, code | Synthesized answer + source list |
| Unique features | Goggles, structured data (`schemas`), rich callbacks | Token budget control, threshold modes | Multi-iteration search, streaming, OpenAI SDK compatible |
| Speed | Fast (~0.5-1s) | Fast (<1s) | Slower (~30-180s) |
| Best for | Search UIs, data extraction, custom ranking | RAG pipelines, AI agents, grounding | Chat interfaces, thorough research |

## Parameters

| Parameter | Type | Required | Default | Description |
|--|--|--|--|--|
| `q` | string | **Yes** | - | Search query (1-400 chars, max 50 words) |
| `country` | string | No | `US` | Search country (2-letter country code or `ALL`) |
| `search_lang` | string | No | `en` | Language preference (2+ char language code) |
| `ui_lang` | string | No | `en-US` | UI language (e.g., "en-US") |
| `count` | int | No | `20` | Max results per page (1-20) |
| `offset` | int | No | `0` | Page offset for pagination (0-9) |
| `safesearch` | string | No | `moderate` | Adult content filter (`off`/`moderate`/`strict`) |
| `freshness` | string | No | - | Time filter (`pd`/`pw`/`pm`/`py` or date range) |
| `text_decorations` | bool | No | `true` | Include highlight markers |
| `spellcheck` | bool | No | `true` | Auto-correct query |
| `result_filter` | string | No | - | Filter result types (comma-separated) |
| `goggles` | string | No | - | Custom ranking filter (URL or inline) |
| `extra_snippets` | bool | No | - | Get up to 5 extra snippets per result |
| `operators` | bool | No | `true` | Apply search operators |
| `units` | string | No | - | Measurement units (`metric`/`imperial`) |
| `enable_rich_callback` | bool | No | `false` | Enable rich 3rd party data callback |
| `include_fetch_metadata` | bool | No | `false` | Include `fetched_content_timestamp` on results |

### Freshness Values

| Value | Description |
|--|--|
| `pd` | Past day (24 hours) |
| `pw` | Past week (7 days) |
| `pm` | Past month (31 days) |
| `py` | Past year (365 days) |
| `YYYY-MM-DDtoYYYY-MM-DD` | Custom date range |

### Result Filter Values

Filter types: `discussions`, `faq`, `infobox`, `news`, `query`, `videos`, `web`, `locations`

```bash
# Only web and video results
curl "...&result_filter=web,videos"
```

### Location Headers (Optional)

For location-aware results, add these headers. **Lat/Long is sufficient** when coordinates are known — the other headers are only needed as a fallback when coordinates are unavailable.

| Header | Type | Description |
|--|--|--|
| `X-Loc-Lat` | float | User latitude (-90.0 to 90.0) |
| `X-Loc-Long` | float | User longitude (-180.0 to 180.0) |
| `X-Loc-Timezone` | string | IANA timezone (e.g., "America/San_Francisco") |
| `X-Loc-City` | string | City name |
| `X-Loc-State` | string | State/region code (ISO 3166-2) |
| `X-Loc-State-Name` | string | State/region full name (e.g., "California") |
| `X-Loc-Country` | string | 2-letter country code |
| `X-Loc-Postal-Code` | string | Postal code (e.g., "94105") |

> **Priority**: `X-Loc-Lat` + `X-Loc-Long` take precedence. When provided, downstream services resolve the location directly from coordinates and the text-based headers (City, State, Country, Postal-Code) are not used for location resolution. Provide text-based headers **only** when you don't have coordinates. Sending both won't break anything — lat/long simply wins.

## Response Format

### Response Fields

| Field | Type | Description |
|--|--|--|
| `type` | string | Always `"search"` |
| `query.original` | string | The original search query |
| `query.altered` | string? | Spellcheck-corrected query (if changed) |
| `query.cleaned` | string? | Cleaned/normalized query |
| `query.spellcheck_off` | bool? | Whether spellcheck was disabled |
| `query.more_results_available` | bool | Whether more pages exist |
| `query.show_strict_warning` | bool? | True if strict safesearch blocked adult results |
| `query.search_operators` | object? | Applied search operators (`applied`, `cleaned_query`, `sites`) |
| `web.type` | string | Always `"search"` |
| `web.results[].title` | string | Page title |
| `web.results[].url` | string | Page URL |
| `web.results[].description` | string? | Snippet/description text |
| `web.results[].age` | string? | Human-readable age (e.g., "2 days ago") |
| `web.results[].language` | string? | Content language code |
| `web.results[].meta_url` | object | URL components (`scheme`, `netloc`, `hostname`, `path`) |
| `web.results[].thumbnail` | object? | Thumbnail (`src`, `original`) |
| `web.results[].thumbnail.original` | string? | Original full-size image URL |
| `web.results[].thumbnail.logo` | bool? | Whether the thumbnail is a logo |
| `web.results[].profile` | object? | Publisher identity (`name`, `url`, `long_name`, `img`) |
| `web.results[].page_age` | string? | ISO datetime of publication (e.g., `"2025-04-12T14:22:41"`) |
| `web.results[].extra_snippets` | list[str]? | Up to 5 additional excerpts |
| `web.results[].deep_results` | object? | Additional links (`buttons`, `links`) from the page |
| `web.results[].schemas` | list? | Raw schema.org structured data |
| `web.results[].product` | object? | Product info and reviews |
| `web.results[].recipe` | object? | Recipe details (ingredients, time, ratings) |
| `web.results[].article` | object? | Article metadata (author, publisher, date) |
| `web.results[].book` | object? | Book info (author, ISBN, rating) |
| `web.results[].software` | object? | Software product info |
| `web.results[].rating` | object? | Aggregate ratings |
| `web.results[].faq` | object? | FAQ found on the page |
| `web.results[].movie` | object? | Movie info (directors, actors, genre) |
| `web.results[].video` | object? | Video metadata (duration, views, creator) |
| `web.results[].location` | object? | Location/restaurant details |
| `web.results[].qa` | object? | Question/answer info |
| `web.results[].creative_work` | object? | Creative work data |
| `web.results[].music_recording` | object? | Music/song data |
| `web.results[].organization` | object? | Organization info |
| `web.results[].review` | object? | Review data |
| `web.results[].content_type` | string? | Content type classification |
| `web.results[].fetched_content_timestamp` | int? | Fetch timestamp (with `include_fetch_metadata=true`) |
| `web.mutated_by_goggles` | bool | Whether results were re-ranked by Goggles |
| `web.family_friendly` | bool | Whether results are family-friendly |
| `mixed` | object? | Preferred display order (see Mixed Response below) |
| `discussions.results[]` | array? | Forum discussion clusters |
| `discussions.results[].data.forum_name` | string? | Forum/community name |
| `discussions.results[].data.num_answers` | int? | Number of answers/replies |
| `discussions.results[].data.question` | string? | Discussion question |
| `discussions.results[].data.top_comment` | string? | Top-voted comment excerpt |
| `faq.results[]` | array? | FAQ entries |
| `news.results[]` | array? | News articles |
| `videos.results[]` | array? | Video results |
| `infobox.results[]` | array? | Knowledge graph entries |
| `locations.results[]` | array? | Local POI results |
| `rich.hint.vertical` | string? | Rich result type |
| `rich.hint.callback_key` | string? | Callback key for rich data |

### JSON Example

```json
{
  "type": "search",
  "query": {
    "original": "python frameworks",
    "altered": "python web frameworks",
    "spellcheck_off": false,
    "more_results_available": true
  },
  "web": {
    "type": "search",
    "results": [
      {
        "title": "Top Python Web Frameworks",
        "url": "https://example.com/python-frameworks",
        "description": "A comprehensive guide to Python web frameworks...",
        "age": "2 days ago",
        "language": "en",
        "meta_url": {
          "scheme": "https",
          "netloc": "example.com",
          "hostname": "example.com",
          "path": "/python-frameworks"
        },
        "thumbnail": {
          "src": "https://...",
          "original": "https://original-image-url.com/img.jpg"
        },
        "extra_snippets": ["Additional excerpt 1...", "Additional excerpt 2..."]
      }
    ],
    "family_friendly": true
  },
  "mixed": {
    "type": "mixed",
    "main": [
      {"type": "web", "index": 0, "all": false},
      {"type": "web", "index": 1, "all": false},
      {"type": "videos", "all": true}
    ],
    "top": [],
    "side": []
  },
  "videos": { "...": "..." },
  "news": { "...": "..." },
  "rich": {
    "type": "rich",
    "hint": {
      "vertical": "weather",
      "callback_key": "<callback_key_hex>"
    }
  }
}
```

### Mixed Response

The `mixed` object defines the preferred display order of results across types. It contains three arrays:

| Array | Purpose |
|--|--|
| `main` | Primary result list (ordered sequence of results to display) |
| `top` | Results to display above main results |
| `side` | Results to display alongside main results (e.g., infobox) |

Each entry is a `ResultReference` with `type` (e.g., `"web"`, `"videos"`), `index` (into the corresponding result array), and `all` (`true` to include all results of that type at this position).

## Search Operators

| Operator | Syntax | Description |
|--|--|--|
| Site | `site:example.com` | Limit results to a specific domain |
| File extension | `ext:pdf` | Results with a specific file extension |
| File type | `filetype:pdf` | Results created in a specific file type |
| In title | `intitle:python` | Pages with term in the title |
| In body | `inbody:tutorial` | Pages with term in the body |
| In page | `inpage:guide` | Pages with term in title or body |
| Language | `lang:es` | Pages in a specific language (ISO 639-1) |
| Location | `loc:us` | Pages from a specific country (ISO 3166-1 alpha-2) |
| Include | `+term` | Force inclusion of a term |
| Exclude | `-term` | Exclude pages containing the term |
| Exact match | `"exact phrase"` | Match the exact phrase in order |
| AND | `term1 AND term2` | Both terms required (uppercase) |
| OR / NOT | `term1 OR term2`, `NOT term` | Logical operators (uppercase) |

Set `operators=false` to disable operator parsing.

## Goggles (Custom Ranking) — Unique to Brave

Goggles let you **re-rank search results** — boost trusted sources, suppress SEO spam, or build focused search scopes.

| Method | Example |
|--|--|
| **Hosted** | `--data-urlencode "goggles=https://raw.githubusercontent.com/brave/goggles-quickstart/main/goggles/rust_programming.goggle"` |
| **Inline** | `--data-urlencode 'goggles=$discard\n$site=example.com'` |

> **Hosted** goggles must be on GitHub/GitLab, include `! name:`, `! description:`, `! author:` headers, and be registered at https://search.brave.com/goggles/create. **Inline** rules need no registration.

**Syntax**: `$boost=N` / `$downrank=N` (1–10), `$discard`, `$site=example.com`. Combine with commas: `$site=example.com,boost=3`. Separate rules with `\n` (`%0A`).

**Allow list**: `$discard\n$site=docs.python.org\n$site=developer.mozilla.org` — **Block list**: `$discard,site=pinterest.com\n$discard,site=quora.com`

**Resources**: [Discover](https://search.brave.com/goggles/discover) · [Syntax](https://search.brave.com/help/goggles) · [Quickstart](https://github.com/brave/goggles-quickstart)

## Rich Data Enrichments

For queries about weather, stocks, sports, currency, etc., use the rich callback workflow:

```bash
# 1. Search with rich callback enabled
curl -s "https://api.search.brave.com/res/v1/web/search?q=weather+san+francisco&enable_rich_callback=true" \
  -H "X-Subscription-Token: ${BRAVE_SEARCH_API_KEY}"

# Response includes: "rich": {"hint": {"callback_key": "abc123...", "vertical": "weather"}}

# 2. Get rich data with the callback key
curl -s "https://api.search.brave.com/res/v1/web/rich?callback_key=abc123..." \
  -H "X-Subscription-Token: ${BRAVE_SEARCH_API_KEY}"
```

**Supported Rich Types**: Calculator, Definitions, Unit Conversion, Unix Timestamp, Package Tracker, Stock, Currency, Cryptocurrency, Weather, American Football, Baseball, Basketball, Cricket, Football/Soccer, Ice Hockey, Web3, Translator

### Rich Callback Endpoint

```http
GET https://api.search.brave.com/res/v1/web/rich
```

| Parameter | Type | Required | Description |
|--|--|--|--|
| `callback_key` | string | Yes | Callback key from the web search `rich.hint.callback_key` field |

## Use Cases

- **General-purpose search integration**: Richest result set (web, news, videos, discussions, FAQ, infobox, locations) in one call. For RAG/LLM grounding, prefer `llm-context`.
- **Structured data extraction**: Products, recipes, ratings, articles via `schemas` and typed fields on results.
- **Custom search with Goggles**: Unique to Brave. Boost/discard sites with inline rules or hosted Goggles for fully customized ranking.

## Notes

- **Pagination**: Use `offset` (0-9) with `count` to page through results
- **Count**: Max 20 for web search; actual results may be less than requested
