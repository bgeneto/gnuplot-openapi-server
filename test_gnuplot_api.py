from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import main
from main import app


class FakeGnuplot:
    instances: list["FakeGnuplot"] = []
    output_bytes = b"fake gnuplot image"

    def __init__(self, log: bool = False):
        self.log = log
        self.settings = {}
        self.commands: list[str] = []
        self.operations: list[tuple[str, tuple, dict]] = []
        self.output_dirty = False
        FakeGnuplot.instances.append(self)

    def set(self, **settings):
        self.settings.update(settings)

    def unset(self, *items):
        self.operations.append(("unset", items, {}))

    def cmd(self, *commands):
        self.commands.extend(commands)
        if any(command == "unset output" for command in commands):
            self._write_output()
            self.output_dirty = False
            return

        if any("plot" in command or command.startswith("load ") for command in commands):
            self.output_dirty = True

    def plot(self, *items, **settings):
        self.operations.append(("plot", items, settings))
        self.output_dirty = True

    def splot(self, *items, **settings):
        self.operations.append(("splot", items, settings))
        self.output_dirty = True

    def plot_data(self, data, *items, **settings):
        self.operations.append(("plot_data", (data, *items), settings))
        self.output_dirty = True

    def splot_data(self, data, *items, **settings):
        self.operations.append(("splot_data", (data, *items), settings))
        self.output_dirty = True

    def _write_output(self):
        if not self.output_dirty:
            return

        output = self.settings.get("output")
        if not output:
            return

        output_path = Path(_unquote_gnuplot_string(output))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(self.output_bytes)


