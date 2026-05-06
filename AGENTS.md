# Agent Guide

This file is for LLM and AI coding agents working in this repository. Treat it as the project-specific operating manual. Keep it current whenever behavior, commands, dependencies, or deployment assumptions change.

## Project Snapshot

`gnuplot-server` is a FastAPI/OpenAPI tool server for Open WebUI-style tool calling. It is not a native MCP server. It exposes structured HTTP endpoints under `/gnuplot` so an LLM can ask gnuplot to render plots and receive output metadata and URLs.

The implementation lives mainly in `main.py` and wraps `py-gnuplot`, which in turn requires the native `gnuplot` executable at runtime. Generated files are written to `GNUPLOT_OUTPUT_DIR`, defaulting to `/tmp/gnuplot-tool-server`, and served by FastAPI static files at `/outputs/{filename}`.

Critical product constraint: keep PNG output as the default. Open WebUI's markdown editor can render returned PNG URLs directly. If the caller omits `output` and `terminal`, the server should generate a unique `.png` file using `pngcairo`.

## Repository Files

- `main.py`: FastAPI app, Pydantic request models, gnuplot wrapper, validation, static output serving, and all `/gnuplot` endpoints.
- `README.md`: User-facing setup, API, Docker, Open WebUI, and examples. Keep this aligned with `main.py`.
- `Dockerfile`: Production image. Must install native gnuplot plus Cairo/Pango/font libraries for `pngcairo`.
- `compose.dev.yaml`: Local host-access compose file with `8000:8000`.
- `compose.prod.yaml`: Open WebUI network compose file using the external `chatwebui` network.
- `requirements.txt`: Runtime Python dependencies.
- `requirements-dev.txt`: Test/development dependencies.
- `test_gnuplot_api.py`: API contract tests using a fake py-gnuplot backend.
- `.dockerignore`: Docker build context exclusions.
- `.gitignore`: Local Python/cache/editor exclusions.

## Runtime And Deployment

Default service details:

- Service/container name: `gnuplot-server`
- Internal port: `8000`
- OpenAPI schema: `/openapi.json`
- Swagger docs: `/docs`
- Health check: `/gnuplot/health`
- Output URL prefix: `/outputs`

Native packages expected in Docker:

```text
gnuplot-nox
fontconfig
fonts-dejavu-core
libcairo2
libpango-1.0-0
libpangocairo-1.0-0
```

The Dockerfile also smoke-checks `pngcairo` with:

```bash
gnuplot -e "set terminal pngcairo"
```

Do not remove Cairo/Pango packages just because `gnuplot-nox` may pull them transitively. They are explicit because PNG rendering is core to this tool's Open WebUI use case.

## Configuration

Supported environment variables:

- `GNUPLOT_OUTPUT_DIR`: directory for generated plots, inline data files, and inline script files. Defaults to `/tmp/gnuplot-tool-server`.
- `GNUPLOT_ALLOWED_ROOTS`: extra input-file roots separated by the OS path separator. On Linux this is `:`.
- `GNUPLOT_PUBLIC_OUTPUT_BASE_URL`: optional browser-facing static URL prefix for generated output files. Use this when Open WebUI calls the API through `http://gnuplot-server:8000` but the browser needs a public URL such as `https://host/gnuplot-outputs/file.png`.

Input file reads are allowed only under:

- the current working directory,
- `GNUPLOT_OUTPUT_DIR`,
- any extra roots in `GNUPLOT_ALLOWED_ROOTS`.

Output paths are always constrained to `GNUPLOT_OUTPUT_DIR`. Relative `output` values are resolved under that directory. Absolute or relative paths that escape it must remain rejected.

## API Surface

All plotting operations are under `/gnuplot`.

- `GET /gnuplot/health`
- `POST /gnuplot/plot_function`
- `POST /gnuplot/plot_file`
- `POST /gnuplot/splot_function`
- `POST /gnuplot/splot_file`
- `POST /gnuplot/plot_data`
- `POST /gnuplot/splot_data`
- `POST /gnuplot/multiplot`
- `POST /gnuplot/run_script`
- `POST /gnuplot/run_commands`

Static generated files are served from `/outputs/{relative_path}`.

Most successful plotting responses include:

- `success: true`
- `operation`
- endpoint-specific metadata such as `items`, `commands`, `data_file`, or `script_path`
- `output.path`
- `output.filename`
- `output.url`
- `output.mime_type`
- `output.size_bytes`
- optional `output.base64` and `output.data_uri` when `include_image_base64` is true
- `result.text_output`
- `result.output_path`
- `result.output_url`

Endpoint aliases are intentional. Preserve them unless changing API compatibility on purpose:

- `terminal` also accepts `term`
- `items` may also accept `functions` or `plots` on direct function endpoints
- `items` may also accept `plots` on file/data endpoints
- `commands` may also accept `cmd` or `pre_commands`
- `script_path` may also accept `script_file`, `file_path`, or `file`
- `file_path` may also accept `file` or `path`

## PNG Default Rule

This is the easiest thing to accidentally break.

When no `output` is supplied:

- create a unique `.png` filename,
- return an `image/png` response MIME type,
- use a terminal beginning with `pngcairo`.

When `output` is supplied with a `.png` suffix and no explicit terminal:

- use `pngcairo enhanced font "arial,10" size {width},{height}`.

Only produce SVG, PDF, GIF, JPG, text, or other output types when the caller explicitly requests them through `output` extension or `terminal`.

The tests include `test_plot_function_defaults_to_png_for_open_webui_markdown`. Update that test if and only if the product requirement changes.

## Security And Validation

Gnuplot can run shell commands. Keep the safety checks conservative.

By default, block these forms unless `allow_unsafe_commands=true`:

