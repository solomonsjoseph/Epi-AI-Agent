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
included synthetic RePORT India package:

```bash
python study_installer.py --study report-india-synthetic-0.4.0.tar.gz
```

The installer asks where to store study packages. Install other studies with
the same command and their archive paths, then restart the app.
For NHANES analyses, use the corrected package version 0.3.1 or later.

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

Default semantic search uses OpenAI embeddings. When they are unavailable,
search falls back to lexical matching.

You need at least on API key or local vLLM server configure to start using the application.
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

With no URL, the app behaves as before and shows a "PHI checker: off" badge next
to the paperclip.

### What changes when it is on

- The paperclip accepts one `.zip` file only. Plain uploads to
  `/api/threads/{id}/attachments` are refused (409) before the body is read.
- The ZIP is shown to the plug's page in a modal (iframe). The app never stores
  or attaches the ZIP itself.
- When the plug releases the cleaned study ZIP, the browser gets only an opaque
  handle. The app pulls the ZIP from the plug server to server, opens it in
  memory (the ZIP itself is never written to disk or logged), checks it against
  the plug's manifest and stages each file under its own name
  (`dataset_1.csv`), all or none, through the normal upload checks.
- Files staged this way show a "De-identified" badge on the composer chip and
  the message card. The model sees a `deidentified` mark on them and one fixed
  sentence: the upload checker can miss some identifiers, bracketed tokens such
  as `[NAME]` or `[DATE]` stand for removed values, date columns may hold day
  offsets and ID columns hold study IDs. The badge says the files came through
  the configured plug, not that no protected health information remains.
  Plain uploads and files the app derives later are never marked.

### Contract version 2 (pinned)

Browser and plug page (`postMessage`, exact origin and source window checked
both ways):

- The app loads `{origin}/embed?thread_id={id}&contract=2`.
- Plug to app: `{type: "ready"}`, `{type: "done", thread_id, handle}`,
  `{type: "cancel"}`, `{type: "error", code}`; every message carries
  `contract: "2"`. Codes are `[A-Z_]` only; anything else becomes
  `PLUG_REJECTED`. An `error` on another contract version is shown as a
  contract mismatch.
- App to plug, after `ready`: `{type: "file", contract, thread_id, caps, file}`.
  `caps` holds this app's limits: `max_files`, `max_file_bytes`,
  `max_total_bytes`, `max_table_rows`, `max_table_columns`, `max_table_cells`.
- A handle matches `^[A-Za-z0-9_-]{16,128}$`.

App server and plug (`Authorization: Bearer {secret}`, `X-Gate-Contract: 2`,
query `thread_id`, no redirects, no proxy settings from the environment):

- `GET {url}/v1/deliveries/{handle}` returns
  `{contract: "2", archive: {size, sha256}, members: [{path, mime, size, sha256}, ...]}`.
  Each `path` is a role folder and a neutral name: `dataset/dataset_N`,
  `dictionary/dictionary_N`, `forms/form_N` or `metadata/metadata_N`, with
  `.csv`, `.tsv`, `.xlsx`, `.txt`, `.md`, `.json`, `.xml` or `.docx`.
- Before the archive is fetched, the app checks every path, that file names are
  unique ignoring case, its file count, per-file and total caps, and that
  `archive.size` is at most `sum(size) + 22 + sum(76 + 2 * len(path))`. A
  delivery refused here is not spent.
- `GET {url}/v1/deliveries/{handle}/archive` returns the ZIP, read up to
  `archive.size` and checked against `archive.sha256`.
- The ZIP's entries must be exactly the manifest's paths (no extra, missing,
  duplicate, directory, absolute, `..` or backslash entry), not encrypted,
  stored or deflated only, each with its manifest size and sha256. Any failure
  refuses the whole delivery with a fixed `PLUG_REJECTED` message; a 409 is
  `CONTRACT_MISMATCH`.
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
