# Gnuplot Tool Server

`gnuplot-server` is a FastAPI/OpenAPI tool server for Open WebUI-style tool calling. It is not a native MCP server, but it exposes schema-driven plotting endpoints that let an LLM create gnuplot charts through ordinary HTTP calls.

The architecture splits responsibilities into two containers:
- **gnuplot-server** (FastAPI): handles all API endpoints under `/gnuplot`
- **nginx** (Nginx): serves generated static files from `/tmp/images` via `/outputs/{filename}`

The server wraps `py-gnuplot`, writes generated files and temporary data/script files under `GNUPLOT_OUTPUT_DIR` (default: `/tmp/images`), and serves plot outputs back from `/outputs/{filename}` through the Nginx sidecar.

PNG is the recommended output format for Open WebUI because its markdown editor can render generated plot URLs reliably with normal image syntax. Output filenames are always made unique with a UUID; omitted outputs get unique PNG filenames and the default high-resolution `pngcairo` terminal.

## What It Provides

- 2D function plots with `/gnuplot/plot_function`
- 2D file, uploaded-file, or inline-data plots with `/gnuplot/plot_file`
- 3D function plots with `/gnuplot/splot_function`
- 3D file, uploaded-file, or inline-data plots with `/gnuplot/splot_file`
- Inline `plot_data()` and `splot_data()` requests
- Multi-panel gnuplot images with `/gnuplot/multiplot`
- Inline or file-based `.gnu`/`.gp` script execution
- Raw gnuplot command execution for trusted workflows
- Static access to generated output files through the Nginx sidecar at `/outputs`
- A health endpoint that reports Python package and `gnuplot` executable availability
- PNG-by-default output for Open WebUI markdown rendering

## Runtime Layout

| Service | Container | Port | Purpose |
| --- | --- | --- | --- |
| `gnuplot-server` | FastAPI | `8000` | API endpoints (`/gnuplot/*`) |
| `nginx` | Nginx | `80` | Static file serving (`/outputs/*`) |

**Local access (dev):**
- API: `http://localhost:8000`
- Outputs: `http://localhost:8080/outputs/{filename}`
- Docs: `http://localhost:8000/docs`
- Health: `http://localhost:8000/gnuplot/health`

**Docker network access:**
- API (Open WebUI backend): `http://gnuplot-server:8000`
- Outputs (browser): `http://gnuplot-nginx:80/outputs/{filename}`

If Open WebUI is running on the same Docker network, it should reach the API at:

```text
http://gnuplot-server:8000
```

## Requirements

- Python 3.10 or newer
- Python dependencies listed in `requirements.txt`
- The `gnuplot` executable
- Headless font/rendering packages for image output

For Debian/Ubuntu-style systems, install the native plotting pieces with:

```bash
sudo apt-get update
sudo apt-get install -y gnuplot-nox fontconfig fonts-dejavu-core libcairo2 libpango-1.0-0 libpangocairo-1.0-0
```

Install the Python dependencies locally with:

```bash
pip install -r requirements.txt
```

For test/development work:

```bash
pip install -r requirements-dev.txt
```

The Docker image installs `gnuplot-nox`, font packages, and the Cairo/Pango runtime libraries needed by the default `pngcairo` terminal before installing the Python requirements.

## Running the Server

Run locally with Uvicorn:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Run with Docker Compose for local host access:

```bash
docker compose -f compose.dev.yaml up --build -d
```

The dev compose file defaults generated image URLs to the Nginx sidecar:

```text
http://localhost:8080/outputs
```

Run with the Open WebUI Docker network setup:

```bash
GNUPLOT_PUBLIC_OUTPUT_BASE_URL=https://your-public-host.example/plot-outputs \
docker compose -f compose.prod.yaml up --build -d
```

The prod compose file requires `GNUPLOT_PUBLIC_OUTPUT_BASE_URL`, because the browser cannot render Docker-internal URLs such as `http://gnuplot-server:8000/outputs/...`.

