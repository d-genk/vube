# Submitting Jobs to Archivault from the Command Line

A practical guide to running Archivault jobs programmatically using
`utility_scripts/submit_job.py`, or a client of your own modeled on it.

This guide is task-oriented. If you want the endpoint-by-endpoint reference, see
[`api_documentation.md`](api_documentation.md). If you want to understand what the
pipeline does internally, see [`architecture.md`](architecture.md) and
[`README.md`](README.md).

**Who this is for:** anyone who wants to process more material than is comfortable
to click through in the web UI, or who wants processing to run as part of a script
or scheduled workflow. You do not need to know AWS. You do need to be able to run a
command in a terminal.

---

## Contents

1. [Before you start](#1-before-you-start)
2. [Your first job](#2-your-first-job)
3. [Choosing processing steps](#3-choosing-processing-steps)
4. [Where your files come from](#4-where-your-files-come-from)
5. [Telling the system about your material](#5-telling-the-system-about-your-material)
6. [Choosing models](#6-choosing-models)
7. [Customizing metadata output](#7-customizing-metadata-output)
8. [Supplying per-image context](#8-supplying-per-image-context)
9. [Controlling foliation](#9-controlling-foliation)
10. [What you get back](#10-what-you-get-back)
11. [Credits and cost](#11-credits-and-cost)
12. [Best practices](#12-best-practices)
13. [Troubleshooting](#13-troubleshooting)
14. [Writing your own client](#14-writing-your-own-client)

---

## 1. Before you start

### Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.8 or newer | `python --version` to check. Any recent Python works. |
| The `requests` library | `pip install requests` |
| An Archivault account | Sign up in the web UI, or via `POST /auth/signup`. |
| Credits on that account | New accounts start with 25. One credit processes one image. |
| The script | `utility_scripts/submit_job.py` from this repository. |

That is the whole dependency list. `submit_job.py` uses only `requests` plus the
Python standard library, so it will run on a laptop, a lab workstation, or a
headless server without further setup.

```bash
pip install requests
```

If you are not comfortable installing packages globally, use a virtual environment:

```bash
python -m venv archivault-env && source archivault-env/bin/activate && pip install requests
```

On Windows the activation line is `archivault-env\Scripts\activate` instead.

### The API endpoint

All requests go to a single base URL, which is baked into the script as the default:

```
https://d2vqeenx44rrj7.cloudfront.net
```

You only need `--api-url` if you are pointed at a different deployment (a staging
stack, for instance).

### A note on credentials

`submit_job.py` takes `--email` and `--password` on the command line. This is
convenient for interactive use but has real drawbacks: the password is visible in
your shell history, and on shared machines it may be visible in the process list.

For anything beyond occasional manual runs, read the password from the environment
instead:

```bash
export ARCHIVAULT_PASSWORD='...'
python submit_job.py --email you@example.org --password "$ARCHIVAULT_PASSWORD" --dir ./scans
```

Logging in returns a session token that stays valid for **7 days**. If you are
building a longer-running integration, cache that token rather than re-authenticating
on every job. See [section 14](#14-writing-your-own-client).

---

## 2. Your first job

Put a handful of images in a directory and run:

```bash
python submit_job.py --email you@example.org --password YOUR_PASSWORD --dir ./scans --title "First test" --steps transcribe
```

The script will:

1. Log in and report how many credits you have left.
2. Find the eligible files in `./scans`.
3. Request presigned upload URLs and upload the files (8 at a time, with retries).
4. Expand any PDFs into page images.
5. Queue the job and poll until it finishes.
6. Download the result artifacts into `./output`, named after your job title.

Expect to wait a few minutes. The script polls every 30 seconds and prints the
current stage as it goes.

### Which files get picked up

From the directory you name, the script uploads files with these extensions:

```
.pdf  .jpg  .jpeg  .png  .tif  .tiff
```

Everything else is ignored, and the search is **not recursive** — only files sitting
directly in the named directory are considered. Subdirectories are skipped silently,
which is the single most common surprise for new users. If your scans are nested by
box or folder, either flatten them first or submit one job per directory.

### Start small

Run your first job on five or ten representative images, not on the whole
collection. It costs almost nothing, it takes a few minutes instead of an hour, and
it tells you whether your step selection and metadata hints are actually producing
what you want. Scaling up a configuration you have already validated is much less
painful than discovering a problem 4,000 images in.

---

## 3. Choosing processing steps

`--steps` decides what the pipeline actually does. It takes zero or more of:

| Step | What it does |
|---|---|
| `transcribe` | Produces a text transcription of each image. |
| `foliate` | Groups sequential images into logical items (a letter, a report, a bound section). |
| `ner` | Extracts named entities. Runs as part of transcription. |
| `metadata` | Generates Dublin Core metadata per item, plus MODS XML. |

Passing no steps at all is valid: every image still gets an automatically generated
caption, which is the pipeline's baseline output.

Steps combine freely, and the pipeline routes accordingly:

```bash
# Transcription only — the most common starting point
--steps transcribe

# Group pages into items, then transcribe each
--steps foliate transcribe

# The full treatment
--steps foliate transcribe ner metadata
```

### Two behaviors worth knowing

**`metadata` implies `foliate`.** Metadata is generated per *item*, not per image, so
the pipeline needs to know where item boundaries fall. If you ask for `metadata`
without `foliate`, the script adds `foliate` for you and sets an internal flag
(`foliation_override_discrete`) recording that you did not ask for grouping
explicitly. You do not need to do anything about this — just do not be surprised to
see foliation appear in your results.

**`ner` needs `transcribe`.** Named entity recognition operates on transcribed text.
Requesting `ner` alone will not produce entities.

### Steps the script does not expose

The backend also supports `layout` (PaddleOCR page-layout detection, which feeds
table extraction). `submit_job.py` restricts `--steps` to the four above, so if you
need layout you will need to either widen the `choices=[...]` list in the argument
parser or call the API directly ([section 14](#14-writing-your-own-client)).

The API accepts no steps beyond these five; anything else is rejected with a 400.

---

## 4. Where your files come from

There are two ingest flows. Use whichever matches where your material already lives.

### Local upload

Files on your own disk. This is the default and needs no AWS knowledge.

```bash
python submit_job.py --email you@example.org --password "$PW" --dir ./scans --steps transcribe
```

Uploads run 8 files in parallel, with five retry attempts and exponential backoff on
transient S3 errors, so a flaky connection will usually recover on its own.

Note that presigned upload URLs expire **one hour** after they are issued. For very
large batches over a slow connection, this is a real ceiling — split the batch into
multiple jobs rather than fighting it.

### S3 import

If your material already sits in an S3 bucket, skip the round trip through your
laptop entirely. Archivault copies server-side, which is dramatically faster for
large collections.

```bash
python submit_job.py --email you@example.org --password "$PW" --source-bucket my-collections-bucket --keys folder/img001.tif folder/img002.tif --steps transcribe
```

Requirements and constraints:

- The Archivault account must have read access to your bucket. Grant this via a
  bucket policy before you submit — contact info@archivault.ai for the principal
  to authorize.
- **Basenames must be unique across the key list.** `boxA/page1.tif` and
  `boxB/page1.tif` both flatten to `page1.tif` and the request will be rejected with
  a 400. Rename before importing.
- The copy runs in the background. The script polls the job status while it is
  `IMPORTING` and continues once it flips to `PENDING` or `ENQUEUEING`.

If you are supplying a context file or foliation file in this flow, include them in
`--keys` so they get copied alongside the images, then reference them by basename
with `--context-file` / `--foliation-file`.

---

## 5. Telling the system about your material

The pipeline works substantially better when it knows what it is looking at. These
flags are all optional, and all of them are worth setting when you know the answer.

| Flag | Accepted values |
|---|---|
| `--writing-style` | `handwritten`, `printed`, `typed` |
| `--language` | `english`, `spanish`, `portuguese`, `french`, `german`, `italian`, `latin` |
| `--time-period` | `contemporary`, `mid_20th_century`, `early_20th_century`, `19th_century_or_earlier` |
| `--layout-structure` | `free_form`, `paragraphs`, `lists`, `tables`, `forms`, `mixed` |
| `--non-textual-elements` | `illustrations`, `stamps_or_seals`, `handwritten_notes`, `diagrams`, `charts_or_graphs` (multiple allowed) |

Omitting `--language` means auto-detect, which is usually fine. Setting it helps on
short documents or mixed-script material where there is little text to detect from.

```bash
--writing-style handwritten --language spanish --time-period 19th_century_or_earlier --non-textual-elements stamps_or_seals illustrations
```

### Values are fuzzy-matched, and silently

The backend fuzzy-matches your input against the accepted vocabulary at an 80%
similarity threshold. So `"Handwritten"`, `"hand written"`, and `"handwriting"` all
resolve to `handwritten`. But a value that scores below the threshold is **discarded
silently** and treated as unset — you get no error and no warning.

The practical consequence: on your first job, read the JSON artifact and confirm the
output reflects the hints you meant to give. A typo that quietly dropped your
`--language` hint is much cheaper to catch on ten images than on a thousand.

### Transcription preferences

These control transcription style. Defaults are shown; each flag flips its setting.

| Flag | Default | Effect when passed |
|---|---|---|
| `--expand-abbreviations` | off | Expands "Dr." to "Doctor", etc. |
| `--no-preserve-line-breaks` | on | Reflows text instead of keeping original line breaks. |
| `--no-retain-punctuation` | on | Allows normalizing original punctuation and spelling. |
| `--normalize-to-modern` | off | Modernizes archaic spelling and phrasing. |
| `--ignore-marginalia` | off | Skips marginal notes. |

The defaults are deliberately conservative — they preserve the source as written,
which is normally what an archive wants. Reach for `--normalize-to-modern` only when
you are producing a reading edition rather than a faithful transcription.

### Free-text instructions

For project-specific guidance that the structured options do not cover:

```bash
--transcription-instructions "Render superscript ordinals inline. Transcribe crossed-out text in [brackets]."
```

**Capped at 500 characters**, silently truncated beyond that. Keep it to concrete,
checkable rules — this is not the place for general background about the collection.
Background belongs in `--description` or a context file.

### Job-level description

```bash
--title "Ramírez correspondence, box 3" --description "Business letters, 1890-1910, sent between Havana and Veracruz. Some water damage on later folders." --country Cuba --state Havana
```

`--description` feeds into transcription and metadata generation as general context
and is genuinely useful. `--title` is truncated to **32 characters** server-side, so
treat it as a short label rather than a full citation — it also becomes the filename
of your downloaded artifacts ([section 10](#10-what-you-get-back)), which is a second
reason to keep it short and distinctive. `--country` and `--state` record provenance.

---

## 6. Choosing models

Each pipeline module can use a different model. The available sets are tailored per
module and are not interchangeable — a model valid for transcription may not be
accepted for aggregation.

| Flag | Default | Available |
|---|---|---|
| `--transcription-model` | `gemini-3.7-flash` | `gemini-3.1-pro-preview`, `gpt-5.6-terra`, `gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gpt-5.6-luna` |
| `--captioning-model` | `gemini-3.5-flash-lite` | `gemini-3.7-flash`, `gpt-5.6-terra`, `gemini-3.5-flash-lite`, `gpt-5.6-luna` |
| `--foliation-model` | `gemini-3.7-flash` | `gemini-3.7-flash`, `gpt-5.6-terra`, `gemini-3.5-flash-lite`, `gpt-5.6-luna` |
| `--aggregation-model` | `gemini-3.5-flash-lite` | `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gpt-5.6-luna`, `gemini-3.7-flash` |
| `--metadata-model` | `gemini-3.7-flash` | `gemini-3.1-pro-preview`, `gpt-5.6-terra`, `gemini-3.7-flash`, `gemini-3.5-flash-lite` |
| `--ner-model` | `gemini-3.7-flash` | `gemini-3.7-flash`, `gpt-5.6-terra`, `gemini-3.5-flash-lite`, `gpt-5.6-luna` |

### How to think about this

The defaults are a reasonable balance and most jobs should leave them alone.

`gemini-3.7-flash` is the current default for most modules. Its published gains over
`gemini-3.6-flash` are on coding and agentic benchmarks rather than vision or OCR, so
`gemini-3.6-flash` is retained in the transcription set for direct comparison on your
own material.

Where upgrading pays off is **transcription of difficult material** — dense
secretary hand, heavy marginalia, damaged pages, unusual scripts. That is where
model quality translates most directly into output quality:

```bash
--transcription-model gemini-3.1-pro-preview
```

`--metadata-model` is the other place a stronger model shows, since descriptive
metadata rewards synthesis across a whole item.

Aggregation is different: it runs pairwise across every image in the job, so its
cost scales badly and its model set skews deliberately cheap. Raising it is rarely
worth the expense.

The honest test is empirical. Run the same twenty representative images through two
transcription models and compare. On clean printed text the difference is often
negligible; on hard manuscript it can be decisive.

---

## 7. Customizing metadata output

By default the `metadata` step produces the fifteen Dublin Core elements. If your
institution needs a different shape, supply your own schema — a JSON object mapping
field names to descriptions of what should go in them. Those descriptions are
instructions to the model, so write them as you would write them for a cataloger.

Either pass a file path:

```bash
--metadata-schema ./my_schema.json
```

Or a raw JSON string:

```bash
--metadata-schema '{"title": "...", "date_range": "..."}'
```

A minimal example:

```json
{
  "title": "A concise display title for the item.",
  "correspondents": "Names of sender and recipient, formatted 'Sender to Recipient'.",
  "date": "Date of composition in ISO 8601 format, as precise as the document allows.",
  "place_of_origin": "City and country where the document was written, if stated.",
  "condition": "Notes on physical condition: staining, tears, fading, missing portions."
}
```

**The serialized schema is limited to 4,000 characters.** Exceed it and the backend
logs a warning and **silently falls back to the default Dublin Core schema** — your
job will complete successfully with entirely the wrong fields. Check the length
before submitting:

```bash
python -c "import json;print(len(json.dumps(json.load(open('my_schema.json')))))"
```

Fewer, well-described fields beat many terse ones. A field described as
`"date"` will produce worse results than one described as
`"Date of composition in ISO 8601 format, as precise as the document allows."`

---

## 8. Supplying per-image context

If you already hold catalog data, finding-aid entries, or accession notes keyed to
individual images, you can feed it to the pipeline. This is the single highest-value
option in this guide for institutions with existing description.

The file is a JSON **array** of objects with `file` and `context` keys, where `file`
is the image's basename:

```json
[
  {"file": "img001.tif", "context": "Letter from María Ramírez to her brother Tomás, dated 12 March 1893. Accession 1994.22."},
  {"file": "img002.tif", "context": "Verso of preceding letter. Postal markings from Havana."}
]
```

```bash
--context-file ./context.json
```

Constraints:

- Must be a JSON array at the top level. If it is not — or if the file cannot be
  parsed — the context is **dropped silently** and the job proceeds without it.
- Context is truncated at **2,000 characters per image**. Put the most important
  information first.
- `file` must match the image basename exactly, including extension and case.
  Non-matching entries are ignored.

By default the context is offered to every module. Narrow it if the information is
only relevant to some:

```bash
--additional-context-modules transcription metadata
```

Valid modules: `captioning`, `foliation`, `metadata`, `transcription`, `ner`,
`aggregation`, `layout`.

Narrowing is worth doing when your context is specialized. Detailed provenance notes
help metadata generation and mislead captioning; physical-description notes ("torn
along the left margin") help transcription and are noise everywhere else.

---

## 9. Controlling foliation

Foliation groups images into logical items. Two options adjust how it behaves.

### Supplying known boundaries

If you already know where items begin and end, provide them rather than asking the
model to infer them. This is more accurate and cheaper.

The file is a JSON array of arrays, each inner array listing the basenames belonging
to one item:

```json
[
  ["img001.tif", "img002.tif"],
  ["img003.tif"],
  ["img004.tif", "img005.tif", "img006.tif"]
]
```

```bash
--foliation-file ./boundaries.json
```

Passing this automatically adds `foliate` to your steps. As with the context file, a
malformed file is dropped silently — verify your grouping appears in the JSON artifact.

### Grouping criteria

By default, foliation and aggregation group on **physical characteristics** — paper,
hand, ink, page sequence. This keeps two separate letters from the same
correspondent as two separate items, which is normally what you want.

```bash
--allow-subject-similarity
```

Passing this loosens the criterion so items sharing a subject or correspondent may be
grouped together. Useful when you are trying to reconstruct a thematic unit spread
across physically distinct documents; actively harmful for typical
item-level description. Leave it off unless you have a specific reason.

---

## 10. What you get back

When the job completes, `submit_job.py` downloads the artifacts into `--out-dir`
(default `./output`):

| Artifact | Contents |
|---|---|
| `<name>.json` | The full structured output. This is the authoritative record. |
| `<name>.md` | A formatted, human-readable Markdown report of the same results. |
| `<name>_tables.zip` | Excel-ready CSVs of extracted tabular data. Only present when the job ran layout analysis alongside transcription and found tables. |

### How artifacts are named

`<name>` is **your job title**, or the **job ID** when the job has no title. A job
titled `Ramirez box 3` produces:

```
Ramirez box 3.json
Ramirez box 3.md
Ramirez box 3_tables.zip
```

An untitled job produces `7f3a....json`, and so on, using the job ID.

This naming is applied server-side by the aggregator, which stamps each artifact
with a `Content-Disposition` header when it writes it to S3. Both the web UI and
`submit_job.py` honor that header, so a given job's artifacts have the same names
whether you download them through the browser or the script.

Two details worth knowing:

- **Titles are truncated to 32 characters** server-side, so filenames are too. Keep
  titles short and distinctive; `Ramirez box 3` is a better filename than
  `Ramirez family business correspondence, box 3`, which will be cut mid-word.
- **Characters that are illegal in filenames are replaced with underscores.** A job
  titled `Box 3: A/B letters` writes `Box 3_ A_B letters.json`. This affects
  `: / \ * ? " < > |` and applies on every platform, so results are consistent
  between Windows, macOS, and Linux.

### Downloading into the same directory twice

Distinct job titles produce distinct filenames, so successive jobs no longer
overwrite one another's output. If a filename *is* already taken — most commonly
because you reran a job under the same title — the script adds a numeric suffix
rather than clobbering the existing file:

```
Ramirez box 3.json      ← first run
Ramirez box 3_1.json    ← second run
Ramirez box 3_2.json    ← third run
```

The practical consequence is that nothing in `--out-dir` is ever silently
destroyed, but a repeatedly rerun job will accumulate files. If you want each run
to stand alone, give it its own `--out-dir`:

```bash
--out-dir ./results/box3-$(date +%Y%m%d-%H%M)
```

If you are scripting against a *predictable* output path, do not rely on the
suffix rule — write each run to a fresh directory instead, so the name you expect
is the name you get.

MODS XML is written per item to S3 under `uploads/{jobId}/xml/` and referenced from
the JSON artifact via each group's `xml_s3_key`.

### Shape of the JSON artifact

The JSON artifact is stored in S3 as `uploads/{jobId}/result.json` but downloads as
`<name>.json`. If you see it called `result.json` elsewhere in this repository's
documentation, that is the S3 key, not the file you end up with.

**Without foliation**, a list of images, each with `file`, `image_caption`, and —
depending on steps — `transcription`, `image_named_entities`.

**With foliation**, a list of groups, each with `title`, `item_description`, `images`
(the filenames in that group), and — depending on steps — `metadata`,
`item_named_entities`, `xml_s3_key`.

This difference matters if you are writing anything downstream. Foliating changes the
top-level shape of the file, so a parser written against one will not read the other.

### Deleting source data

```bash
--delete-data
```

Removes source derivatives from S3 after processing completes, reducing storage
footprint. Your results are unaffected. Worth setting for sensitive material or
routine bulk processing — but note it is not reversible, so make sure you have your
artifacts before relying on it.

---

## 11. Credits and cost

**One credit per image.** New accounts start with 25.

The details that matter:

- Credits are checked and deducted at the **enqueue** step, after upload and after
  PDF expansion. If you are short, the job fails there with HTTP 402 and nothing is
  charged.
- **PDFs are counted by page, not by file.** A single 300-page PDF costs 300 credits.
  This is by far the easiest way to consume credits unexpectedly.
- Cost is driven by image count only. Adding steps or choosing a stronger model does
  not change the credit charge.
- Long PDFs are no longer skipped. Anything past roughly 400 pages is split into
  chunks and rendered in parallel, so a 2,000-page volume expands fully — and costs
  2,000 credits. Only files that are unreadable or larger than about 2 GB are skipped;
  those leave a note on the job and contribute no images.

Check your balance before a large run — the script prints it at login, or:

```bash
curl -H "Authorization: Bearer $TOKEN" https://d2vqeenx44rrj7.cloudfront.net/auth/me
```

To request more credits, contact info@archivault.ai.

---

## 12. Best practices

**Pilot before you scale.** Ten representative images, then read the JSON artifact
properly. Confirm your hints registered, your schema is the one you wrote, and your
foliation boundaries look right. Most configuration errors in this system fail
silently, so inspection is the only way to catch them.

**Count pages before submitting PDFs.** `--dir` with a folder of large PDFs can
consume a credit balance in a single command. Count first.

**Prefer S3 import for large collections.** Server-side copy avoids pulling
everything through your local connection, and sidesteps the one-hour presigned URL
expiry entirely.

**Give every job a distinct title.** Titles become artifact filenames, so unique
titles keep successive runs from piling into ambiguously named files. Within the
32-character limit, something like `Ramirez b3 1890-1900` carries more than
`Test job`.

**Keep jobs coherent.** Metadata hints apply to the entire job. A batch that mixes
handwritten Spanish letters with typed English reports cannot be described accurately
by one set of hints. Split by material type — you will get better results and more
interpretable output.

**Split very large batches.** Several jobs of a few hundred images are easier to
monitor, cheaper to retry, and less exposed to upload-window problems than one job of
several thousand.

**Save your invocation.** Put the full command in a shell script and commit it
alongside your data. Reproducing a result six months later means knowing exactly which
models and options produced it. This also makes the configuration reviewable by
colleagues who did not run it.

**Do not put passwords in scripts.** Read from the environment. If you are running
unattended, cache the 7-day session token rather than storing the password at all.

**Log the job ID.** `submit_job.py` prints it early. Keep it — it is what you need to
re-query status or artifacts later, and it is the first thing support will ask for.

**Be deliberate about `--allow-subject-similarity` and `--normalize-to-modern`.**
Both change output in ways that are hard to detect after the fact and hard to undo
without reprocessing.

---

## 13. Troubleshooting

### The script exits immediately

| Message | Cause |
|---|---|
| `No valid files (images/PDFs) found` | Wrong directory, unsupported extensions, or files are in subdirectories. The search is not recursive. |
| `Directory '...' does not exist` | Path typo, or a relative path resolved from an unexpected working directory. |
| `Login failed` | Bad credentials. Special characters in a password may need shell quoting — use single quotes. |

### HTTP errors

| Code | Meaning | Fix |
|---|---|---|
| 400 | Malformed request. In S3 import: duplicate basenames, or no valid keys. | Check for basename collisions across your key list. |
| 401 | Not authenticated, or session expired. | Log in again. Sessions last 7 days. |
| 402 | Insufficient credits. The response reports required and remaining. | Reduce the job, or request more credits. |
| 403 | The job belongs to a different account. | Confirm you are authenticating as the account that created it. |

### The job fails partway

Job status goes to `ERROR` with a message the script prints. Common causes:

- **S3 copy failed** during import — usually the Archivault account lacks read
  permission on your bucket, or a key does not exist.
- **PDF processing failed** — a corrupt or password-protected PDF.
- **A processing stage failed** — provide the job ID to info@archivault.ai, which
  lets support locate the CloudWatch logs for the failing Lambda.

### The job succeeded but the output is wrong

Nearly always a silently discarded option. Work through:

- Did a metadata hint fuzzy-match below threshold and get dropped? Check that the
  values in the JSON artifact match what you passed.
- Did your custom schema exceed 4,000 characters and revert to Dublin Core? Compare
  the fields present against the ones you specified.
- Is your context file a top-level JSON array, with `file` values matching image
  basenames exactly?
- Is your foliation file an array of arrays of basenames?
- Did `--transcription-instructions` get truncated at 500 characters mid-rule?

### My downloaded files have `_1` or `_2` in the name

Expected. A file of that name already existed in `--out-dir`, so the script added a
suffix instead of overwriting it — usually because you reran a job under the same
title. Use a distinct `--title` or a fresh `--out-dir` per run. See
[section 10](#10-what-you-get-back).

### My downloaded files are named `result.json` and `report.md`

You are on a version of `submit_job.py` from before artifacts were named after the
job title. Pull the current version from this repository. Note that the old behavior
overwrote output when successive jobs shared an `--out-dir`, so check whether any
earlier results were lost.

### The job seems stuck

Long jobs legitimately take a while; the script polls every 30 seconds. Status
progresses roughly:

```
IMPORTING → PENDING → DERIVING → ENQUEUEING → SUBMITTED
  → CAPTIONING → FOLIATING → ANALYZING_LAYOUT → TRANSCRIBING
  → AGGREGATING → COMPLETED
```

Not every job passes through every stage — the sequence depends on your steps. If the
status is advancing, it is working. If it has not moved in a long while, note the job
ID and the stage it stopped at before contacting support.

`DERIVING` is the stage to expect a long pause on, and it is worth knowing why: a large
PDF is split into chunks that render in parallel, and the job waits for all of them. The
status response carries a `progress` object during this stage with `completed` and `total`
chunk counts, so you can tell a slow job from a stalled one.

---

## 14. Writing your own client

`submit_job.py` is a reference implementation, not a required one. The API is plain
HTTP and JSON, and the sequence is five calls. Reimplementing it in another language,
or embedding it in an existing workflow, is straightforward.

### The sequence

1. **`POST /auth/login`** with `{email, password}`. Returns `token` and
   `creditsRemaining`. The token goes in `Authorization: Bearer <token>` on every
   subsequent request and is valid for 7 days.

2. **`POST /presign`** with `job_title`, `filenames`, `steps`, `country`, `state`,
   `description`, and the `metadata` object. Returns `jobId`, `presignedUrls`
   (`[{filename, url, key}]`), and `pdf_count`.

   For S3 import, include `source_bucket` and put your keys in `filenames`. You get
   back an empty URL list and `status: "IMPORTING"`; poll `GET /jobs/{jobId}` until
   the status leaves `IMPORTING`.

3. **`PUT`** each file to its presigned URL with the correct `Content-Type`. These
   go directly to S3, not through the API. URLs expire after one hour. Retry 5xx
   responses with backoff; treat 4xx as fatal.

4. **`POST /pdf`** with `{jobId}` if `pdf_count > 0`, then poll `GET /jobs/{jobId}`
   until the status is `ENQUEUEING`. Expect this to take a while on large PDFs: they
   are split into chunks and rendered in parallel, and the `progress` object reports
   `completed`/`total` chunks while it runs.

5. **`POST /jobs`** with `{jobId, steps}`. This is where credits are checked and
   deducted.

6. **`GET /jobs/{jobId}`** on an interval until `COMPLETED` or `ERROR`. On success
   the response carries an `artifacts` object with presigned download URLs.

### Implementation notes

The `metadata` object assembled at `submit_job.py:510` is the complete, current
payload shape — copy its structure rather than reconstructing it from prose.

`artifacts` contains a `viewer_eligible` boolean alongside the artifact entries, so
iterate defensively: check that each value is an object containing `presigned_url`
before treating it as a download. `submit_job.py:371` does exactly this.

**Read the filename off the response, not off the S3 key.** Every artifact lives at
a fixed key — `uploads/{jobId}/result.json`, `report.md`, `tables.zip` — so a client
that names downloads from `s3_key` will write `result.json` for every job and
silently overwrite previous output. The real filename is in the `Content-Disposition`
header on the presigned GET response:

```
Content-Disposition: attachment; filename="Ramirez box 3.json"
```

Prefer that header, and fall back to `{job_title or jobId}` plus the appropriate
suffix (`.json`, `.md`, `_tables.zip`) when it is absent. `download_artifacts()` at
`submit_job.py:363` implements this, including filename sanitization — worth copying,
since job titles are free text and can contain path separators or characters that are
illegal in filenames.

Note that `GET /jobs/{jobId}` also returns `job_title`, already truncated to 32
characters. That is the value the aggregator named the artifacts after, so use it in
preference to the title you submitted.

The authoritative validator for every metadata field, vocabulary list, and model set
is `validate_metadata()` in
[`web-demo/presign_lambda/presign.py`](web-demo/presign_lambda/presign.py). When this
guide and that function disagree, the function is correct. Note that it accepts
`color_format`, `orientation`, and `image_quality_notes` — documented in
[`job_metadata_reference.md`](job_metadata_reference.md) — which `submit_job.py` does
not currently expose as flags.

Because validation is permissive by design, a direct client will not be told when a
field is rejected. Log the `metadata` block echoed back by `GET /jobs/{jobId}` and
compare it against what you sent. That diff is the only reliable way to catch a
dropped field.

---

## Reference: full example

A realistic invocation using most of what this guide covers:

```bash
python submit_job.py --email you@example.org --password "$ARCHIVAULT_PASSWORD" --dir ./ramirez_box3 --title "Ramirez box 3" --description "Business correspondence, 1890-1910, Havana and Veracruz. Water damage in later folders." --country Cuba --state Havana --steps foliate transcribe ner metadata --writing-style handwritten --language spanish --time-period 19th_century_or_earlier --layout-structure free_form --non-textual-elements stamps_or_seals --transcription-model gemini-3.1-pro-preview --metadata-model gemini-3.1-pro-preview --transcription-instructions "Transcribe crossed-out text in [brackets]. Preserve original paragraph breaks." --context-file ./accession_notes.json --additional-context-modules transcription metadata --metadata-schema ./ssda_schema.json --out-dir ./results/box3
```

---

## Where to go next

- [`api_documentation.md`](api_documentation.md) — endpoint reference
- [`job_metadata_reference.md`](job_metadata_reference.md) — every metadata field in detail
- [`architecture.md`](architecture.md) — how the pipeline works internally
- [`CODE_WALKTHROUGH.md`](CODE_WALKTHROUGH.md) — Lambda-by-Lambda code tour

Questions, credit requests, and bucket-access setup: info@archivault.ai
