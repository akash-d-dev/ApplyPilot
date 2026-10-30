# Launch ApplyPilot with Cursor

ApplyPilot runs from Terminal.
Cursor powers job scoring, resume tailoring, cover letters, and browser autofill.
The browser agent fills applications without submitting by default.

## 1. Open the project and activate the venv

Run this whenever you open a new Terminal window:

```bash
cd /Users/akash/Desktop/projects/ApplyPilot
source .venv/bin/activate
applypilot doctor
```

The environment prepared for this checkout already includes the Python dependencies, Cursor SDK, and Playwright Chromium.
Your Cursor key was saved through `applypilot configure-cursor`.
You still need your resume, profile, and search preferences before using the pipeline.

If setting up a fresh checkout, install inside a venv first:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[cursor,dev]' python-jobspy
python -m playwright install chromium
applypilot configure-cursor
```

Python 3.11+, Google Chrome or Chromium, and Node.js with `npx` are required for the full workflow.
Cursor's Python SDK includes its bridge; a separate Cursor CLI or TypeScript project is unnecessary.

## 2. Set up your resume and profile

```bash
applypilot init
```

Complete the wizard:

1. Enter the full path to your master resume.
   Supply a `.txt` resume for the AI stages.
   If you select a PDF, also provide its plain-text version when asked.
2. Enter your real contact details, work authorization, experience, skills, salary expectations, and availability.
3. Set target job titles, location, and search radius.
4. Enable AI features and explicitly select **`cursor`** at the provider prompt.
   The prompt defaults to Gemini, so type `cursor`.
   Your saved Cursor configuration will be reused.
5. Enable browser automation if you want autofill.
   CapSolver is optional; skip it if you do not have a key.

Run the setup check again:

```bash
applypilot doctor
```

Check that your profile, plain-text resume, Cursor provider, browser agent, Chrome, and Node.js are available.
The tier label describes installed capabilities; also check the individual profile and resume checks.

To replace your Cursor key later:

```bash
applypilot configure-cursor
```

The key prompt uses hidden input.
The default model is `composer-2.5`.
ApplyPilot explicitly sends `fast=false` for Composer 2.5, so it uses standard mode even if the account defaults to Fast.

## 3. Discover jobs and prepare applications

```bash
applypilot run
```

This discovers jobs, enriches descriptions, scores matches, tailors resumes, writes cover letters, and generates PDFs.
It does not launch the application submission stage.
Tailoring normally selects jobs scoring at least 7 out of 10.

Check progress and browse the generated dashboard:

```bash
applypilot status
applypilot dashboard
```

The dashboard is an HTML view opened in your browser.
There is no separate web server to start.

You can also run individual stages:

```bash
applypilot run discover enrich
applypilot run score tailor cover pdf
```

After changing your search preferences, rerun discovery.
Already processed jobs retain their stored pipeline state.

## 4. Autofill one application for review

```bash
applypilot apply --agent cursor --limit 1
```

Chrome opens, Cursor fills an eligible application, and the run stops without final submission.
The browser closes after the worker finishes.
Review the screenshot and execution summary under `~/.applypilot/logs/`.

Successful previews are recorded as `previewed`, not `applied`.
Later preview runs skip those jobs, while submission runs can select them.
Previews do not increase failure-attempt counts.

To target a particular job already stored in the database with a prepared resume:

```bash
applypilot apply --agent cursor --url 'PASTE_PREPARED_JOB_URL_HERE'
```

Replace the placeholder with that job's URL.
`--url` does not discover a new job or prepare its resume, and it requires one worker.

## 5. Submit when ready

```bash
applypilot apply --agent cursor --submit --limit 1
```

This fills and submits one eligible application, including a previously previewed job.
The preview browser session is not resumed.
Do not combine `--submit` with `--dry-run`.
Failed submission runs are not automatically selected for another submission attempt.

To process two applications in parallel:

```bash
applypilot apply --agent cursor --workers 2 --limit 2
applypilot apply --agent cursor --submit --workers 2 --limit 2
```

The first command previews; the second submits.
Each worker uses its own browser and upload directory.
Press Ctrl+C once to skip current jobs, or twice to stop the application run.

## Reading progress logs

Startup shows the selected model, whether submission is enabled, and saved totals from previous runs.
Composer 2.5 appears as `standard (fast=false)`.
Routine HTTP request lines are hidden; warnings and errors remain visible.

During preparation, logs look like:

```text
Model: cursor | composer-2.5 | standard (fast=false)
Mode: preparation only; no applications will be submitted
Applications this run: applied=0 | previewed=0
Scoring: processed=0/59 | scored=0 | failed=0 | START
Scoring [1/59] START | Backend Developer | Remote, India | source=linkedin
Scoring: processed=1/59 | scored=1 | failed=0 | OK | Backend Developer | Remote, India | source=linkedin | fit=8/10
```

Resume and cover-letter stages have separate `resumes_saved` and `letters_saved` counters.
Each completed job is saved before its success counter is logged, so restarting keeps that completed work.
Titles, locations, sources, and scores come from existing records; these progress messages make no extra AI requests.

Browser submission runs start with `applied=0` for that run and increment it only after a successful submission is recorded.
Previews increment `previewed` instead, and failures increment `failed`.
These shared counts include all parallel browser workers.
Saved totals are shown separately from the counters for the current run.

The `start_applypilot.command` launcher prepares documents; its applied count remains zero because it does not submit applications.
An already running process needs a restart to load logging changes.

## Files and settings

Personal data and generated files normally live in `~/.applypilot/`.

| Path | Contents |
| --- | --- |
| `~/.applypilot/.env` | Cursor key and provider/model settings |
| `~/.applypilot/profile.json` | Applicant details used in forms and prompts |
| `~/.applypilot/resume.txt` | Master resume used by AI stages |
| `~/.applypilot/searches.yaml` | Target roles, locations, and discovery preferences |
| `~/.applypilot/applypilot.db` | Jobs, scores, previews, and application results |
| `~/.applypilot/tailored_resumes/` | Generated resumes |
| `~/.applypilot/cover_letters/` | Generated cover letters |
| `~/.applypilot/logs/` | Browser run logs, summaries, and review screenshots |

Each browser run has its own folder containing `agent.log` and `summary.json`.
Successful browser runs also attempt to save `review.png`; a preview requires a successful screenshot capture.

Cursor settings in `.env` are:

```dotenv
LLM_PROVIDER=cursor
APPLY_AGENT=cursor
CURSOR_MODEL=composer-2.5
LLM_MODEL=composer-2.5
```

Keep the existing `CURSOR_API_KEY` entry saved by the configuration command.
`CURSOR_MODEL` controls the default browser model, and `LLM_MODEL` controls text stages.
Use `--model MODEL_ID` on `applypilot apply` to override the browser model for one run.
The selected model must be available to your Cursor key.

## Common fixes

| Problem | What to do |
| --- | --- |
| `applypilot: command not found` | Activate `.venv` using step 1, then retry |
| Missing profile or resume | Run `applypilot init` and provide a plain-text resume |
| No tailored resumes ready | Run `applypilot run`, then inspect scores with `applypilot status` |
| No matching jobs | Review `searches.yaml`, discovered jobs, and the minimum score |
| Cursor authentication error | Run `applypilot configure-cursor` with a working user or service-account key |
| Cursor model unavailable | Set an accessible model in `.env` or pass `--model` for the browser stage |
| Chrome not found | Install Chrome or set `CHROME_PATH` to its executable |
| `npx` missing | Install Node.js, open a new terminal, and activate the venv again |
| Playwright browser missing | Run `python -m playwright install chromium` inside the venv |
| Email verification fails | Configure the existing Gmail MCP authentication or complete the application manually |
| CAPTCHA blocks progress | Configure optional CapSolver or handle the application manually |
| Previewed job no longer selected | Use `--submit` to submit it, or `--url` to explicitly preview it again |

Before retrying a failed submission, inspect its logs and check whether the employer already received it.
An explicit `--url` run can retry an unsubmitted job.
The `--reset-failed` option resets failed records for another run and leaves successful previews intact.

For more command options:

```bash
applypilot --help
applypilot run --help
applypilot apply --help
```