The included Docker setup runs:
- **FastAPI** via `uvicorn main:app --host=0.0.0.0 --port=8000`
- **Nginx** via `nginx -g 'daemon off;'` serving static files on port 80

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GNUPLOT_OUTPUT_DIR` | `/tmp/images` | Directory where generated outputs and temporary uploaded/inline data/scripts are written. |
| `GNUPLOT_ALLOWED_ROOTS` | empty | Additional input-file roots, separated with the OS path separator (`:` on Linux). Existing data/script files must be under the current working directory, the output directory, or one of these extra roots. |
| `GNUPLOT_PUBLIC_OUTPUT_BASE_URL` | empty | Optional browser-facing base URL for generated images. When set, `output.url` uses this value instead of the private Docker-network URL. |

Relative `output` paths are always resolved under `GNUPLOT_OUTPUT_DIR`. Output paths outside that directory are rejected.

## Public Output URLs

Open WebUI calls this tool server from the backend using the private Docker-network URL, for example:

```text
http://gnuplot-server:8000
```

That URL is not usually reachable from the user's browser. The Nginx sidecar serves static files on the Docker network at `http://gnuplot-nginx:80/outputs/{filename}`. To render generated PNGs in Open WebUI markdown, expose the static output path through your reverse proxy and set:

```yaml
environment:
  - GNUPLOT_PUBLIC_OUTPUT_BASE_URL=https://your-public-host.example/plot-outputs
```

With Caddy on the same Docker network as `gnuplot-server` and `nginx`, expose only generated files like this:

```caddyfile
your-public-host.example {
    handle_path /plot-outputs/* {
        rewrite * /outputs{path}
        reverse_proxy http://gnuplot-nginx:80
    }

    # Keep the tool API private. Add your normal Open WebUI routes elsewhere.
    respond /gnuplot/* 404
    respond /docs 404
    respond /openapi.json 404
}
```
or something like:

```
    @images-server host your-images-server.app

    handle @images-server {
        handle_path /plot-outputs/* {
            rewrite * /outputs{path}
            reverse_proxy http://gnuplot-nginx:80 {
                import headers-proxy
                import transport-settings
            }
        }
        respond 404
    }
```

Then a generated file such as `/tmp/images/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png` is returned to the LLM as:

```text
https://your-public-host.example/plot-outputs/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png?v=123-456
```

Only `/plot-outputs/*` needs to be public. The plotting API can remain private on the Docker network.

## OpenAPI Surface