- backticks
- leading `!`
- `system(...)`
- `system "..."` or `system '...'`
- `popen(...)`
- user-supplied `load "..."` commands
- user-supplied `call "..."` commands

Script execution is special:

- `run_script` can load a server-created inline script or an allowed existing script file.
- User script text is still validated unless `allow_unsafe_commands=true`.
- Script files must end with `.gnu`, `.gp`, `.plt`, or `.gplot`.

Path safety is also part of the security model:

- never allow outputs outside `GNUPLOT_OUTPUT_DIR`,
- never allow arbitrary file reads outside allowed roots,
- keep inline data and inline script files written under `GNUPLOT_OUTPUT_DIR`.

## Implementation Notes

`GnuplotTool` is intentionally instantiated inside each endpoint. It picks up current environment values and avoids shared mutable process state.

`py-gnuplot` is imported lazily in `_import_gnuplot()`. Do not import it at module import time, because OpenAPI discovery and tests should still work on machines without native gnuplot installed.

The FastAPI app mounts `/outputs` at import time using `DEFAULT_OUTPUT_DIR`. Tests monkeypatch `DEFAULT_OUTPUT_DIR` and use direct response metadata checks; be careful if changing app mounting behavior.

Pydantic v2 is required. `requirements.txt` pins lower bounds for FastAPI and Pydantic because `main.py` uses `field_validator`, `ConfigDict`, and `AliasChoices`.

Important Pydantic gotcha: inherited validator method names can override each other. Keep validator method names unique across base and child models. For example, do not reuse `validate_optional_text` in both `GnuplotBaseInput` and `FilePlotInput`.

The default terminal mapping lives in `_default_terminal()`. The extension inference lives in `_extension_from_terminal()`. Keep these two functions consistent when adding formats.

## Testing

Install development dependencies with:

```bash
pip install -r requirements-dev.txt
```

Run the suite with:

```bash
pytest -q
```

In this workspace, the active system Python may not have the dependencies. The `linkspix` pyenv environment has been used successfully:

```bash
PYTHONDONTWRITEBYTECODE=1 PYENV_VERSION=linkspix pyenv exec pytest -q -p no:cacheprovider
```

Current expected result:

```text
16 passed
```

The tests mock `py-gnuplot` with `FakeGnuplot`. This is deliberate. API contract tests should not require the host machine to have native gnuplot installed. Use Docker builds, container smoke tests, or manual endpoint calls when you specifically need to verify real rendering.

Useful syntax check:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m py_compile main.py test_gnuplot_api.py
```

Avoid leaving `__pycache__` or `.pytest_cache` in the working tree. `.gitignore` and `.dockerignore` already exclude them.

## Docker Workflows

Local host access:

```bash
docker compose -f compose.dev.yaml up --build -d
```

`compose.dev.yaml` defaults `GNUPLOT_PUBLIC_OUTPUT_BASE_URL` to `http://localhost:8000/outputs` because it publishes port 8000 on the host.

Open WebUI network deployment:

```bash
GNUPLOT_PUBLIC_OUTPUT_BASE_URL=https://your-public-host.example/gnuplot-outputs \
docker compose -f compose.prod.yaml up --build -d
```

`compose.prod.yaml` intentionally requires `GNUPLOT_PUBLIC_OUTPUT_BASE_URL`, because Open WebUI calls the tool through the private Docker hostname while the browser needs a public static image URL.

Builds may need network access for `apt-get` and `pip`. If running in a sandboxed agent environment, request escalation rather than trying to work around network restrictions.

After a real container starts, useful smoke checks are:

```bash
curl http://localhost:8000/gnuplot/health
curl -X POST http://localhost:8000/gnuplot/plot_function \
  -H 'Content-Type: application/json' \
  -d '{"items":["[-10:10] sin(x) title \"sin(x)\" with lines"]}'
```

The plot response should include a `.png` `output.url` that can be embedded in Open WebUI markdown:

```markdown
![Generated plot](http://gnuplot-server:8000/outputs/example.png)
```

For browser-rendered markdown, prefer a public static prefix and expose only the output folder. Example Caddy route:

```caddyfile
your-public-host.example {
    handle_path /gnuplot-outputs/* {
        rewrite * /outputs{path}
        reverse_proxy http://gnuplot-server:8000
    }
}
```

Set `GNUPLOT_PUBLIC_OUTPUT_BASE_URL=https://your-public-host.example/gnuplot-outputs` on the gnuplot service so response URLs point at that route.

## Documentation Expectations

Keep `README.md` in sync with `main.py`. If adding, renaming, or removing endpoints, request fields, aliases, environment variables, Docker packages, or response fields, update both README and tests.

Do not reintroduce old math-server wording such as `/math`, ODE, or `better-math-server`. This repository is the gnuplot tool server.

Prefer examples that return PNG unless demonstrating a non-PNG format intentionally.

## Coding Style

Use the existing single-file FastAPI style unless a change clearly needs a split. Avoid broad refactors while making behavior fixes.

Prefer structured Pydantic validation over ad hoc endpoint checks. Keep error messages clear because OpenAPI tool callers often surface them directly to users.

Use `Path` operations for filesystem paths. Do not switch to string concatenation for paths.

Keep comments short and useful. Add comments for security-sensitive or non-obvious gnuplot behavior, not for ordinary assignments.

Use ASCII in source and docs unless there is a strong reason not to.

## Change Checklist

Before finishing work, check the relevant items:

- README matches the implemented API.
- PNG default still works for Open WebUI markdown.
- Dockerfile still installs gnuplot, Cairo/Pango, and fonts.
- Path safety and unsafe command blocking still work.
- Tests cover any new endpoint, alias, response shape, or bug fix.
- `pytest -q` or the pyenv equivalent passes, unless local dependencies are unavailable.
- No generated cache files are left in the working tree.
