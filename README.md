# Gnuplot Tool Server

`gnuplot-server` is a FastAPI/OpenAPI tool server for Open WebUI-style tool calling. It is not a native MCP server, but it exposes schema-driven plotting endpoints that let an LLM create gnuplot charts through ordinary HTTP calls.

The server wraps `py-gnuplot`, writes generated files under `GNUPLOT_OUTPUT_DIR` (default: `/tmp/gnuplot-tool-server`), and serves them back from `/outputs/{filename}`.

PNG is the recommended output format for Open WebUI because its markdown editor can render generated plot URLs reliably with normal image syntax. Omit `output` or use a `.png` filename to get the server's default `pngcairo` terminal.

## What It Provides

- 2D function plots with `/gnuplot/plot_function`
- 2D file or inline-data plots with `/gnuplot/plot_file`
- 3D function plots with `/gnuplot/splot_function`
- 3D file or inline-data plots with `/gnuplot/splot_file`
- Inline `plot_data()` and `splot_data()` requests
- Multi-panel gnuplot images with `/gnuplot/multiplot`
- Inline or file-based `.gnu`/`.gp` script execution
- Raw gnuplot command execution for trusted workflows
- Static access to generated output files through `/outputs`
- A health endpoint that reports Python package and `gnuplot` executable availability
- PNG-by-default output for Open WebUI markdown rendering

## Runtime Layout

- Service/container name: `gnuplot-server`
- Default internal port: `8000`
- Local docs: `http://localhost:8000/docs`
- Local OpenAPI schema: `http://localhost:8000/openapi.json`
- Health check: `http://localhost:8000/gnuplot/health`
- Generated outputs: `http://localhost:8000/outputs/{filename}`

If Open WebUI is running on the same Docker network, it should usually reach this server at:

```text
http://gnuplot-server:8000
```

If you are accessing it from the host machine through the published port, use:

```text
http://localhost:8000
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

The dev compose file defaults generated image URLs to:

```text
http://localhost:8000/outputs
```

Run with the Open WebUI Docker network setup:

```bash
GNUPLOT_PUBLIC_OUTPUT_BASE_URL=https://your-public-host.example/gnuplot-outputs \
docker compose -f compose.prod.yaml up --build -d
```

The prod compose file requires `GNUPLOT_PUBLIC_OUTPUT_BASE_URL`, because the browser cannot render Docker-internal URLs such as `http://gnuplot-server:8000/outputs/...`.

The included Docker setup runs the FastAPI app as:

```bash
uvicorn main:app --host=0.0.0.0 --port=8000
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GNUPLOT_OUTPUT_DIR` | `/tmp/gnuplot-tool-server` | Directory where generated outputs and temporary inline data/scripts are written. |
| `GNUPLOT_ALLOWED_ROOTS` | empty | Additional input-file roots, separated with the OS path separator (`:` on Linux). Existing data/script files must be under the current working directory, the output directory, or one of these extra roots. |
| `GNUPLOT_PUBLIC_OUTPUT_BASE_URL` | empty | Optional browser-facing base URL for generated images. When set, `output.url` uses this value instead of the private Docker-network URL. |

Relative `output` paths are always resolved under `GNUPLOT_OUTPUT_DIR`. Output paths outside that directory are rejected.

## Public Output URLs

Open WebUI calls this tool server from the backend using the private Docker-network URL, for example:

```text
http://gnuplot-server:8000
```

That URL is not usually reachable from the user's browser. To render generated PNGs in Open WebUI markdown, expose only the static output path through your reverse proxy and set:

```yaml
environment:
  - GNUPLOT_PUBLIC_OUTPUT_BASE_URL=https://your-public-host.example/gnuplot-outputs
```

With Caddy on the same Docker network as `gnuplot-server`, expose only generated files like this:

```caddyfile
your-public-host.example {
    handle_path /gnuplot-outputs/* {
        rewrite * /outputs{path}
        reverse_proxy http://gnuplot-server:8000
    }

    # Keep the tool API private. Add your normal Open WebUI routes elsewhere.
    respond /gnuplot/* 404
    respond /docs 404
    respond /openapi.json 404
}
```