def _unquote_gnuplot_string(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


@pytest.fixture(autouse=True)
def isolated_gnuplot_runtime(monkeypatch, tmp_path):
    FakeGnuplot.instances = []
    FakeGnuplot.output_bytes = b"fake gnuplot image"
    monkeypatch.setattr(main, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.delenv("GNUPLOT_PUBLIC_OUTPUT_BASE_URL", raising=False)
    monkeypatch.setattr(
        main,
        "DEFAULT_ALLOWED_ROOTS",
        [Path.cwd().resolve(), tmp_path.resolve()],
    )
    monkeypatch.setattr(
        main.GnuplotTool,
        "_import_gnuplot",
        lambda self: SimpleNamespace(Gnuplot=FakeGnuplot),
    )
    return tmp_path


async def post_json(path: str, payload: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=payload)


@pytest.mark.asyncio
async def test_health_and_openapi_schema_expose_gnuplot_tools():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        health_response = await client.get("/gnuplot/health")
        schema_response = await client.get("/openapi.json")

    assert health_response.status_code == 200
    health = health_response.json()
    assert health["success"] is True
    assert health["service"] == "gnuplot-tool-server"
    assert "pygnuplot_available" in health
    assert "gnuplot_executable" in health

    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert "default pngcairo terminal" in schema["info"]["description"]

    paths = schema["paths"]
    assert "/gnuplot/health" in paths
    for path in (
        "/gnuplot/plot_function",
        "/gnuplot/plot_file",
        "/gnuplot/splot_function",
        "/gnuplot/splot_file",
        "/gnuplot/plot_data",
        "/gnuplot/splot_data",
        "/gnuplot/multiplot",
        "/gnuplot/run_script",
        "/gnuplot/run_commands",
    ):
        assert path in paths

    assert (
        paths["/gnuplot/plot_function"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/GnuplotSuccessResponse"
    )
    assert (
        paths["/gnuplot/health"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/GnuplotHealthResponse"
    )
    assert "Use this endpoint" in paths["/gnuplot/plot_file"]["post"]["description"]

    components = schema["components"]["schemas"]
    assert "OutputInfo" in components
    assert "OperationResult" in components
    assert "GnuplotSuccessResponse" in components
    assert (
        "Open WebUI markdown"
        in components["PlotFunctionInput"]["properties"]["output"]["description"]
    )
    assert (
        "shell escapes"
        in components["PlotFunctionInput"]["properties"]["commands"]["description"]
    )
    assert (
        "GNUPLOT_PUBLIC_OUTPUT_BASE_URL"
        in components["PlotFunctionInput"]["description"]
    )


@pytest.mark.asyncio
async def test_plot_function_returns_output_metadata_and_base64():
    response = await post_json(
        "/gnuplot/plot_function",
        {
            "output": "unit-plot.png",
            "items": ['[-10:10] sin(x) title "sin(x)" with lines'],
            "settings": {"grid": ""},
            "include_image_base64": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["operation"] == "plot_function"
    assert body["output"]["filename"] == "unit-plot.png"
    assert body["output"]["mime_type"] == "image/png"
    assert body["output"]["data_uri"].startswith("data:image/png;base64,")
    assert Path(body["output"]["path"]).exists()
    assert FakeGnuplot.instances[-1].operations[-1][0] == "plot"
    assert FakeGnuplot.instances[-1].commands[-1] == "unset output"


@pytest.mark.asyncio
async def test_plot_function_rejects_zero_byte_output(monkeypatch):
    FakeGnuplot.output_bytes = b""
    monkeypatch.setattr(
        main.GnuplotTool,
        "_wait_for_output_file",
        lambda self, output_path: False,
    )

    response = await post_json(
        "/gnuplot/plot_function",
        {
            "output": "empty.png",
            "items": ['[-10:10] sin(x) title "sin(x)" with lines'],
        },
    )

    assert response.status_code == 400
    assert "output file is empty (0 bytes)" in response.json()["detail"]


@pytest.mark.asyncio
async def test_public_output_base_url_overrides_private_request_url(monkeypatch):
    monkeypatch.setenv(
        "GNUPLOT_PUBLIC_OUTPUT_BASE_URL", "https://plots.example.test/plot-outputs/"
    )

    response = await post_json(
        "/gnuplot/plot_function",
        {
            "output": "nested/unit plot.png",
            "items": ['[-10:10] sin(x) title "sin(x)" with lines'],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert (
        body["output"]["url"]
        == "https://plots.example.test/plot-outputs/nested/unit%20plot.png"
    )
    assert body["result"]["output_url"] == body["output"]["url"]


@pytest.mark.asyncio
async def test_plot_function_defaults_to_png_for_open_webui_markdown():
    response = await post_json(
        "/gnuplot/plot_function",
        {
            "items": ['[-10:10] sin(x) title "sin(x)" with lines'],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["output"]["filename"].endswith(".png")
    assert body["output"]["url"].endswith(".png")
    assert body["output"]["mime_type"] == "image/png"
    assert body["terminal"].startswith("pngcairo")
    assert 'font "DejaVu Sans,14"' in body["terminal"]
    assert "size 1600,1000" in body["terminal"]


@pytest.mark.asyncio
async def test_plot_file_writes_inline_data_and_replaces_file_placeholder():
    response = await post_json(
        "/gnuplot/plot_file",
        {
            "output": "inline.svg",
            "data": [[0, 0], [1, 1], [2, 4]],
            "data_filename": "series.dat",
            "items": ['{file} using 1:2 title "series" with linespoints'],
            "terminal": "svg",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["operation"] == "plot_file"
    assert body["output"]["mime_type"] == "image/svg+xml"
    assert Path(body["data_file"]).read_text(encoding="utf-8") == "0 0\n1 1\n2 4\n"
    assert "{file}" not in body["items"][0]
    assert "series.dat" in body["items"][0]


@pytest.mark.asyncio
async def test_plot_file_accepts_uploaded_csv_text_and_infers_separator():
    csv_content = "time,temp\n0,22.1\n1,22.8\n2,24.0\n"

    response = await post_json(
        "/gnuplot/plot_file",
        {
            "output": "uploaded-csv.png",
            "uploaded_file": {
                "filename": "measurements.csv",
                "content": csv_content,
                "mime_type": "text/csv",
            },
            "using": "1:2",
            "title": "Temperature",
            "style": "with linespoints",
        },
    )

    assert response.status_code == 200
    body = response.json()
    data_file = Path(body["data_file"])
    assert body["operation"] == "plot_file"
    assert body["data_source"] == "uploaded_file"
    assert body["uploaded_filename"] == "measurements.csv"
    assert data_file.name.endswith("-measurements.csv")
    assert data_file.read_text(encoding="utf-8") == csv_content
    assert FakeGnuplot.instances[-1].settings["datafile"] == 'separator ","'


@pytest.mark.asyncio
async def test_splot_file_accepts_uploaded_base64_dat_file():
    dat_content = b"0 0 0\n1 1 2\n"

    response = await post_json(
        "/gnuplot/splot_file",
        {
            "output": "uploaded-dat.png",
            "upload": {
                "filename": "points.dat",
                "content_base64": base64.b64encode(dat_content).decode("ascii"),
            },
            "using": "1:2:3",
            "style": "with points",
        },
    )

    assert response.status_code == 200
    body = response.json()
    data_file = Path(body["data_file"])
    assert body["operation"] == "splot_file"
    assert body["data_source"] == "uploaded_file"
    assert data_file.name.endswith("-points.dat")
    assert data_file.read_bytes() == dat_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "operation"),
    [
        (
            "/gnuplot/splot_function",
            {
                "output": "surface.png",
                "items": ['[-1:1][-1:1] x*y title "surface" with lines'],
            },
            "splot_function",
        ),
        (
            "/gnuplot/splot_file",
            {
                "output": "xyz.png",
                "data": [[0, 0, 0], [1, 1, 2]],
                "using": "1:2:3",
                "style": "with points",
            },
            "splot_file",
        ),
        (
            "/gnuplot/plot_data",
            {
                "output": "plot-data.png",
                "data": [[0, 0], [1, 1]],
                "items": ['using 1:2 title "series" with lines'],
            },
            "plot_data",
        ),
        (
            "/gnuplot/splot_data",
            {
                "output": "splot-data.png",
                "data": [[0, 0, 0], [1, 1, 2]],
                "items": ['using 1:2:3 title "series" with points'],
            },
            "splot_data",
        ),
        (
            "/gnuplot/multiplot",
            {
                "output": "multi.png",
                "layout": '2,1 title "Two Panels"',
                "panels": [
                    {
                        "kind": "plot",
                        "items": ['[-10:10] sin(x) title "sin" with lines'],
                    },
                    {
                        "kind": "plot",
                        "items": ['[-10:10] cos(x) title "cos" with lines'],
                    },
                ],
            },
            "multiplot",
        ),
    ],
)
async def test_plotting_endpoints_return_success(path, payload, operation):
    response = await post_json(path, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["operation"] == operation
    assert Path(body["output"]["path"]).exists()


@pytest.mark.asyncio
async def test_run_script_accepts_inline_safe_script():
    response = await post_json(
        "/gnuplot/run_script",
        {
            "output": "script.png",
            "script": "\n".join(
                [
                    'set title "Script Plot"',
                    "set grid",
                    'plot [-10:10] sin(x) title "sin(x)" with lines',
                ]
            ),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["operation"] == "run_script"
    assert body["script_path"].endswith(".gnu")
    assert Path(body["script_path"]).exists()


@pytest.mark.asyncio
async def test_run_commands_supports_cmd_alias_and_output_checking():
    response = await post_json(
        "/gnuplot/run_commands",
        {
            "output": "commands.png",
            "cmd": [
                'set title "Raw Commands"',
                'plot [-10:10] sin(x) title "sin(x)" with lines',
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["operation"] == "run_commands"
    assert body["commands"][-1].startswith("plot ")
    assert Path(body["output"]["path"]).exists()


@pytest.mark.asyncio
async def test_run_commands_can_skip_output_requirement():
    response = await post_json(
        "/gnuplot/run_commands",
        {
            "output": "no-render.png",
            "commands": ['set title "Metadata Only"'],
            "expect_output": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["output"] is None
    assert body["result"]["output_url"] is None


@pytest.mark.asyncio
async def test_dangerous_commands_are_rejected_by_default():
    response = await post_json(
        "/gnuplot/run_commands",
        {
            "commands": "! touch /tmp/should-not-run",
            "expect_output": False,
        },
    )

    assert response.status_code == 400
    assert "blocked gnuplot shell escape" in response.json()["detail"]


@pytest.mark.asyncio
async def test_output_paths_must_stay_under_output_directory():
    response = await post_json(
        "/gnuplot/plot_function",
        {
            "output": "../escape.png",
            "items": ['sin(x) title "sin(x)" with lines'],
        },
    )

    assert response.status_code == 400
    assert "Output path must be under one of" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["output", "terminal"])
async def test_file_plot_keeps_base_empty_text_validation(field):
    payload = {
        "output": "valid.png",
        "data": [[0, 0], [1, 1]],
        "items": ['{file} using 1:2 title "series" with lines'],
    }
    payload[field] = ""

    response = await post_json("/gnuplot/plot_file", payload)

    assert response.status_code == 422
