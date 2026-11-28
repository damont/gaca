# Photos on artifact pages

A page can show photos and let the person add them. **The page never touches a camera or a file**: it asks the
app, the app shows its own sheet, and the page gets photo **ids** back. The page decides where those ids live in its
own data (`meals/tacos.photo`, a `photos` array, a collection with one document per photo). The agent adds photos
the same way: it gets an id and writes it into the data.

## Where photos are kept

| | where | phase |
|---|---|---|
| a page on your server | `artifact_photos` table in `~/gaca-data/gaca.db` (bytes in BLOB columns, like chat attachments); covered by `gaca-api backup` | 1 (this) |
| a page in GACA Cloud | a private, encrypted S3 bucket (`photos/<account>/<page>/<photo id>`), metadata in MongoDB | 4 |

Until phase 4, a page with photos can't be moved to the cloud (`has_photos`).

Every upload is decoded and re-encoded (`services/artifact_photos.py`): upright (EXIF orientation applied), at most
2048 px on the long side, plus a 480 px thumbnail, JPEG (or WebP when it has transparency), **no EXIF or other
metadata** (no location). Up to 15 MB per upload, 500 photos and 200 MB per page. A photo no state or document of
its page mentions is removed once it is 7 days old (checked whenever that page gets a new photo), and all of a page's
photos go when the page is deleted.

## API

| | |
|---|---|
| `POST /api/artifacts/{id}/photos` | body = the raw image bytes (any Content-Type). `201` → `PhotoOut {id, content_type, width, height, size, added_by, created_at}` |
| `POST /api/artifacts/{id}/photos/from-attachment` | `{"attachment_id": "..."}`: a photo sent in a chat, copied onto the page |
| `GET /api/artifacts/{id}/photos` | `{"photos": [PhotoOut...]}`, oldest first |
| `GET /api/artifacts/{id}/photos/{photo_id}[?thumb=true]` | the image |
| `DELETE /api/artifacts/{id}/photos/{photo_id}` | `204` |

Photo ids look like `ph_` + 16 hex characters. `added_by` is `agent` for an API token, `app` for a paired device.

## The page's side (`window.app.photos`)

```js
app.photos.add({ multiple })   // only from a tap; → [PhotoOut...] ([] if cancelled). The page saves the ids.
app.photos.url(id, { thumb })  // → a blob: URL for <img src>
app.photos.list()              // → [PhotoOut...]
```

Bridge methods (the SDK sends these like every other `app.*` call):

| method | args | who answers |
|---|---|---|
| `photo.get` | `{id, thumb}` | the server, through `POST /{id}/call` → `{id, content_type, width, height, data: base64}`. Apps need no change: they already forward unknown methods to `/call`. |
| `photo.list` | `{}` | the server, through `/call` → `{photos: [...]}` |
| `photo.add` | `{multiple}` | **the app** (phases 2 and 3). An app that forwards it to `/call` gets `400` "This version of the app can't add photos yet…", which the page shows. |

### What an app does for `photo.add`

1. Show its own sheet: **Take photo** / **Choose photo(s)** (`multiple` allows several). At most one sheet at a time;
   a second `photo.add` while one is open is answered with an error.
2. Shrink each picked image before uploading (about 2048 px long side, JPEG ~85%, HEIC converted to JPEG). The
   server re-encodes anyway; this only keeps uploads fast.
3. `POST /api/artifacts/{id}/photos` with the bytes, one per photo.
4. Answer the page `{photos: [PhotoOut...]}` (only the ones that uploaded), or `{photos: []}` if the person cancelled.
   If every upload failed, answer an error with the server's message.

The SDK refuses `add()` unless it runs straight from a tap (`navigator.userActivation.isActive`), so a page can't pop
the sheet on its own.