Then a generated file such as `/tmp/gnuplot-tool-server/trig.png` is returned to the LLM as:

```text
https://your-public-host.example/gnuplot-outputs/trig.png
```

Only `/gnuplot-outputs/*` needs to be public. The plotting API can remain private on the Docker network.

## OpenAPI Surface

Base path: `/gnuplot`

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/gnuplot/health` | Report service version, output directory, `pygnuplot` import availability, and `gnuplot` executable path. |
| `POST` | `/gnuplot/plot_function` | Plot one or more 2D gnuplot function/item clauses. |
| `POST` | `/gnuplot/plot_file` | Plot 2D data from an existing allowed file or inline data written to a temporary file. |
| `POST` | `/gnuplot/splot_function` | Plot one or more 3D function/item clauses. |
| `POST` | `/gnuplot/splot_file` | Plot 3D data from an existing allowed file or inline data written to a temporary file. |
| `POST` | `/gnuplot/plot_data` | Pass inline data directly to `py-gnuplot` `plot_data()`. |
| `POST` | `/gnuplot/splot_data` | Pass inline data directly to `py-gnuplot` `splot_data()`. |
| `POST` | `/gnuplot/multiplot` | Create a single output using gnuplot multiplot panels. |
| `POST` | `/gnuplot/run_script` | Run an inline script or an existing `.gnu`, `.gp`, `.plt`, or `.gplot` file. |
| `POST` | `/gnuplot/run_commands` | Run raw gnuplot commands after applying output and setting defaults. |

## Common Request Fields

Most plotting endpoints inherit these fields:

| Field | Purpose |
| --- | --- |
| `output` | Optional output filename. Relative names are written under `GNUPLOT_OUTPUT_DIR`; omitted outputs get a unique `.png` filename. |
| `terminal` or `term` | Optional gnuplot terminal string. If omitted, the server chooses one from the output extension. For Open WebUI, prefer the default `pngcairo` behavior. |
| `width`, `height` | Default image size used by generated terminal settings. |
| `settings` | Mapping of gnuplot `set` options. Example: `{"grid": "", "title": "\"Demo\""}`. |
| `unset` | List of gnuplot options to unset before plotting. |
| `commands`, `cmd`, or `pre_commands` | Commands to run before the plot operation. |
| `post_commands` | Commands to run after the plot operation. |
| `timeout` | Optional timeout in seconds, from 1 to 120. |
| `include_image_base64` | Include `base64` and `data_uri` fields in the JSON response. |
| `allow_unsafe_commands` | Allows blocked shell-like gnuplot constructs. Use only for trusted input. |

By default, the server blocks common shell escape forms such as backticks, leading `!`, `system(...)`, `popen(...)`, and user-supplied `load`/`call` script commands.

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
    "output": "trig.png",
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
    "output": "inline-data.png",
    "data": [[0, 0], [1, 1], [2, 4], [3, 9]],
    "using": "1:2",
    "title": "x squared",
    "style": "with linespoints",
    "settings": {"grid": "", "xlabel": "\"x\"", "ylabel": "\"y\""}
  }'
```

Create a 3D surface:

```bash
curl -X POST http://localhost:8000/gnuplot/splot_function \
  -H 'Content-Type: application/json' \
  -d '{
    "output": "surface.png",
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
    "output": "raw-commands.png",
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
    "path": "/tmp/gnuplot-tool-server/trig.png",
    "filename": "trig.png",
    "url": "http://localhost:8000/outputs/trig.png",
    "mime_type": "image/png",
    "size_bytes": 12345
  },
  "result": {
    "text_output": "Created plot_function output at http://localhost:8000/outputs/trig.png",
    "output_path": "/tmp/gnuplot-tool-server/trig.png",
    "output_url": "http://localhost:8000/outputs/trig.png"
  }
}
```

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

For plots you want shown directly in markdown, use the PNG URL returned in `output.url`:

```markdown
![Generated plot](http://gnuplot-server:8000/outputs/trig.png)
```

## Testing

Run the test suite with:

```bash
pytest -q
```

The tests mock `py-gnuplot` so the API contract can be checked even on machines that do not have the native `gnuplot` binary installed. A real Docker build or manual endpoint call is still useful for verifying native rendering.