Base path: `/gnuplot`

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/gnuplot/health` | Report service version, output directory, `pygnuplot` import availability, and `gnuplot` executable path. |
| `POST` | `/gnuplot/plot_function` | Plot one or more 2D gnuplot function/item clauses. |
| `POST` | `/gnuplot/plot_file` | Plot 2D data from an existing allowed file, JSON-uploaded data file, or inline data written to a temporary file. |
| `POST` | `/gnuplot/splot_function` | Plot one or more 3D function/item clauses. |
| `POST` | `/gnuplot/splot_file` | Plot 3D data from an existing allowed file, JSON-uploaded data file, or inline data written to a temporary file. |
| `POST` | `/gnuplot/plot_data` | Pass inline data directly to `py-gnuplot` `plot_data()`. |
| `POST` | `/gnuplot/splot_data` | Pass inline data directly to `py-gnuplot` `splot_data()`. |
| `POST` | `/gnuplot/multiplot` | Create a single output using gnuplot multiplot panels. |
| `POST` | `/gnuplot/run_script` | Run an inline script or an existing `.gnu`, `.gp`, `.plt`, or `.gplot` file. |
| `POST` | `/gnuplot/run_commands` | Run raw gnuplot commands after applying output and setting defaults. |

## Common Request Fields

Most plotting endpoints inherit these fields:

| Field | Purpose |
| --- | --- |
| `output` | Optional output filename/path hint. Relative names are written under `GNUPLOT_OUTPUT_DIR`; the server appends a UUID to the final filename while preserving the requested directory, readable stem, and extension. Omitted outputs get a unique `.png` filename. |
| `terminal` or `term` | Optional gnuplot terminal string. If omitted, the server chooses one from the output extension. For Open WebUI, prefer the default high-resolution `pngcairo` behavior. |
| `width`, `height` | Default image size used by generated terminal settings. Defaults to `1600` by `1000` for PNG output. |
| `settings` | Mapping of gnuplot `set` options. Example: `{"grid": "", "title": "\"Demo\""}`. For `/gnuplot/plot_function` and 2D `/gnuplot/multiplot` panels, the server defaults to `samples=2000` unless you set `samples` yourself. If you send `grid` as an empty string, the server enables a more visible default grid style; send an explicit grid clause to override it. |
| `unset` | List of gnuplot options to unset before plotting. |
| `commands`, `cmd`, or `pre_commands` | Commands to run before the plot operation. |
| `post_commands` | Commands to run after the plot operation. |
| `timeout` | Optional timeout in seconds, from 1 to 120. |
| `include_image_base64` | Include `base64` and `data_uri` fields in the JSON response. |
| `allow_unsafe_commands` | Allows blocked shell-like gnuplot constructs. Use only for trusted input. |

By default, the server blocks common shell escape forms such as backticks, leading `!`, `system(...)`, `popen(...)`, and user-supplied `load`/`call` script commands.

## Data File Inputs

`/gnuplot/plot_file` and `/gnuplot/splot_file` accept exactly one data source:

| Field | Use case |
| --- | --- |
| `file_path`, `file`, or `path` | Plot an existing server-side file under the current working directory, `GNUPLOT_OUTPUT_DIR`, or `GNUPLOT_ALLOWED_ROOTS`. |
| `data` | Send pasted or generated data directly as raw text or a list of rows. The server writes it to a temporary `.dat` file. |
| `uploaded_file`, `upload`, or `file_upload` | Send a JSON-native file payload with a filename and text/base64 content. This is the best shape when an LLM has contents from an attached `.csv` or `.dat` file. |

Uploaded file payloads look like this:

```json
{
  "uploaded_file": {
    "filename": "measurements.csv",
    "content": "time,temp\n0,22.1\n1,22.8\n2,24.0\n",
    "mime_type": "text/csv"
  }
}
```

For exact payload preservation, use `content_base64` instead of `content`:

```json
{
  "upload": {
    "filename": "points.dat",
    "content_base64": "MCAwIDAKMSAxIDIK"
  }
}
```

Uploaded filenames must end with `.csv`, `.dat`, `.data`, `.txt`, `.tsv`, `.xy`, or `.xyz`. The server stores only the basename under `GNUPLOT_OUTPUT_DIR` with a unique prefix, so uploaded paths cannot escape the output directory. `.csv` uploads automatically set `set datafile separator ","` unless you pass `separator` explicitly.

## PNG Quality

The default PNG terminal is:

```text
pngcairo enhanced font "DejaVu Sans,18" size 1600,1000
```

This intentionally renders more pixels than a typical chat pane displays, so
browser downscaling keeps plot lines, ticks, labels, and legends smoother. The
Docker image installs DejaVu fonts so the default font is available in
headless containers.

For even sharper output, pass larger `width` and `height` values, or provide an
explicit `terminal` such as:

```json
{
  "terminal": "pngcairo enhanced font \"DejaVu Sans,18\" size 2400,1500"
}
```

Use SVG or PDF only when the caller explicitly wants a vector format.

## Output Naming

For normal LLM/Open WebUI usage, omit `output`. The server will create a unique
PNG filename such as `gnuplot-<id>.png`, which avoids browser or markdown cache
confusion when several plots are generated during one conversation.

If `output` is supplied, it is treated as a readable filename/path hint, not an
exact final name. For example, `trig_functions.png` becomes something like
`trig_functions-<uuid>.png`. The requested directory and extension are preserved,
and response URLs also include a `?v=...` cache-busting query.

## 2D Function Sampling

For `/gnuplot/plot_function`, the server now applies `set samples 2000` by default when the request does not already include `settings.samples`. This improves curve smoothness for typical LLM-generated function plots without taking control away from the caller.

The same default also applies to 2D panels in `/gnuplot/multiplot` through the request-level settings path. If you want another value, keep sending it explicitly in `settings` and it will win over the default.

This is intentionally limited to 2D function-style plots. For 3D `splot` requests, the comparable density control is usually `isosamples`, and forcing that to `2000` would be too expensive.

## Grid Visibility

If a request uses `"settings": {"grid": ""}`, the server now expands that into `front lc rgb "#7f8c8d" lw 1.5 dashtype 2` instead of bare `set grid`. That keeps the common LLM payload short while making the dashed grid easier to see in generated PNGs after browser downscaling without visually competing with the solid axes.

If you want full control, pass your own `grid` clause, for example `back lc rgb "#808080" lw 2.0`, and the server will use it as-is.

## LLM Prompt Examples

These are examples of real prompts a user can ask an LLM after this OpenAPI tool server is enabled in Open WebUI. The LLM should call the appropriate `/gnuplot` endpoint and return the generated PNG URL as markdown.

Function comparison:

```text
Plot sin(x), cos(x), and sin(x)/x from -20 to 20 on the same chart. Use a PNG output, add a grid, put the legend in the top right, and label the axes.
```

Polynomial and roots:

```text
Plot f(x) = x^3 - 6x^2 + 11x - 6 from x = -1 to 5. Highlight the x-axis and make the title "Cubic With Three Real Roots". Return the generated image in markdown.
```

Generated data:

```text
Create a small data table for monthly revenue: Jan 12000, Feb 13500, Mar 12800, Apr 16000, May 17250, Jun 18100. Plot it as a PNG line chart with points, readable labels, and a title.
```

Scatter plot with trend:

```text
Here are measurements: (1, 2.1), (2, 2.9), (3, 3.7), (4, 4.2), (5, 5.1), (6, 5.8). Plot them as points and overlay a simple fitted line so I can see the trend.
```

CSV-style inline data:

```text
Plot this CSV data as a PNG using column 1 as time and column 2 as temperature. Add a grid and label the y-axis "Temperature C":
time,temp
0,22.1
1,22.8
2,24.0
3,25.4
4,24.9
5,23.7
```

Uploaded/attached CSV file:

```text
Plot the attached CSV file as a PNG. Use column 1 for time and column 2 for temperature, add a grid, label the y-axis "Temperature C", and return the generated image URL in markdown.
```

Existing data file:

```text
Use the gnuplot tool to plot the file data/benchmark.dat. Use column 1 for input size and column 2 for runtime. Make it a PNG line chart with points, title it "Benchmark Runtime", and return the plot URL.
```

3D surface:

```text
Create a 3D PNG surface plot of z = sin(sqrt(x*x + y*y)) / sqrt(x*x + y*y) for x and y from -10 to 10. Use pm3d, hidden3d, and a readable camera angle.
```

Heat-map style surface:

```text
Plot z = exp(-(x*x + y*y) / 10) as a colored 3D surface and also make it easy to see the peak near the origin. Return the generated PNG image as markdown.
```

Multi-panel comparison:

```text
Create a two-panel PNG figure. The top panel should show sin(x) and cos(x). The bottom panel should show tan(x) clipped to y from -5 to 5. Use the same x range, -pi to pi, and add grid lines.
```

Engineering-style step response:

```text
Plot y = 1 - exp(-t/2) from t = 0 to 12 as a control-system step response. Use a PNG output, title it "First-Order Step Response", label the axes, and show the final value line at y = 1.
```

## Examples

Create a 2D function plot:

```bash
curl -X POST http://localhost:8000/gnuplot/plot_function \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [
      "[-10:10] sin(x) title \"sin(x)\" with lines",
      "cos(x) title \"cos(x)\" with lines"
    ],
    "settings": {
      "title": "\"Simple Function Plot\"",
      "grid": "",
      "key": "left top"
    }
  }'
