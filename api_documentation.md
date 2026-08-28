# API Documentation

The HTTP contract between the frontend (`/archivault-ui`) and the backend in
`/archival_materials_ingest/web-demo`. All endpoints sit behind the same CloudFront
distribution.

Related documents, which do not overlap with this one:

- `api_client_guide.md` — end-user guide to the `submit_job.py` CLI, task-oriented
- `job_metadata_reference.md` — every field accepted in the `metadata` object
- `architecture.md` — how the backend is put together and why
- `CODE_WALKTHROUGH.md` — code-level trace and current scaling limits

Every endpoint except signup and login requires `Authorization: Bearer <token>`.

## Authentication

Handled by `auth_lambda/auth.py`, which routes on path suffix.

### `POST /auth/signup`

Creates an account and logs in immediately.

```json
{ "email": "user@example.com", "password": "securepassword123" }
```

Password must be at least 6 characters. Returns the same body as login.
**409** if the user already exists; **400** on a malformed request.

### `POST /auth/login`

```json
{ "email": "user@example.com", "password": "securepassword123" }
```

```json
{ "token": "<sessionToken>", "userId": "user@example.com", "creditsRemaining": 25 }
```

**401** on invalid credentials. The token is a session token stored in the `Sessions`
table with an `expiresAt` timestamp; expired tokens are deleted on use and return **401**.

### `POST /auth/logout`

Deletes the session record. Acknowledges completion.

### `GET /auth/me`

```json
{ "userId": "user@example.com", "creditsRemaining": 25 }
```

**401** if the token is missing, expired, or the account no longer exists.

---

## Job creation

### `POST /presign`

Requests presigned S3 upload URLs, or copies files from an external bucket when
`source_bucket` is supplied.

* **Lambda:** `presign_lambda/presign.py`

Standard upload:

```json
{
  "job_title": "My Archival Job",
  "filenames": ["document1.pdf", "image1.jpg"],
  "steps": ["transcribe"],
  "country": "US",
  "state": "CA",
  "description": "Historical documents",
  "metadata": { "language": "english", "transcription_model": "gemini-3.1-pro-preview" }
}
```

External bucket import — `filenames` are source keys, and basenames must be unique
across the list:

```json
{
  "job_title": "My Archival Job",
  "source_bucket": "my-external-s3-bucket",
  "filenames": ["folder/document1.pdf", "folder/image1.jpg"],
  "steps": ["transcribe"]
}
```

Upload response returns `presignedUrls` mapping each filename to a `PUT` URL and its S3
key, plus `pdf_count`:

```json
{ "jobId": "<uuid>", "presignedUrls": [{ "filename": "…", "url": "…", "key": "…" }], "pdf_count": 1 }
```

Import response returns immediately with the copy running in the background:

```json
{ "jobId": "<uuid>", "presignedUrls": [], "pdf_count": 0, "status": "IMPORTING" }
```

Poll `GET /jobs/{jobId}` until the status leaves `IMPORTING` before continuing.

**400** on no valid keys or duplicate basenames. `metadata` is validated and
fuzzy-matched server-side; unrecognised values are set to `null` rather than rejected.

### `POST /pdf`

Begins PDF expansion. Call only when `pdf_count > 0`, and only after every upload has
completed.

* **Lambda:** `pdf_orchestrator/pdf_orchestrator.py`

```json
{ "jobId": "<uuid>" }
```

Responds **202** with the render plan. Each PDF is classified as rendered in one pass,
split into page-range chunks and rendered in parallel, or skipped when unreadable or
beyond `SPLIT_MAX_MB`:

```json
{
  "jobId": "<uuid>",
  "path": "split",
  "fast_submitted": 0,
  "split_submitted": 1,
  "render_units": 2,
  "skipped": [],
  "pdfs": [{ "key": "…", "sizeMB": 71.3, "pages": 560, "decision": "split", "chunkPages": 300, "units": 2 }]
}
```

