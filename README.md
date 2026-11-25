# Epi-AI-Agent

A local AI assistant for epidemiological research, with a browser interface
for exploring study data, reviewing evidence, and running analyses.
Supports macOS and Linux with Python 3.12.

[Watch the demo video](https://drive.google.com/file/d/1A-N8pTOn6tKcZ_D6DLPc62EWvb2e_68E/view?usp=sharing)

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
git clone https://github.com/xutao-wang/Epi-AI-Agent.git
cd Epi-AI-Agent

uv python install 3.12
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt

cp .env.example .env
```

Add a provider API key to `.env`, or configure one when you first start the app.

## Start the app

```bash
python run_fastapi.py
```

Open <http://127.0.0.1:8080/>.

First startup asks you to choose a data folder, configure a model provider,
and acknowledge the generated Python execution warning.
To change providers later, run `python run_fastapi.py --reconfigure`.

## Study data

Install a study package to ask questions about its data. For example, use the
RePORT India Synthetic package:

```bash
python study_installer.py --study report-india-synthetic-0.5.0.tar.gz
```

The installer asks where to store study packages. Install other studies with
the same command and their archive paths, then restart the app.
For NHANES analyses, use package version 0.4.0 with Cohere or version
0.3.1 with OpenAI.

## Model providers

Configure the providers you want in `.env`:

- **OpenAI:** `OPENAI_API_KEY`.
- **Anthropic:** `ANTHROPIC_API_KEY`.
- **OpenRouter:** `OPENROUTER_API_KEY` and models registered in
  `config/custom_models.json`; start from
  [the example configuration](config/custom_models.example.json).
- **Amazon Bedrock (optional):** `BEDROCK_API_KEY` and `BEDROCK_REGION`.


For a local vLLM server, run `python run_fastapi.py --reconfigure` and choose
**Connect to a compatible endpoint**. For HPC setup, see
[the Amarel instructions](config/vllm_amarel/load_llm_amarel_instruction.md).

You need at least one API key or a local vLLM server to start the application.

## Semantic search

The default profile uses OpenAI text-embedding-3-large and `OPENAI_API_KEY`.
For semantic search with NHANES 0.4.0 and RePORT India Synthetic 0.5.0, set
`BEDROCK_API_KEY` (or `AWS_BEARER_TOKEN_BEDROCK`) and select the Cohere profile
in `.env` before restarting the app.

```dotenv
DB_RAG_EMBEDDING_PROFILE=bedrock-cohere-embed-v4
```

This profile calls Cohere Embed v4 in `us-east-1` and matches package indexes
declaring `Bedrock/cohere-embed-v4-1536`. Searches fall back to lexical
matching if the selected profile is unavailable or differs from a package's
embedding identity.

## Python analyses

Generated Python runs with your account's permissions and is not an
operating-system sandbox. Read the startup warning before enabling it.
Analysis results are staged for your review.

To add a Python package, update `requirements.txt`, stop the app, and run
`uv pip install -r requirements.txt` before restarting. See
[the execution policy](tools/python_execution_policy.md) for details.

## Optional upload checker (upload plug)

The paperclip can hand uploads to a separate checker service (a "plug") before
anything reaches a conversation. It is off by default.

### Settings

Set both in `.env` (or the environment) to turn it on:

| Variable | Meaning |
| --- | --- |
| `REPORT_AGENT_UPLOAD_PLUG_URL` | The plug's origin only, for example `http://127.0.0.1:8090`. No path, query or credentials. Plain `http` only for `127.0.0.1`, `localhost` or `::1`; otherwise `https`. |
| `REPORT_AGENT_UPLOAD_PLUG_SECRET` | Shared secret, at least 32 characters. The app refuses to start if the URL is set without it. |
| `REPORT_AGENT_UPLOAD_PLUG_TIMEOUT_SECONDS` | Server-to-server timeout, default 30. |
| `REPORT_AGENT_UPLOAD_PLUG_REQUIRED` | Optional, empty (off) by default. `1`, `true` or `yes` makes the checker mandatory: with no URL set, every upload is refused (409 `UPLOAD_PLUG_REQUIRED`) and the paperclip says "Uploads are disabled until the upload checker is configured". Any value other than `1`, `true`, `yes`, `0`, `false`, `no` or empty refuses to start. |

With no URL and the setting off, the app behaves as before and shows a
"PHI checker: off" badge next to the paperclip.

### What changes when it is on

- The paperclip accepts one `.zip` file only. Plain uploads to
  `/api/threads/{id}/attachments` are refused (409) before the body is read.
- The ZIP is shown to the plug's page in a modal (iframe). The app never stores
  or attaches the ZIP itself.
- When the plug releases the cleaned study ZIP, the browser gets only an opaque
  handle. The app pulls the ZIP from the plug server to server, opens it in
  memory (the ZIP itself is never written to disk or logged), checks it against
  the plug's manifest and stages each file under the name it was uploaded with
  (`Riverbend Heart (v2)_DATA.csv`), all or none, through the normal upload
  checks. The dictionary, forms and metadata arrive byte for byte as uploaded,
  so a PDF form arrives as a PDF.
- Files staged this way show a "Passed upload checker" badge on the composer
  chip and the message card. The model sees a `deidentified` mark on them and,
  on each dataset, the checker's per-column record in short: the method (Safe
  Harbor or Expert Determination), how many columns were kept, and each changed
  column's header, a fixed word (`NAME`, `ID`, `PARTICIPANT_ID`, `DATE`, `AGE`,
  `ZIP`, `FREE_TEXT`, `REDACTED` and so on) and what it now holds, at most 200
  per file. One fixed note explains those words, study codes, year-only or
  shifted dates, `90+` and 3-digit ZIP codes, and that the checker can miss an
  identifier, which the model must not repeat, infer from or use to re-identify
  anyone. The badge says the files came through the configured plug, not that
  no protected health information remains. The app trusts whichever plug holds
  the shared secret: a plug that does not check would still get its files
  marked. Plain uploads and files the app derives later are never marked.