```

Plot inline 2D data through a temporary data file:

```bash
curl -X POST http://localhost:8000/gnuplot/plot_file \
  -H 'Content-Type: application/json' \
  -d '{
    "data": [[0, 0], [1, 1], [2, 4], [3, 9]],
    "using": "1:2",
    "title": "x squared",
    "style": "with linespoints",
    "settings": {"grid": "", "xlabel": "\"x\"", "ylabel": "\"y\""}
  }'
```

Plot a JSON-uploaded CSV file:

```bash
curl -X POST http://localhost:8000/gnuplot/plot_file \
  -H 'Content-Type: application/json' \
  -d '{
    "uploaded_file": {
      "filename": "temperature.csv",
      "content": "time,temp\n0,22.1\n1,22.8\n2,24.0\n3,25.4\n",
      "mime_type": "text/csv"
    },
    "using": "1:2",
    "title": "Temperature",
    "style": "with linespoints",
    "settings": {"grid": "", "xlabel": "\"time\"", "ylabel": "\"Temperature C\""}
  }'
```

Create a 3D surface:

```bash
curl -X POST http://localhost:8000/gnuplot/splot_function \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [
      "[-5:5][-5:5] sin(sqrt(x*x+y*y))/sqrt(x*x+y*y) title \"sinc surface\""
    ],
    "settings": {"hidden3d": "", "pm3d": "", "view": "60, 35"}
  }'