Rendering is asynchronous. The job moves to `DERIVING`; poll `GET /jobs/{jobId}` until it
reaches `ENQUEUEING` before enqueuing. Skipped files leave a `skip_note` on the job and
contribute no images.

### `POST /jobs`

Enqueues an uploaded, expanded job. Checks and deducts credits against the image count.

* **Lambda:** `enqueue_lambda/enqueue.py`

```json
{ "jobId": "<uuid>", "steps": ["transcribe", "metadata"] }
```

Returns **202** with `{ "jobId": "<uuid>" }`.

* **402** when credits are short, with `credits_required` and `credits_remaining`
* **400** if PDFs are still outstanding — wait for `ENQUEUEING`
* **403** if the job belongs to another user

The authoritative image list comes from the job's S3 manifest; any keys in the request
body are ignored.

Valid `steps` values are `foliate`, `layout`, `transcribe`, `ner`, and `metadata`.
Any other value is rejected with a 400 by both `/presign` and `/enqueue`.
An empty list captions only.

---

## Job status

### `GET /jobs`

Job history for the authenticated user.

* **Lambda:** `status_lambda/status_handler.py`

Returns summaries projecting `jobId`, `job_title`, `status`, `createdAt`, `pdf_count`,
and `n_images`.

### `GET /jobs/{jobId}`

The current job record, fetched live from DynamoDB.

* **Lambda:** `status_lambda/status_handler.py`

Includes a server-computed `progress` object so clients need not know which internal
counter backs which phase:

```json
{
  "status": "DERIVING",
  "progress": { "phase": "DERIVING", "label": "Rendering PDF pages",
                "completed": 1, "total": 2, "unit": "chunks" }
}
```

`completed`, `total`, and `unit` are present only when the phase has a denominator —
`chunks` during PDF expansion, `batches` during the main pipeline. Phases without one
return `phase` and `label` alone, and clients should show an indeterminate indicator
rather than a zeroed bar.

When `status` is `COMPLETED` the response gains an `artifacts` object:

```json
{
  "artifacts": {
    "json": { "s3_key": "…/result.json", "presigned_url": "…" },
    "markdown": { "s3_key": "…/report.md", "presigned_url": "…" },
    "tables_zip": { "s3_key": "…/tables.zip", "presigned_url": "…" },
    "viewer_eligible": true
  }
}
```

`tables_zip` appears only when transcription found tabular data. `viewer_eligible` is true
when the job was transcribed and the user did not set `delete_data`; it gates the viewer
link below.

**403** if the job belongs to another user; **404** if it does not exist.

### `GET /jobs/{jobId}/viewer`

Presigned image URLs for the side-by-side transcription viewer.

* **Lambda:** `status_lambda/status_handler.py`

```json
{
  "job_id": "<uuid>",
  "job_title": "…",
  "images": [{ "key": "…", "filename": "…", "presigned_url": "…" }],
  "total_images": 560,
  "truncated": false,
  "result_url": "…",
  "transcriptions_url": "…",
  "preserve_line_breaks": true
}
```

`images` is capped at `VIEWER_MAX_IMAGES` (2000). When a job exceeds that, `truncated` is
true and `total_images` reports the real count — clients should surface this rather than
silently showing a prefix. `transcriptions_url` is present only for jobs that opted out of
line breaks, supplying the original line structure for alignment; treat it as optional.

**400** if the job is not viewer-eligible.

---

## Call sequence

```
POST /auth/login
POST /presign                          → jobId, presignedUrls, pdf_count
PUT  {presigned url}                   → once per file, direct to S3
POST /pdf                              → only if pdf_count > 0
GET  /jobs/{jobId}                     → poll until status leaves DERIVING
POST /jobs                             → credits deducted here
GET  /jobs/{jobId}                     → poll until COMPLETED, then read artifacts
GET  /jobs/{jobId}/viewer              → optional, if viewer_eligible
```