- In the checker's page the uploader reviews the per-column plan, chooses Safe
  Harbor or Expert Determination and answers Keep or Remove for any column the
  checker asks about. These choices are made in the checker, not in this app.

### Contract version 4 (pinned)

Browser and plug page (`postMessage`, exact origin and source window checked
both ways):

- The app loads `{origin}/embed?thread_id={id}&contract=4`.
- Plug to app: `{type: "ready"}`, `{type: "done", thread_id, handle}`,
  `{type: "cancel"}`, `{type: "error", code}`; every message carries
  `contract: "4"`. Codes are `[A-Z_]` only; anything else becomes
  `PLUG_REJECTED`. An `error` on another contract version is shown as a
  contract mismatch.
- App to plug, after `ready`: `{type: "file", contract, thread_id, caps, file}`.
  `caps` holds this app's limits: `max_files`, `max_file_bytes`,
  `max_total_bytes`, `max_table_rows`, `max_table_columns`, `max_table_cells`.
- A handle matches `^[A-Za-z0-9_-]{16,128}$`.

App server and plug (`Authorization: Bearer {secret}`, `X-Gate-Contract: 4`,
query `thread_id`, no redirects, no proxy settings from the environment):

- `GET {url}/v1/deliveries/{handle}` returns
  `{contract: "4", archive: {size, sha256}, members: [{path, mime, size, sha256}, ...], record: {path, mime, size, sha256}}`.
  Each `path` is a role folder (`dataset`, `dictionary`, `forms` or `metadata`)
  and the uploaded file name: printable ASCII, no slash or backslash,
  starting with a letter, digit, `_` or `(`, at most 200 characters before
  `.csv`, `.tsv`, `.xlsx`, `.txt`, `.md`, `.json`, `.xml`, `.docx` or `.pdf`.
- Before the archive is fetched, the app checks every path, that file names are
  unique ignoring case across the delivery, its file count, per-file and total
  caps, and that `archive.size` is at most
  `sum(size) + 22 + sum(76 + 2 * len(path))`, the record counted as one more
  entry. `record` must be `record/columns.json`, `application/json`, 1 byte to
  2 MiB. A delivery refused here is not spent.
- `GET {url}/v1/deliveries/{handle}/archive` returns the ZIP, read up to
  `archive.size` and checked against `archive.sha256`.
- The ZIP's entries must be exactly the manifest's paths (no extra, missing,
  duplicate, directory, absolute, `..` or backslash entry), not encrypted,
  stored or deflated only, each with its manifest size and sha256, and the
  record must be the last entry.
- The record is the checker's per-column record: `contract`, `method`, `model`,
  `prompt_version`, `gate`, `request_sha256`, `statement`, `not_sent`, `unchanged_files`,
  `columns` (per column: `file`, `sheet`, `header`, `action`, `written`,
  `source`, `reason`, `cells_changed`, `note`) and `risk` (uniqueness counts per
  table). The app checks its sha256 and validates it strictly: ASCII JSON,
  exactly these keys, the closed lists of methods, actions and sources,
  non-negative whole numbers, strings of at most 1024 characters, and every
  column and count belonging to a delivered dataset (matched by name, as the
  record names files by their path in the uploaded ZIP). It is not staged as
  an attachment. Each dataset keeps its own summary with the file; only the
  method, the kept count and the changed columns reach the model, never the
  counts, reasons, notes or statement. Any failure refuses the whole delivery
  with a fixed `PLUG_REJECTED` message; a 409 is `CONTRACT_MISMATCH`.
- A delivery can be pulled once.

### Limits

- Native run only: `compose.yaml` does not pass the plug variables.
- Open the app at the exact origin the plug expects (`127.0.0.1` and
  `localhost` are different origins).
- With the plug on, images and loose files cannot be attached.
- Typed chat text is not checked by the plug.
- If staging fails after the pull, the delivery is spent: upload the ZIP again.
  The error lists the app's reason codes.
- The checker this was built for (epi-phi-gate) is pre-1.0, tested on synthetic
  data only, and not cleared for real protected health information.