```

Run inline gnuplot commands:

```bash
curl -X POST http://localhost:8000/gnuplot/run_commands \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      "set title \"Raw Commands\"",
      "set grid",
      "plot [-10:10] sin(x) title \"sin(x)\" with lines"
    ]
  }'
```

Successful plotting responses include:

```json
{
  "success": true,
  "operation": "plot_function",
  "output": {
    "path": "/tmp/images/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png",
    "filename": "gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png",
    "url": "http://localhost:8080/outputs/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png?v=123-456",
    "mime_type": "image/png",
    "size_bytes": 12345
  },
  "result": {
    "text_output": "Created plot_function output at http://localhost:8080/outputs/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png?v=123-456. Return this markdown: ![Generated plot](http://localhost:8080/outputs/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png?v=123-456)",
    "output_path": "/tmp/images/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png",
    "output_url": "http://localhost:8080/outputs/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png?v=123-456",
    "markdown": "![Generated plot](http://localhost:8080/outputs/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png?v=123-456)"
  }
}
```

If gnuplot completes but the expected output file is missing or empty, the
server returns a `400` error instead of a URL that cannot be rendered.

## Open WebUI Integration

Open WebUI can use this server directly as an OpenAPI tool server.

1. Start the container or local server.
2. In Open WebUI, open `Settings -> Tools -> Add Tool`.
3. Enter the base URL for this server.
4. Save the tool server so Open WebUI can read `/openapi.json`.
5. Enable the discovered gnuplot tools in chat.

Recommended URLs:

- If Open WebUI is running in Docker on the same network, such as the `chatwebui` network: `http://gnuplot-server:8000`
- If Open WebUI is running on the host: `http://localhost:8000`

For plots you want shown directly in markdown, use the PNG URL returned in `output.url`. In the Docker setup, this points to the Nginx sidecar:

```markdown
![Generated plot](http://gnuplot-nginx:80/outputs/gnuplot-1f2e3d4c5b6a7980abcd1234567890ef.png?v=123-456)
```

## Architecture

```
Browser                          gnuplot-server              nginx
    |                                |                         |
    |--- POST /gnuplot/* ---------->|                         |
    |   (API calls)                  |                         |
    |<-- JSON response -------------|                         |
    |                                |                         |
    |--- GET /outputs/* -------------------------------------->|
    |   (static files)               |                         |
    |<-- image/png ------------------|------------------------>|
```

- **gnuplot-server** (FastAPI on port 8000): handles all API endpoints under `/gnuplot`. Generates files into the shared `/tmp/images` directory.
- **nginx** (Nginx on port 80): serves static files from the shared `/tmp/images` directory at `/outputs/{filename}`.

Both containers share the same volume (`/tmp/images` on the host, mounted as `/tmp/images` inside containers).

## Testing

Run the test suite with:

```bash
pytest -q
```

The tests mock `py-gnuplot` so the API contract can be checked even on machines that do not have the native `gnuplot` binary installed. A real Docker build or manual endpoint call is still useful for verifying native rendering.
