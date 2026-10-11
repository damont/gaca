# Daily briefing API

A briefing is one day's digest, assembled from independent **sections**. Each
source (calendar, weather, news, sports…) PUTs its own section; the app reads
the assembled day. All routes need the normal bearer token and are scoped to
that user. Base: `https://<your-server>/api`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/briefings?date=YYYY-MM-DD` | The day's briefing (empty, not 404, if nothing posted). Omit `date` = server's UTC today; clients should pass their local date. |
| `GET` | `/briefings/dates` | Days that have content, newest first (max 60). |
| `PUT` | `/briefings/{date}` | Set the overall summary: `{"summary": "…"}`. Sections untouched. |
| `PUT` | `/briefings/{date}/sections/{key}` | Create/**replace** one section. Idempotent — safe for cron re-runs. |
| `DELETE` | `/briefings/{date}/sections/{key}` | Remove a section (404 if absent). |

`{key}` is a slug: `^[a-z0-9][a-z0-9_-]{0,39}$` (e.g. `calendar`, `weather`, `news`, `sports`).

## Section body

```json
{
  "title": "Family calendar",
  "kind": "calendar",              // general | calendar | weather | news | sports | tasks (display hint)
  "headline": "2 things today",    // optional one-liner shown under the title
  "display_order": 1,              // ascending; ties broken by key
  "items": [                       // up to 50, shown in the order sent
    {
      "title": "Soccer practice",  // required, ≤200
      "time": "5:00 PM",           // free text, shown as-is (poster owns timezone/format)
      "location": "Riverside Park",
      "body": "Bring cleats",      // ≤2000
      "link": "https://…",         // http(s) only
      "priority": "high"           // high | medium (default) | low
    }
  ]
}
```

## Conventions for the morning cron

- Post each source as its own section so one failing source doesn't block the rest.
- Post `summary` last, once you know what else made it in.
- Re-posting a section replaces it. To retract a section, DELETE it.
- Send `date` as the user's local date (America/New_York), not UTC.
- Suggested keys/order: `calendar`(1), `weather`(2), `sports`(3), `news`(4).

```bash
curl -X PUT "$GACA/api/briefings/2026-10-01/sections/weather" \
  -H "Authorization: Bearer $GACA_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Weather","kind":"weather","headline":"72° and sunny","display_order":2,"items":[]}'
```
