"""
Gnuplot OpenAPI tool server for Open-WebUI tools usage.

This is an OpenAPI Tool Server that acts like a MCP-style plotting tool for
LLMs. It wraps gnuplot through the py-gnuplot package and exposes structured
endpoints for plot, splot, multiplot, data-file plots, generated-data plots,
and .gnu script execution.

Author: bgeneto
Since: 2026-05-06
Version: 1.0.0
"""

from __future__ import annotations

import base64
import binascii
import importlib.util
import logging
import mimetypes
import os
import re
import shutil
import signal
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("gnuplot-tool")

DEFAULT_OUTPUT_DIR = Path(
    os.getenv("GNUPLOT_OUTPUT_DIR", "/tmp/images")
).resolve()
DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PUBLIC_BASE_URL_ENV = "GNUPLOT_PUBLIC_OUTPUT_BASE_URL"
DEFAULT_IMAGE_WIDTH = 1600
DEFAULT_IMAGE_HEIGHT = 1000
DEFAULT_PNG_FONT = "DejaVu Sans"
DEFAULT_PNG_FONT_SIZE = 14
MAX_UPLOADED_DATA_FILE_BYTES = 5 * 1024 * 1024
MAX_UPLOADED_DATA_FILE_BASE64_CHARS = 7 * 1024 * 1024
ALLOWED_UPLOADED_DATA_SUFFIXES = {
    ".csv",
    ".dat",
    ".data",
    ".txt",
    ".tsv",
    ".xy",
    ".xyz",
}
DEFAULT_PNG_TERMINAL = (
    f'pngcairo enhanced font "{DEFAULT_PNG_FONT},{DEFAULT_PNG_FONT_SIZE}" '
    f"size {DEFAULT_IMAGE_WIDTH},{DEFAULT_IMAGE_HEIGHT}"
)

DEFAULT_ALLOWED_ROOTS = [
    Path.cwd().resolve(),
    DEFAULT_OUTPUT_DIR,
]

app = FastAPI(
    title="Gnuplot",
    version="1.0.0",
    description=(
        "OpenAPI tool server for creating gnuplot charts. Use /gnuplot/plot_function "
        "for 2D formulas, /gnuplot/plot_file for existing files, uploaded data "
        "files, or inline table data, "
        "/gnuplot/splot_function and /gnuplot/splot_file for 3D plots, /gnuplot/multiplot "
        "for multi-panel figures, and /gnuplot/run_script or /gnuplot/run_commands only "
        "when the user asks for script/command-level control. For Open WebUI markdown, "
        "prefer PNG output: omit output or use a .png filename so the server chooses "
        "the default pngcairo terminal and returns an image/png URL under /outputs."
    ),
)

# Enable CORS (allowing all origins for Open-WebUI style tool usage)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


GnuplotScalar = str | int | float | bool | None
GnuplotOptionValue = GnuplotScalar | list[str]
DataCell = str | int | float
DataRows = list[list[DataCell]]
CommandInput = str | list[str]


class OutputInfo(BaseModel):
    """Metadata for a generated plot file served from `/outputs`."""

    path: str = Field(
        ...,
        description="Absolute server-side path to the generated output file.",
        examples=["/tmp/images/trig.png"],
    )
    filename: str = Field(
        ...,
        description="Output filename. It ends with .png by default for Open WebUI.",
        examples=["trig.png"],
    )
    url: str = Field(
        ...,
        description=(
            "HTTP URL for the generated file. Return this URL to the user, usually "
            "as markdown image syntax for PNG outputs."
        ),
        examples=["http://localhost:8000/outputs/trig.png"],
    )
    mime_type: str = Field(
        ...,
        description="MIME type inferred from the output filename.",
        examples=["image/png"],
    )
    size_bytes: int = Field(
        ...,
        description="Size of the generated output file in bytes.",
        examples=[12345],
    )
    base64: Optional[str] = Field(
        None,
        description="Base64-encoded output bytes when include_image_base64 is true.",
    )
    data_uri: Optional[str] = Field(
        None,
        description="Data URI when include_image_base64 is true.",
    )


class OperationResult(BaseModel):
    """LLM-friendly summary of the generated plot location."""

    text_output: str = Field(
        ...,
        description="Short natural-language status message for tool callers.",
        examples=[
            "Created plot_function output at http://localhost:8000/outputs/trig.png"
        ],
    )
    output_path: str = Field(
        ...,
        description="Absolute server-side output path.",
        examples=["/tmp/images/trig.png"],
    )
    output_url: Optional[str] = Field(
        None,
        description="HTTP URL to the generated output, or null when no output was expected.",
        examples=["http://localhost:8000/outputs/trig.png"],
    )


class GnuplotSuccessResponse(BaseModel):
    """
    Common success response for plotting endpoints.

    Endpoint-specific metadata such as `items`, `commands`, `data_file`,
    `script_path`, `panels`, and `terminal` is included as extra fields.
    """

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "examples": [
                {
                    "success": True,
                    "operation": "plot_function",
                    "terminal": DEFAULT_PNG_TERMINAL,
                    "items": ['[-10:10] sin(x) title "sin(x)" with lines'],
                    "output": {
                        "path": "/tmp/images/trig.png",
                        "filename": "trig.png",
                        "url": "http://localhost:8000/outputs/trig.png",
                        "mime_type": "image/png",
                        "size_bytes": 12345,
                    },
                    "result": {
                        "text_output": (
                            "Created plot_function output at "
                            "http://localhost:8000/outputs/trig.png"
                        ),
                        "output_path": "/tmp/images/trig.png",
                        "output_url": "http://localhost:8000/outputs/trig.png",
                    },
                }
            ]
        },
    )

    success: bool = Field(True, description="True when gnuplot rendered successfully.")
    operation: str = Field(
        ...,
        description="Operation name, for example plot_function, plot_file, or multiplot.",
    )
    output: Optional[OutputInfo] = Field(
        ...,
        description=(
            "Generated file metadata. This can be null only for run_commands when "
            "expect_output=false and no file was created."
        ),
    )
    result: OperationResult = Field(
        ...,
        description="Convenient text and URL fields for LLM-facing responses.",
    )


class GnuplotHealthResponse(BaseModel):
    """Health response for `/gnuplot/health`."""

    success: bool = Field(True, description="True when the server is reachable.")
    service: str = Field("gnuplot-tool-server", description="Service identifier.")
    version: str = Field(..., description="FastAPI app version.")
    output_dir: str = Field(
        ..., description="Directory where generated files are written."
    )
    pygnuplot_available: bool = Field(
        ..., description="Whether the pygnuplot Python package can be imported."
    )
    gnuplot_executable: Optional[str] = Field(
        None,
        description="Path to the native gnuplot executable, or null if not found.",
    )


class HttpErrorResponse(BaseModel):
    """Error response produced by FastAPI HTTPException for rejected tool input."""

    detail: str = Field(..., description="Human-readable error detail.")


class InternalErrorResponse(BaseModel):
    """Global unhandled-error response."""

    success: bool = Field(False, description="False for internal errors.")
    error: str = Field(..., description="Human-readable error message.")


class GnuplotBaseInput(BaseModel):
    """
    Base request model for gnuplot operations.

    Attributes:
        output: Optional output filename/path. Relative paths are written under
            the configured output directory.
        terminal: Gnuplot terminal string. If omitted, a reasonable terminal is
            selected from the output extension.
        width: Image width used by the default terminal.
        height: Image height used by the default terminal.
        settings: Extra gnuplot `set` options passed to py-gnuplot.
        unset: Gnuplot options to unset before plotting.
        commands: Optional gnuplot commands to run before plotting.
        post_commands: Optional gnuplot commands to run after plotting.
        include_image_base64: Include base64/data URI in the JSON response.
        allow_unsafe_commands: Disable command safety checks for trusted callers.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "description": (
                "Common plotting options. For Open WebUI markdown, omit output or use "
                "a .png filename so the server returns an image/png URL. Relative "
                "outputs are written under GNUPLOT_OUTPUT_DIR. Commands are checked "
                "for shell-like unsafe gnuplot constructs unless allow_unsafe_commands "
                "is true. If GNUPLOT_PUBLIC_OUTPUT_BASE_URL is configured, response "
                "URLs use that public static prefix instead of the private tool URL."
            )
        },
    )

    output: Optional[str] = Field(
        None,
        description=(
            "Optional output filename/path. Relative paths are written under "
            f"{DEFAULT_OUTPUT_DIR}. If omitted, a unique .png file is created and "
            "the default pngcairo terminal is used. For Open WebUI markdown, omit "
            "this field or choose a .png filename."
        ),
        max_length=500,
        examples=["plot.png", "reports/plot.png"],
    )
    terminal: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("terminal", "term"),
        description=(
            "Optional gnuplot terminal string, for example "
            f"'{DEFAULT_PNG_TERMINAL}'. Leave this empty "
            "for normal Open WebUI usage so PNG output is selected automatically."
        ),
        max_length=500,
        examples=[DEFAULT_PNG_TERMINAL],
    )
    width: int = Field(
        DEFAULT_IMAGE_WIDTH,
        description="Default output width used when terminal is not provided.",
        ge=100,
        le=8000,
    )
    height: int = Field(
        DEFAULT_IMAGE_HEIGHT,
        description="Default output height used when terminal is not provided.",
        ge=100,
        le=8000,
    )
    settings: Optional[dict[str, GnuplotOptionValue]] = Field(
        None,
        description=(
            "Optional gnuplot set options. Example: "
            "{'title': '\"Simple Plots\"', 'xrange': '[-10:10]', 'grid': ''}. "
            "Use quoted strings inside values when gnuplot expects a string."
        ),
        examples=[{"title": '"Simple Plots"', "xrange": "[-10:10]", "grid": ""}],
    )
    unset: Optional[list[str]] = Field(
        None,
        description="Optional gnuplot options to unset before plotting.",
        max_length=50,
    )
    commands: Optional[CommandInput] = Field(
        None,
        validation_alias=AliasChoices("commands", "cmd", "pre_commands"),
        description=(
            "Optional gnuplot commands to execute before plotting. Blocked by "
            "default if they contain shell escapes such as !, backticks, system, "
            "popen, load, or call."
        ),
        examples=[["set xzeroaxis", "set yzeroaxis"]],
    )
    post_commands: Optional[CommandInput] = Field(
        None,
        description="Optional gnuplot commands to execute after plotting.",
    )
    log: bool = Field(False, description="Enable py-gnuplot command logging.")
    timeout: Optional[int] = Field(
        None,
        description="Optional computation timeout in seconds.",
        ge=1,
        le=120,
    )
    include_image_base64: bool = Field(
        False,
        description=(
            "Include base64 image data and a data URI in the response. Usually false "
            "for Open WebUI because output.url is enough for markdown rendering."
        ),
    )
    allow_unsafe_commands: bool = Field(
        False,
        description=(
            "Allow shell-like gnuplot commands such as system/backticks/load. "
            "Use only in trusted environments."
        ),
    )

    @field_validator("output", "terminal")
    @classmethod
    def validate_base_optional_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Text fields cannot be empty")
        return value

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, settings):
        if settings is None:
            return settings

        option_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for key, value in settings.items():
            if not option_name.match(key):
                raise ValueError(
                    f"Invalid gnuplot option name '{key}'. Use names like title, xrange, grid, or style."
                )
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str) and len(item) > 1000:
                    raise ValueError(f"Value for option '{key}' is too long")
        return settings

    @field_validator("unset")
    @classmethod
    def validate_unset(cls, unset_items):
        if unset_items is None:
            return unset_items

        option_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for item in unset_items:
            if not option_name.match(item):
                raise ValueError(
                    f"Invalid unset option '{item}'. Use a simple gnuplot option name."
                )
        return unset_items


class PlotItemsInput(GnuplotBaseInput):
    """
    Input model for direct plot/splot operations.

    The `items` are passed as positional strings to py-gnuplot's plot() or
    splot() methods, matching the package examples.
    """

    items: list[str] = Field(
        ...,
        validation_alias=AliasChoices("items", "functions", "plots"),
        description=(
            "Full gnuplot plot clauses for formulas or expressions. Use this for "
            "direct mathematical functions such as sin(x), cos(x), exp(-x*x), or "
            "3D expressions in x and y. Example: "
            "['[-10:10] sin(x) title \"sin(x)\" with lines']."
        ),
        min_length=1,
        max_length=50,
        examples=[['[-10:10] sin(x) title "sin(x)" with lines']],
    )

    @field_validator("items")
    @classmethod
    def validate_items(cls, items):
        for item in items:
            if not item or not item.strip():
                raise ValueError("Plot items cannot be empty")
            if len(item) > 2000:
                raise ValueError("Plot item is too long")
        return items


class PlotFunctionInput(PlotItemsInput):
    """Input model for 2D function plots."""


class SplotFunctionInput(PlotItemsInput):
    """Input model for 3D function plots."""


class UploadedDataFile(BaseModel):
    """
    JSON-native uploaded data file for OpenAPI tool callers.

    OpenAPI LLM tools usually cannot send multipart form data reliably, so this
    model lets a caller provide a named text data file inside the JSON request.
    """

    model_config = ConfigDict(populate_by_name=True)

    filename: str = Field(
        ...,
        description=(
            "Original data filename. The server keeps only the basename, requires a "
            ".csv/.dat/.data/.txt/.tsv/.xy/.xyz suffix, and stores it under "
            "GNUPLOT_OUTPUT_DIR with a unique prefix."
        ),
        min_length=1,
        max_length=120,
        examples=["measurements.csv", "series.dat"],
    )
    content: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("content", "text", "data"),
        description=(
            "UTF-8 text content of the uploaded data file. Use this for pasted CSV, "
            "DAT, TSV, XY, or XYZ data."
        ),
        max_length=MAX_UPLOADED_DATA_FILE_BYTES,
        examples=["time,temp\n0,22.1\n1,22.8\n2,24.0\n"],
    )
    content_base64: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("content_base64", "base64"),
        description=(
            "Base64-encoded data file bytes. Use this when preserving the exact file "
            "payload is easier than sending escaped JSON text."
        ),
        max_length=MAX_UPLOADED_DATA_FILE_BASE64_CHARS,
    )
    encoding: str = Field(
        "utf-8",
        description="Text encoding used for content or to validate base64 bytes.",
        max_length=40,
        examples=["utf-8"],
    )
    mime_type: Optional[str] = Field(
        None,
        description="Optional source MIME type for documentation/debugging only.",
        max_length=120,
        examples=["text/csv"],
    )

    @field_validator("filename", "encoding", "mime_type")
    @classmethod
    def validate_uploaded_file_optional_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Uploaded file text fields cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_uploaded_file_payload(self):
        has_content = self.content is not None
        has_base64 = self.content_base64 is not None
        if has_content == has_base64:
            raise ValueError(
                "Provide exactly one of uploaded_file.content or "
                "uploaded_file.content_base64"
            )
        return self


class FilePlotInput(GnuplotBaseInput):
    """
    Input model for plotting data from a file, uploaded file, or inline data.

    If `items` is omitted, the server builds one plot clause from file_path/data,
    using, title, and style. If `items` is provided, `{file}` placeholders are
    replaced with the safely quoted data file path.
    """

    file_path: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("file_path", "file", "path"),
        description=(
            "Path to an existing data file to plot. The path must be under the "
            "current working directory, GNUPLOT_OUTPUT_DIR, or GNUPLOT_ALLOWED_ROOTS."
        ),
        max_length=500,
        examples=["data/benchmark.dat"],
    )
    data: Optional[str | DataRows] = Field(
        None,
        description=(
            "Inline data to write to a temporary data file. Accepts raw text or "
            "a list of rows. Use this when the user pasted data or the LLM generated "
            "a small table that should be plotted with ordinary file-based gnuplot syntax."
        ),
        examples=[[[0, 0], [1, 1], [2, 4], [3, 9]]],
    )
    uploaded_file: Optional[UploadedDataFile] = Field(
        None,
        validation_alias=AliasChoices("uploaded_file", "upload", "file_upload"),
        description=(
            "JSON-native uploaded .csv/.dat/.data/.txt/.tsv/.xy/.xyz file. Use this "
            "when an LLM has file contents from an attachment and should plot the "
            "file as a real gnuplot data file."
        ),
    )
    data_filename: Optional[str] = Field(
        None,
        description="Optional filename for inline data written under the output directory.",
        max_length=120,
    )
    items: Optional[list[str]] = Field(
        None,
        validation_alias=AliasChoices("items", "plots"),
        description=(
            "Optional full plot clauses. Use {file} as a placeholder for the "
            "safe data path. If omitted, the server builds one clause from using, "
            "title, and style."
        ),
        min_length=1,
        max_length=50,
        examples=[['{file} using 1:2 title "series" with linespoints']],
    )
    using: Optional[str] = Field(
        None,
        description="Optional gnuplot using clause, for example '1:2'.",
        max_length=500,
    )
    style: Optional[str] = Field(
        "with lines",
        description="Optional gnuplot style clause, for example 'with linespoints'.",
        max_length=500,
    )
    title: Optional[str] = Field(
        None,
        description="Optional plot title. It is quoted automatically if needed.",
        max_length=300,
    )
    separator: Optional[str] = Field(
        None,
        description=(
            "Optional datafile separator. Use ',' for CSV data; omit for whitespace-separated data."
        ),
        max_length=20,
        examples=[","],
    )

    @field_validator(
        "file_path", "data_filename", "using", "style", "title", "separator"
    )
    @classmethod
    def validate_file_optional_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Text fields cannot be empty")
        return value

    @field_validator("items")
    @classmethod
    def validate_items(cls, items):
        if items is None:
            return items
        for item in items:
            if not item or not item.strip():
                raise ValueError("Plot items cannot be empty")
            if len(item) > 2000:
                raise ValueError("Plot item is too long")
        return items


class PlotFileInput(FilePlotInput):
    """Input model for 2D file plots."""


class SplotFileInput(FilePlotInput):
    """Input model for 3D file plots."""


class DataPlotInput(GnuplotBaseInput):
    """
    Input model for py-gnuplot plot_data()/splot_data().

    This is useful when an LLM generates tabular data directly instead of
    referencing a pre-existing file.
    """

    data: str | DataRows = Field(
        ...,
        description=(
            "Raw data text or a list of rows to pass directly to py-gnuplot "
            "plot_data/splot_data. Use this for generated inline data when no reusable "
            "data file is needed."
        ),
        examples=[[[0, 0], [1, 1], [2, 4], [3, 9]]],
    )
    items: list[str] = Field(
        ...,
        validation_alias=AliasChoices("items", "plots"),
        description=(
            "Plot clauses without a filename, matching py-gnuplot plot_data(). "
            "Example: ['using 1:2 title \"series\" with lines']."
        ),
        min_length=1,
        max_length=50,
        examples=[['using 1:2 title "series" with linespoints']],
    )
    separator: Optional[str] = Field(
        None,
        description="Optional datafile separator passed through `set datafile separator`.",
        max_length=20,
    )

    @field_validator("items")
    @classmethod
    def validate_items(cls, items):
        for item in items:
            if not item or not item.strip():
                raise ValueError("Plot items cannot be empty")
            if len(item) > 2000:
                raise ValueError("Plot item is too long")
        return items


class MultiplotPanel(BaseModel):
    """
    One plot panel in a multiplot request.
    """

    kind: Literal["plot", "splot"] = Field(
        "plot", description="Panel type: plot for 2D or splot for 3D."
    )
    items: list[str] = Field(
        ...,
        description="Plot/splot clauses for this panel. Each panel uses ordinary gnuplot syntax.",
        min_length=1,
        max_length=50,
        examples=[['[-10:10] sin(x) title "sin(x)" with lines']],
    )
    settings: Optional[dict[str, GnuplotOptionValue]] = Field(
        None, description="Panel-specific gnuplot set options."
    )
    commands: Optional[CommandInput] = Field(
        None, description="Optional gnuplot commands before this panel."
    )

    @field_validator("items")
    @classmethod
    def validate_items(cls, items):
        for item in items:
            if not item or not item.strip():
                raise ValueError("Panel plot items cannot be empty")
            if len(item) > 2000:
                raise ValueError("Panel plot item is too long")
        return items

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, settings):
        if settings is None:
            return settings

        option_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for key, value in settings.items():
            if not option_name.match(key):
                raise ValueError(f"Invalid panel option name '{key}'")
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str) and len(item) > 1000:
                    raise ValueError(f"Value for panel option '{key}' is too long")
        return settings


class MultiplotInput(GnuplotBaseInput):
    """
    Input model for gnuplot multiplot output.
    """

    layout: Optional[str] = Field(
        None,
        description=(
            "Optional layout clause after `set multiplot layout`, for example "
            "'2,1 title \"Two Panels\"'. Use this when the user asks for multiple "
            "panels in one PNG. If omitted, plain `set multiplot` is used."
        ),
        max_length=500,
        examples=['2,1 title "Two Panels"'],
    )
    panels: list[MultiplotPanel] = Field(
        ...,
        description="List of plot/splot panels to render into one output.",
        min_length=1,
        max_length=20,
    )

    @field_validator("layout")
    @classmethod
    def validate_layout(cls, layout):
        if layout is not None and not layout.strip():
            raise ValueError("Layout cannot be empty")
        return layout


class ScriptInput(GnuplotBaseInput):
    """
    Input model for running a .gnu gnuplot script file or inline script.
    """

    script_path: Optional[str] = Field(
        None,
        validation_alias=AliasChoices(
            "script_path", "script_file", "file_path", "file"
        ),
        description=(
            "Path to a .gnu/.gp/.plt/.gplot script file under an allowed input root. "
            "Use script_path only when the user refers to an existing script file."
        ),
        max_length=500,
        examples=["scripts/example.gnu"],
    )
    script: Optional[str] = Field(
        None,
        description=(
            "Inline gnuplot script text. It will be written to a temporary .gnu file "
            "under GNUPLOT_OUTPUT_DIR first. Use for trusted script-style plotting."
        ),
        max_length=20000,
        examples=['set grid\nplot [-10:10] sin(x) title "sin(x)" with lines'],
    )
    apply_output_settings: bool = Field(
        True,
        description=(
            "Apply server terminal/output settings before loading the script. "
            "Scripts can still override them with their own set terminal/output commands."
        ),
    )

    @field_validator("script_path", "script")
    @classmethod
    def validate_script_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Script fields cannot be empty")
        return value


class CommandRunInput(GnuplotBaseInput):
    """
    Input model for running raw gnuplot commands.
    """

    commands: CommandInput = Field(
        ...,
        validation_alias=AliasChoices("commands", "cmd"),
        description=(
            "Raw gnuplot command(s) to execute after output/terminal settings are applied. "
            "Use only when higher-level endpoints are not expressive enough."
        ),
        examples=[["set grid", 'plot [-10:10] sin(x) title "sin(x)" with lines']],
    )
    expect_output: bool = Field(
        True,
        description="Require the configured output file to exist after commands run.",
    )


class GnuplotTool:
    def __init__(self):
        self.default_timeout = 20
        self.output_dir = DEFAULT_OUTPUT_DIR
        self.allowed_roots = self._load_allowed_roots()

    def _load_allowed_roots(self) -> list[Path]:
        """Return input path roots that the server is willing to read."""
        roots = list(DEFAULT_ALLOWED_ROOTS)
        extra_roots = os.getenv("GNUPLOT_ALLOWED_ROOTS", "")
        for raw_root in extra_roots.split(os.pathsep):
            if raw_root.strip():
                roots.append(Path(raw_root).expanduser().resolve())
        return roots

    def _import_gnuplot(self):
        """Import py-gnuplot lazily so OpenAPI discovery still works if missing."""
        try:
            from pygnuplot import gnuplot
        except Exception as exc:
            raise RuntimeError(
                "py-gnuplot is not available. Install it with `pip install py-gnuplot` "
                "and make sure the gnuplot executable is installed in the runtime image."
            ) from exc
        return gnuplot

    def _safe_computation(self, func, *args, timeout=None, **kwargs):
        """
        Execute plotting with a timeout to prevent long-running gnuplot jobs.
        """
        if timeout is None:
            timeout = self.default_timeout

        if sys.platform == "win32":
            return func(*args, **kwargs)

        def timeout_handler(signum, frame):
            raise TimeoutError(f"Gnuplot operation timed out after {timeout} seconds")

        original_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout)

        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            elapsed = time.time() - start_time
            logger.debug("Gnuplot operation completed in %.3f seconds", elapsed)
            return result
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, original_handler)

    def _normalize_command_list(self, commands: Optional[CommandInput]) -> list[str]:
        """Normalize command input into individual gnuplot command strings."""
        if commands is None:
            return []

        if isinstance(commands, str):
            return [line.strip() for line in commands.splitlines() if line.strip()]

        normalized = []
        for command in commands:
            if not command or not command.strip():
                continue
            normalized.append(command.strip())
        return normalized

    def _validate_gnuplot_text(
        self, text: str, kind: str = "gnuplot text", allow_unsafe: bool = False
    ) -> str:
        """
        Validate and sanitize gnuplot text.

        Gnuplot can run shell commands. The default guard blocks common shell
        escape forms while still allowing ordinary plotting expressions.
        """
        if not text or not text.strip():
            raise ValueError(f"{kind} cannot be empty")
        if len(text) > 20000:
            raise ValueError(f"{kind} is too long")

        cleaned = text.strip()
        if allow_unsafe:
            return cleaned

        dangerous_patterns = [
            (r"`", "backtick shell execution"),
            (r"(?im)^\s*!", "gnuplot shell escape"),
            (r"(?i)\bsystem\s*(\(|['\"])", "gnuplot system command"),
            (r"(?i)\bpopen\s*\(", "gnuplot popen command"),
            (r"(?i)\bload\s+['\"]", "loading additional scripts"),
            (r"(?i)\bcall\s+['\"]", "calling additional scripts"),
        ]

        for pattern, label in dangerous_patterns:
            if re.search(pattern, cleaned):
                raise ValueError(
                    f"{kind} contains blocked {label}. Set allow_unsafe_commands=true only for trusted input."
                )

        return cleaned

    def _validate_items(
        self, items: list[str], allow_unsafe: bool = False
    ) -> list[str]:
        """Validate plot/splot items before sending them to gnuplot."""
        return [
            self._validate_gnuplot_text(item, "plot item", allow_unsafe)
            for item in items
        ]

    def _validate_commands(
        self, commands: Optional[CommandInput], allow_unsafe: bool = False
    ) -> list[str]:
        """Validate command strings before executing them."""
        normalized = self._normalize_command_list(commands)
        return [
            self._validate_gnuplot_text(command, "gnuplot command", allow_unsafe)
            for command in normalized
        ]

    def _ensure_allowed_path(self, path: Path, roots: list[Path], kind: str) -> Path:
        """Ensure a path stays inside an allowed root."""
        resolved = path.expanduser().resolve()
        for root in roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue

        allowed = ", ".join(str(root) for root in roots)
        raise ValueError(f"{kind} path must be under one of: {allowed}")

    def _resolve_input_file(self, file_path: str) -> Path:
        """Resolve and validate an input file path."""
        raw_path = Path(file_path).expanduser()
        candidate = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
        resolved = self._ensure_allowed_path(candidate, self.allowed_roots, "Input")
        if not resolved.exists() or not resolved.is_file():
            raise ValueError(f"Input file not found: {file_path}")
        return resolved

    def _resolve_script_file(self, script_path: str) -> Path:
        """Resolve and validate a .gnu/.gp script file path."""
        resolved = self._resolve_input_file(script_path)
        if resolved.suffix.lower() not in {".gnu", ".gp", ".plt", ".gplot"}:
            raise ValueError(
                "Script file should have a .gnu, .gp, .plt, or .gplot extension"
            )
        return resolved

    def _resolve_output_file(
        self, output: Optional[str], default_suffix: str = ".png"
    ) -> Path:
        """Resolve and validate an output file path."""
        if output:
            raw_path = Path(output).expanduser()
            candidate = (
                raw_path if raw_path.is_absolute() else self.output_dir / raw_path
            )
            if not candidate.suffix:
                candidate = candidate.with_suffix(default_suffix)
        else:
            candidate = self.output_dir / f"gnuplot-{uuid.uuid4().hex}{default_suffix}"

        resolved = self._ensure_allowed_path(candidate, [self.output_dir], "Output")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _extension_from_terminal(self, terminal: Optional[str]) -> str:
        """Infer a file extension from a gnuplot terminal string."""
        if not terminal:
            return ".png"

        terminal_name = terminal.strip().split()[0].lower()
        terminal_map = {
            "png": ".png",
            "pngcairo": ".png",
            "svg": ".svg",
            "pdf": ".pdf",
            "pdfcairo": ".pdf",
            "postscript": ".eps",
            "eps": ".eps",
            "epscairo": ".eps",
            "gif": ".gif",
            "jpeg": ".jpg",
            "jpg": ".jpg",
            "dumb": ".txt",
            "canvas": ".html",
            "html": ".html",
            "epslatex": ".tex",
        }
        return terminal_map.get(terminal_name, ".png")

    def _default_terminal(self, output_path: Path, width: int, height: int) -> str:
        """Choose a terminal from the output extension."""
        suffix = output_path.suffix.lower()
        if suffix == ".svg":
            return f"svg enhanced size {width},{height}"
        if suffix == ".pdf":
            return "pdfcairo enhanced color"
        if suffix in {".eps", ".ps"}:
            return "postscript eps enhanced color"
        if suffix == ".gif":
            return f"gif size {width},{height}"
        if suffix in {".jpg", ".jpeg"}:
            return f"jpeg enhanced size {width},{height}"
        if suffix in {".txt", ".ascii"}:
            return f"dumb size {max(width // 10, 40)},{max(height // 20, 20)}"
        return (
            f'pngcairo enhanced font "{DEFAULT_PNG_FONT},{DEFAULT_PNG_FONT_SIZE}" '
            f"size {width},{height}"
        )

    def _quote_gnuplot_string(self, text: str) -> str:
        """Quote a string for gnuplot commands/settings."""
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _quote_file_for_plot_item(self, path: Path) -> str:
        """Quote a file path as a gnuplot plot item filename."""
        return self._quote_gnuplot_string(str(path))

    def _quote_title(self, title: str) -> str:
        """Quote a title unless the caller already supplied gnuplot quotes."""
        stripped = title.strip()
        if (stripped.startswith('"') and stripped.endswith('"')) or (
            stripped.startswith("'") and stripped.endswith("'")
        ):
            return stripped
        return self._quote_gnuplot_string(stripped)

    def _normalize_settings(
        self,
        settings: Optional[dict[str, GnuplotOptionValue]],
        output_path: Optional[Path],
        terminal: Optional[str],
        width: int,
        height: int,
        include_output_settings: bool = True,
    ) -> dict[str, GnuplotOptionValue]:
        """Build py-gnuplot settings with terminal/output defaults."""
        normalized: dict[str, GnuplotOptionValue] = dict(settings or {})

        if include_output_settings and output_path is not None:
            has_terminal = "terminal" in normalized or "term" in normalized
            has_output = "output" in normalized
            if not has_terminal:
                normalized["terminal"] = terminal or self._default_terminal(
                    output_path, width, height
                )
            if not has_output:
                normalized["output"] = self._quote_gnuplot_string(str(output_path))

        return normalized

    def _apply_context_setup(
        self,
        g: Any,
        settings: dict[str, GnuplotOptionValue],
        unset_items: Optional[list[str]],
        commands: Optional[CommandInput],
        allow_unsafe: bool,
    ) -> list[str]:
        """Apply set/unset/cmd setup and return validated command log."""
        if settings:
            g.set(**settings)

        if unset_items:
            g.unset(*unset_items)

        validated_commands = self._validate_commands(commands, allow_unsafe)
        if validated_commands:
            g.cmd(*validated_commands)

        return validated_commands

    def _wait_for_output_file(
        self, output_path: Path, timeout: float = 2.0, interval: float = 0.05
    ) -> bool:
        """Wait briefly for gnuplot terminals to flush output files."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if output_path.exists() and output_path.is_file():
                try:
                    if output_path.stat().st_size > 0:
                        return True
                except OSError:
                    pass
            time.sleep(interval)
        return False

    def _validated_output_size(self, output_path: Path) -> int:
        """Return output size, or raise when gnuplot did not create usable bytes."""
        if not output_path.exists() or not output_path.is_file():
            raise FileNotFoundError(
                f"Gnuplot completed but output file was not found: {output_path}"
            )

        size_bytes = output_path.stat().st_size
        if size_bytes <= 0:
            raise RuntimeError(
                f"Gnuplot completed but output file is empty (0 bytes): {output_path}"
            )
        return size_bytes

    def _finalize_output(
        self, g: Any, output_path: Path, expect_output: bool = True
    ) -> None:
        """
        Close gnuplot's current output target and wait for the file to appear.

        Terminals such as pngcairo may not fully write the file until `unset
        output` closes the target. Without this, the server can check too early
        and report a missing PNG even though gnuplot creates it moments later.
        """
        g.cmd("unset output")
        if expect_output:
            if not self._wait_for_output_file(output_path):
                self._validated_output_size(output_path)

    def _data_to_text(self, data: str | DataRows) -> str:
        """Convert inline data into text suitable for gnuplot."""
        if isinstance(data, str):
            if not data.strip():
                raise ValueError("Inline data cannot be empty")
            return data.strip() + "\n"

        if not data:
            raise ValueError("Inline data rows cannot be empty")

        rows = []
        for row in data:
            if not row:
                continue
            rows.append(" ".join(str(cell) for cell in row))

        if not rows:
            raise ValueError("Inline data rows cannot be empty")
        return "\n".join(rows) + "\n"

    def _safe_uploaded_data_filename(self, filename: str) -> str:
        """Return a safe data filename basename for uploaded file content."""
        name = Path(filename.replace("\\", "/")).name.strip()
        if not name or name in {".", ".."}:
            raise ValueError("Uploaded data filename is invalid")
        if any(ord(char) < 32 for char in name):
            raise ValueError("Uploaded data filename contains control characters")

        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_UPLOADED_DATA_SUFFIXES:
            allowed = ", ".join(sorted(ALLOWED_UPLOADED_DATA_SUFFIXES))
            raise ValueError(f"Uploaded data filename must end with one of: {allowed}")

        return name

    def _uploaded_data_to_bytes(self, uploaded_file: UploadedDataFile) -> bytes:
        """Decode uploaded text/base64 file content and validate that it is text data."""
        encoding = uploaded_file.encoding.strip()
        if uploaded_file.content is not None:
            if not uploaded_file.content.strip():
                raise ValueError("Uploaded data file cannot be empty")
            try:
                payload = uploaded_file.content.encode(encoding)
            except LookupError as exc:
                raise ValueError(f"Unknown uploaded data encoding: {encoding}") from exc
        else:
            try:
                payload = base64.b64decode(
                    uploaded_file.content_base64 or "", validate=True
                )
            except binascii.Error as exc:
                raise ValueError("Uploaded data file base64 content is invalid") from exc

            if not payload.strip():
                raise ValueError("Uploaded data file cannot be empty")
            try:
                payload.decode(encoding)
            except LookupError as exc:
                raise ValueError(f"Unknown uploaded data encoding: {encoding}") from exc
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"Uploaded data file is not valid {encoding} text"
                ) from exc

        if len(payload) > MAX_UPLOADED_DATA_FILE_BYTES:
            raise ValueError(
                f"Uploaded data file is too large; maximum is {MAX_UPLOADED_DATA_FILE_BYTES} bytes"
            )
        if not payload.endswith(b"\n"):
            payload += b"\n"
        return payload

    def _write_uploaded_data_file(self, uploaded_file: UploadedDataFile) -> Path:
        """Write a JSON-uploaded data file under the output directory."""
        safe_name = self._safe_uploaded_data_filename(uploaded_file.filename)
        target = self.output_dir / f"gnuplot-upload-{uuid.uuid4().hex}-{safe_name}"
        target = self._ensure_allowed_path(target, [self.output_dir], "Uploaded data")
        target.write_bytes(self._uploaded_data_to_bytes(uploaded_file))
        return target

    def _datafile_separator_for_path(
        self, data_file: Path, requested_separator: Optional[str]
    ) -> Optional[str]:
        """Choose an explicit or filename-inferred datafile separator."""
        if requested_separator is not None:
            return requested_separator
        if data_file.suffix.lower() == ".csv":
            return ","
        return None

    def _write_inline_data(
        self, data: str | DataRows, data_filename: Optional[str] = None
    ) -> Path:
        """Write inline data under the output directory and return its path."""
        if data_filename:
            name = Path(data_filename).name
            if not name:
                raise ValueError("Invalid data filename")
        else:
            name = f"gnuplot-data-{uuid.uuid4().hex}.dat"

        target = self._ensure_allowed_path(
            self.output_dir / name, [self.output_dir], "Data"
        )
        target.write_text(self._data_to_text(data), encoding="utf-8")
        return target

    def _write_inline_script(self, script: str) -> Path:
        """Write inline script text under the output directory."""
        target = self.output_dir / f"gnuplot-script-{uuid.uuid4().hex}.gnu"
        target = self._ensure_allowed_path(target, [self.output_dir], "Script")
        target.write_text(script.strip() + "\n", encoding="utf-8")
        return target

    def _build_file_items(
        self,
        data_file: Path,
        items: Optional[list[str]],
        using: Optional[str],
        style: Optional[str],
        title: Optional[str],
        allow_unsafe: bool,
        default_using: str,
    ) -> list[str]:
        """Build plot/splot items for file-based plotting."""
        quoted_file = self._quote_file_for_plot_item(data_file)

        if items:
            replaced = [
                item.replace("{file}", quoted_file).replace("$file", quoted_file)
                for item in items
            ]
            return self._validate_items(replaced, allow_unsafe)

        parts = [quoted_file]
        using_clause = using or default_using
        if using_clause:
            parts.append(f"using {using_clause}")
        if title:
            parts.append(f"title {self._quote_title(title)}")
        else:
            parts.append("notitle")
        if style:
            parts.append(style)

        return self._validate_items([" ".join(parts)], allow_unsafe)

    def _output_url(self, request: Request, relative_path: str) -> str:
        """Return the browser-facing URL for a generated output file."""
        public_base_url = os.getenv(OUTPUT_PUBLIC_BASE_URL_ENV, "").strip()
        if public_base_url:
            encoded_path = quote(relative_path, safe="/")
            return f"{public_base_url.rstrip('/')}/{encoded_path}"
        # Fallback: construct URL from request scheme/host when no StaticFiles mount exists.
        # This handles the Nginx sidecar case where the browser needs a public URL.
        try:
            return str(request.url_for("outputs", path=relative_path))
        except Exception:
            # No "outputs" route exists (StaticFiles was removed for Nginx sidecar).
            # Return a placeholder that the caller should replace with the Nginx URL.
            return f"{request.base_url}outputs/{relative_path}"

    def _format_output_response(
        self,
        output_path: Path,
        request: Request,
        include_image_base64: bool,
    ) -> dict[str, Any]:
        """Create output metadata for a generated plot."""
        size_bytes = self._validated_output_size(output_path)

        relative_path = output_path.relative_to(self.output_dir).as_posix()
        mime_type = (
            mimetypes.guess_type(output_path.name)[0] or "application/octet-stream"
        )
        output_url = self._output_url(request, relative_path)

        output_info: dict[str, Any] = {
            "path": str(output_path),
            "filename": output_path.name,
            "url": output_url,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }

        if include_image_base64:
            encoded = base64.b64encode(output_path.read_bytes()).decode("ascii")
            output_info["base64"] = encoded
            output_info["data_uri"] = f"data:{mime_type};base64,{encoded}"

        return output_info

    def _success_response(
        self,
        operation: str,
        output_path: Path,
        request: Request,
        include_image_base64: bool,
        **metadata,
    ) -> dict[str, Any]:
        """Build a standard successful JSON response."""
        output_info = self._format_output_response(
            output_path, request, include_image_base64
        )
        return {
            "success": True,
            "operation": operation,
            **metadata,
            "output": output_info,
            "result": {
                "text_output": f"Created {operation} output at {output_info['url']}",
                "output_path": output_info["path"],
                "output_url": output_info["url"],
            },
        }

    def _handle_error(
        self, operation: str, expression: Optional[str], exc: Exception
    ) -> dict[str, Any]:
        """Convert exceptions into consistent result dictionaries."""
        if isinstance(exc, TimeoutError):
            return {"success": False, "error": str(exc), "error_type": "timeout"}

        if isinstance(exc, (ValueError, FileNotFoundError)):
            logger.warning("Invalid %s input: %s", operation, str(exc))
            return {"success": False, "error": str(exc), "error_type": "input_error"}

        if isinstance(exc, RuntimeError):
            logger.error("%s runtime error: %s", operation, str(exc))
            return {"success": False, "error": str(exc), "error_type": "runtime_error"}

        error_id = uuid.uuid4()
        logger.error(
            "Error ID: %s, Error during %s: %s\n%s",
            error_id,
            operation,
            str(exc),
            traceback.format_exc(),
        )
        return {
            "success": False,
            "error": f"Error during {operation}: {str(exc)}",
            "error_type": "computation_error",
            "error_id": str(error_id),
            "expression": expression,
        }

    def plot_items(
        self,
        data: PlotItemsInput,
        request: Request,
        plot_kind: Literal["plot", "splot"],
    ) -> dict[str, Any]:
        """Render a plot or splot from direct gnuplot item strings."""
        operation = f"{plot_kind}_function"
        expression = ", ".join(data.items)
        try:
            output_path = self._resolve_output_file(
                data.output, self._extension_from_terminal(data.terminal)
            )
            settings = self._normalize_settings(
                data.settings,
                output_path,
                data.terminal,
                data.width,
                data.height,
            )
            items = self._validate_items(data.items, data.allow_unsafe_commands)
            post_commands = self._validate_commands(
                data.post_commands, data.allow_unsafe_commands
            )

            gnuplot = self._import_gnuplot()
            g = gnuplot.Gnuplot(log=data.log)

            def run():
                commands = self._apply_context_setup(
                    g, settings, data.unset, data.commands, data.allow_unsafe_commands
                )
                getattr(g, plot_kind)(*items)
                if post_commands:
                    g.cmd(*post_commands)
                self._finalize_output(g, output_path)
                return commands

            commands = self._safe_computation(run, timeout=data.timeout)
            return self._success_response(
                operation,
                output_path,
                request,
                data.include_image_base64,
                terminal=str(settings.get("terminal") or settings.get("term")),
                items=items,
                commands=commands,
            )
        except Exception as exc:
            return self._handle_error(operation, expression, exc)

    def plot_file(
        self,
        data: FilePlotInput,
        request: Request,
        plot_kind: Literal["plot", "splot"],
    ) -> dict[str, Any]:
        """Render a plot or splot from an existing, uploaded, or inline data file."""
        operation = f"{plot_kind}_file"
        try:
            data_sources = [
                data.file_path is not None,
                data.data is not None,
                data.uploaded_file is not None,
            ]
            if sum(data_sources) != 1:
                raise ValueError(
                    "Provide exactly one of file_path, inline data, or uploaded_file"
                )

            data_source = "inline_data"
            uploaded_filename = None
            if data.data is not None:
                data_file = self._write_inline_data(data.data, data.data_filename)
            elif data.uploaded_file is not None:
                data_file = self._write_uploaded_data_file(data.uploaded_file)
                data_source = "uploaded_file"
                uploaded_filename = self._safe_uploaded_data_filename(
                    data.uploaded_file.filename
                )
            elif data.file_path:
                data_file = self._resolve_input_file(data.file_path)
                data_source = "file_path"
            else:
                raise ValueError(
                    "Provide exactly one of file_path, inline data, or uploaded_file"
                )

            output_path = self._resolve_output_file(
                data.output, self._extension_from_terminal(data.terminal)
            )
            settings = self._normalize_settings(
                data.settings,
                output_path,
                data.terminal,
                data.width,
                data.height,
            )
            separator = self._datafile_separator_for_path(data_file, data.separator)
            if separator:
                settings["datafile"] = f"separator {self._quote_title(separator)}"

            default_using = "1:2:3" if plot_kind == "splot" else "1:2"
            items = self._build_file_items(
                data_file,
                data.items,
                data.using,
                data.style,
                data.title,
                data.allow_unsafe_commands,
                default_using,
            )
            post_commands = self._validate_commands(
                data.post_commands, data.allow_unsafe_commands
            )

            gnuplot = self._import_gnuplot()
            g = gnuplot.Gnuplot(log=data.log)

            def run():
                commands = self._apply_context_setup(
                    g, settings, data.unset, data.commands, data.allow_unsafe_commands
                )
                getattr(g, plot_kind)(*items)
                if post_commands:
                    g.cmd(*post_commands)
                self._finalize_output(g, output_path)
                return commands

            commands = self._safe_computation(run, timeout=data.timeout)
            return self._success_response(
                operation,
                output_path,
                request,
                data.include_image_base64,
                terminal=str(settings.get("terminal") or settings.get("term")),
                data_file=str(data_file),
                data_source=data_source,
                uploaded_filename=uploaded_filename,
                items=items,
                commands=commands,
            )
        except Exception as exc:
            return self._handle_error(operation, data.file_path, exc)

    def plot_data(
        self,
        data: DataPlotInput,
        request: Request,
        plot_kind: Literal["plot_data", "splot_data"],
    ) -> dict[str, Any]:
        """Render py-gnuplot plot_data() or splot_data() from inline data."""
        operation = plot_kind
        try:
            output_path = self._resolve_output_file(
                data.output, self._extension_from_terminal(data.terminal)
            )
            settings = self._normalize_settings(
                data.settings,
                output_path,
                data.terminal,
                data.width,
                data.height,
            )
            if data.separator:
                settings["datafile"] = f"separator {self._quote_title(data.separator)}"

            data_text = self._data_to_text(data.data)
            items = self._validate_items(data.items, data.allow_unsafe_commands)
            post_commands = self._validate_commands(
                data.post_commands, data.allow_unsafe_commands
            )

            gnuplot = self._import_gnuplot()
            g = gnuplot.Gnuplot(log=data.log)

            def run():
                commands = self._apply_context_setup(
                    g, settings, data.unset, data.commands, data.allow_unsafe_commands
                )
                getattr(g, plot_kind)(data_text, *items)
                if post_commands:
                    g.cmd(*post_commands)
                self._finalize_output(g, output_path)
                return commands

            commands = self._safe_computation(run, timeout=data.timeout)
            return self._success_response(
                operation,
                output_path,
                request,
                data.include_image_base64,
                terminal=str(settings.get("terminal") or settings.get("term")),
                items=items,
                commands=commands,
            )
        except Exception as exc:
            return self._handle_error(operation, None, exc)

    def multiplot(self, data: MultiplotInput, request: Request) -> dict[str, Any]:
        """Render a gnuplot multiplot output."""
        operation = "multiplot"
        try:
            output_path = self._resolve_output_file(
                data.output, self._extension_from_terminal(data.terminal)
            )
            settings = self._normalize_settings(
                data.settings,
                output_path,
                data.terminal,
                data.width,
                data.height,
            )
            post_commands = self._validate_commands(
                data.post_commands, data.allow_unsafe_commands
            )

            panel_metadata = []
            for panel in data.panels:
                panel_items = self._validate_items(
                    panel.items, data.allow_unsafe_commands
                )
                panel_commands = self._validate_commands(
                    panel.commands, data.allow_unsafe_commands
                )
                panel_metadata.append(
                    {
                        "kind": panel.kind,
                        "items": panel_items,
                        "settings": dict(panel.settings or {}),
                        "commands": panel_commands,
                    }
                )

            gnuplot = self._import_gnuplot()
            g = gnuplot.Gnuplot(log=data.log)

            def run():
                commands = self._apply_context_setup(
                    g, settings, data.unset, data.commands, data.allow_unsafe_commands
                )

                if data.layout:
                    layout = self._validate_gnuplot_text(
                        data.layout, "multiplot layout", data.allow_unsafe_commands
                    )
                    g.cmd(f"set multiplot layout {layout}")
                else:
                    g.set(multiplot=True)

                try:
                    for panel in panel_metadata:
                        if panel["commands"]:
                            g.cmd(*panel["commands"])
                        panel_settings = panel["settings"]
                        getattr(g, panel["kind"])(*panel["items"], **panel_settings)
                finally:
                    g.cmd("unset multiplot")

                if post_commands:
                    g.cmd(*post_commands)
                self._finalize_output(g, output_path)
                return commands

            commands = self._safe_computation(run, timeout=data.timeout)
            return self._success_response(
                operation,
                output_path,
                request,
                data.include_image_base64,
                terminal=str(settings.get("terminal") or settings.get("term")),
                layout=data.layout,
                panels=panel_metadata,
                commands=commands,
            )
        except Exception as exc:
            return self._handle_error(operation, None, exc)

    def run_script(self, data: ScriptInput, request: Request) -> dict[str, Any]:
        """Execute a .gnu/.gp script file or inline script."""
        operation = "run_script"
        try:
            if data.script_path:
                script_path = self._resolve_script_file(data.script_path)
                script_text = script_path.read_text(encoding="utf-8")
            elif data.script:
                script_text = data.script
                script_path = self._write_inline_script(script_text)
            else:
                raise ValueError("Provide either script_path or inline script")

            if not data.allow_unsafe_commands:
                self._validate_gnuplot_text(
                    script_text, "gnuplot script", data.allow_unsafe_commands
                )

            output_path = self._resolve_output_file(
                data.output, self._extension_from_terminal(data.terminal)
            )
            settings = self._normalize_settings(
                data.settings,
                output_path,
                data.terminal,
                data.width,
                data.height,
                include_output_settings=data.apply_output_settings,
            )
            post_commands = self._validate_commands(
                data.post_commands, data.allow_unsafe_commands
            )

            gnuplot = self._import_gnuplot()
            g = gnuplot.Gnuplot(log=data.log)
            load_command = f"load {self._quote_gnuplot_string(str(script_path))}"

            def run():
                commands = self._apply_context_setup(
                    g, settings, data.unset, data.commands, data.allow_unsafe_commands
                )
                g.cmd(load_command)
                if post_commands:
                    g.cmd(*post_commands)
                self._finalize_output(g, output_path)
                return commands

            commands = self._safe_computation(run, timeout=data.timeout)
            return self._success_response(
                operation,
                output_path,
                request,
                data.include_image_base64,
                terminal=str(settings.get("terminal") or settings.get("term")),
                script_path=str(script_path),
                commands=commands,
            )
        except Exception as exc:
            return self._handle_error(operation, data.script_path, exc)

    def run_commands(self, data: CommandRunInput, request: Request) -> dict[str, Any]:
        """Execute raw gnuplot commands with optional output checking."""
        operation = "run_commands"
        try:
            output_path = self._resolve_output_file(
                data.output, self._extension_from_terminal(data.terminal)
            )
            settings = self._normalize_settings(
                data.settings,
                output_path,
                data.terminal,
                data.width,
                data.height,
            )
            commands = self._validate_commands(
                data.commands, data.allow_unsafe_commands
            )
            post_commands = self._validate_commands(
                data.post_commands, data.allow_unsafe_commands
            )

            gnuplot = self._import_gnuplot()
            g = gnuplot.Gnuplot(log=data.log)

            def run():
                setup_commands = self._apply_context_setup(
                    g, settings, data.unset, None, data.allow_unsafe_commands
                )
                g.cmd(*commands)
                if post_commands:
                    g.cmd(*post_commands)
                self._finalize_output(g, output_path, expect_output=data.expect_output)
                return setup_commands

            setup_commands = self._safe_computation(run, timeout=data.timeout)
            if data.expect_output:
                return self._success_response(
                    operation,
                    output_path,
                    request,
                    data.include_image_base64,
                    terminal=str(settings.get("terminal") or settings.get("term")),
                    commands=commands,
                    setup_commands=setup_commands,
                )

            output_info = None
            if output_path.exists() and output_path.is_file():
                output_info = self._format_output_response(
                    output_path, request, data.include_image_base64
                )

            return {
                "success": True,
                "operation": operation,
                "terminal": str(settings.get("terminal") or settings.get("term")),
                "commands": commands,
                "setup_commands": setup_commands,
                "output": output_info,
                "result": {
                    "text_output": "Executed gnuplot commands successfully",
                    "output_path": str(output_path),
                    "output_url": output_info["url"] if output_info else None,
                },
            }
        except Exception as exc:
            return self._handle_error(operation, None, exc)


# Create gnuplot router
gnuplot_router = APIRouter(prefix="/gnuplot", tags=["gnuplot"])

COMMON_ERROR_RESPONSES = {
    400: {
        "model": HttpErrorResponse,
        "description": "Bad request. The gnuplot input was unsafe, invalid, or could not render.",
    },
    500: {
        "model": InternalErrorResponse,
        "description": "Internal server error.",
    },
}


@gnuplot_router.get(
    "/health",
    response_model=GnuplotHealthResponse,
    summary="Check gnuplot tool server health",
    description=(
        "Use this endpoint to verify that the tool server is reachable, that pygnuplot "
        "is importable, and that the native gnuplot executable is visible in PATH."
    ),
    operation_id="gnuplot_health",
)
async def gnuplot_health():
    """
    Return basic health information for the gnuplot tool server.
    """
    return {
        "success": True,
        "service": "gnuplot-tool-server",
        "version": app.version,
        "output_dir": str(DEFAULT_OUTPUT_DIR),
        "pygnuplot_available": importlib.util.find_spec("pygnuplot") is not None,
        "gnuplot_executable": shutil.which("gnuplot"),
    }


@gnuplot_router.post(
    "/plot_function",
    response_model=GnuplotSuccessResponse,
    response_model_exclude_none=True,
    summary="Plot one or more 2D gnuplot function expressions",
    description=(
        "Use this endpoint when the user asks to plot 2D mathematical functions or "
        "expressions such as sin(x), polynomials, exponentials, or comparisons across "
        "one x-axis. Prefer PNG output for Open WebUI; omit output or choose a .png filename."
    ),
    operation_id="gnuplot_plot_function",
    responses={
        200: {
            "model": GnuplotSuccessResponse,
            "description": "Successful PNG/SVG/PDF/etc. plot response.",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "operation": "plot_function",
                        "terminal": DEFAULT_PNG_TERMINAL,
                        "output": {
                            "path": "/tmp/images/simple.png",
                            "filename": "simple.png",
                            "url": "http://localhost:8000/outputs/simple.png",
                            "mime_type": "image/png",
                            "size_bytes": 12345,
                        },
                        "result": {
                            "text_output": (
                                "Created plot_function output at "
                                "http://localhost:8000/outputs/simple.png"
                            ),
                            "output_path": "/tmp/images/simple.png",
                            "output_url": "http://localhost:8000/outputs/simple.png",
                        },
                    }
                }
            },
        },
        **COMMON_ERROR_RESPONSES,
    },
)
async def gnuplot_plot_function(
    request: Request,
    data: PlotFunctionInput = Body(
        ...,
        openapi_examples={
            "open_webui_default_png": {
                "summary": "Default PNG for Open WebUI markdown",
                "description": (
                    "Omit output and terminal when a markdown-renderable PNG URL is wanted."
                ),
                "value": {
                    "items": ['[-10:10] sin(x) title "sin(x)" with lines'],
                    "settings": {"grid": "", "xlabel": '"x"', "ylabel": '"f(x)"'},
                },
            },
            "simple": {
                "summary": "Plot simple trigonometric functions",
                "value": {
                    "output": "simple-functions.png",
                    "items": [
                        '[-10:10] sin(x) title "sin(x)" with lines',
                        'cos(x) title "cos(x)" with lines',
                    ],
                    "settings": {
                        "title": '"Simple Function Plot"',
                        "grid": "",
                        "key": "left top",
                    },
                },
            },
            "polynomial_roots": {
                "summary": "Plot a polynomial and visible axes",
                "value": {
                    "output": "cubic-roots.png",
                    "items": [
                        '[-1:5] x**3 - 6*x**2 + 11*x - 6 title "f(x)" with lines'
                    ],
                    "settings": {
                        "title": '"Cubic With Three Real Roots"',
                        "grid": "",
                        "xzeroaxis": "",
                        "yzeroaxis": "",
                    },
                },
            },
        },
    ),
):
    """
    Plot one or more 2D function expressions.
    """
    tool = GnuplotTool()
    result = tool.plot_items(data, request, "plot")
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@gnuplot_router.post(
    "/plot_file",
    response_model=GnuplotSuccessResponse,
    response_model_exclude_none=True,
    summary="Plot 2D data from a file, upload, or inline data",
    description=(
        "Use this endpoint when the user provides an existing data file, pasted rows, "
        "CSV-like data, an attached/uploaded .csv or .dat file, or an LLM-generated "
        "table that should be plotted as 2D data. Inline and uploaded data are "
        "written to safe temporary files before plotting."
    ),
    operation_id="gnuplot_plot_file",
    responses=COMMON_ERROR_RESPONSES,
)
async def gnuplot_plot_file(
    request: Request,
    data: PlotFileInput = Body(
        ...,
        openapi_examples={
            "inline_data": {
                "summary": "Write inline data to a temporary file and plot it",
                "value": {
                    "output": "inline-data.png",
                    "data": [[0, 0], [1, 1], [2, 4], [3, 9]],
                    "using": "1:2",
                    "title": "x squared",
                    "style": "with linespoints",
                    "settings": {"grid": "", "xlabel": '"x"', "ylabel": '"y"'},
                },
            },
            "csv_inline_data": {
                "summary": "Plot CSV-style inline data",
                "value": {
                    "output": "temperature.png",
                    "data": "time,temp\n0,22.1\n1,22.8\n2,24.0\n3,25.4\n4,24.9",
                    "separator": ",",
                    "using": "1:2",
                    "title": "Temperature",
                    "style": "with linespoints",
                    "settings": {
                        "grid": "",
                        "xlabel": '"time"',
                        "ylabel": '"Temperature C"',
                    },
                },
            },
            "uploaded_csv_file": {
                "summary": "Plot a JSON-uploaded CSV file",
                "value": {
                    "output": "uploaded-temperature.png",
                    "uploaded_file": {
                        "filename": "temperature.csv",
                        "content": "time,temp\n0,22.1\n1,22.8\n2,24.0\n3,25.4\n",
                        "mime_type": "text/csv",
                    },
                    "using": "1:2",
                    "title": "Temperature",
                    "style": "with linespoints",
                    "settings": {
                        "grid": "",
                        "xlabel": '"time"',
                        "ylabel": '"Temperature C"',
                    },
                },
            },
            "existing_file": {
                "summary": "Plot an existing data file",
                "value": {
                    "output": "file-plot.png",
                    "file_path": "data/example.dat",
                    "items": ['{file} using 1:2 title "series" with lines'],
                },
            },
        },
    ),
):
    """
    Plot 2D data from a safe existing file path or from inline data written to a file.
    """
    tool = GnuplotTool()
    result = tool.plot_file(data, request, "plot")
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@gnuplot_router.post(
    "/splot_function",
    response_model=GnuplotSuccessResponse,
    response_model_exclude_none=True,
    summary="Plot one or more 3D gnuplot function expressions",
    description=(
        "Use this endpoint when the user asks for a 3D surface or mesh from a formula "
        "in x and y. Prefer PNG output for Open WebUI markdown."
    ),
    operation_id="gnuplot_splot_function",
    responses=COMMON_ERROR_RESPONSES,
)
async def gnuplot_splot_function(
    request: Request,
    data: SplotFunctionInput = Body(
        ...,
        openapi_examples={
            "surface": {
                "summary": "Plot a 3D surface function",
                "value": {
                    "output": "surface.png",
                    "items": [
                        '[-5:5][-5:5] sin(sqrt(x*x+y*y))/sqrt(x*x+y*y) title "sinc surface"'
                    ],
                    "settings": {
                        "title": '"3D Surface"',
                        "hidden3d": "",
                        "pm3d": "",
                        "view": "60, 35",
                    },
                },
            }
        },
    ),
):
    """
    Plot one or more 3D function expressions using gnuplot splot.
    """
    tool = GnuplotTool()
    result = tool.plot_items(data, request, "splot")
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@gnuplot_router.post(
    "/splot_file",
    response_model=GnuplotSuccessResponse,
    response_model_exclude_none=True,
    summary="Plot 3D data from a file, upload, or inline data",
    description=(
        "Use this endpoint when the user provides XYZ rows, an uploaded/existing XYZ "
        "data file, or LLM-generated 3D points that should be rendered with gnuplot "
        "splot."
    ),
    operation_id="gnuplot_splot_file",
    responses=COMMON_ERROR_RESPONSES,
)
async def gnuplot_splot_file(
    request: Request,
    data: SplotFileInput = Body(
        ...,
        openapi_examples={
            "inline_grid": {
                "summary": "Write inline XYZ data to a temporary file and splot it",
                "value": {
                    "output": "xyz-surface.png",
                    "data": [[0, 0, 0], [0, 1, 1], [1, 0, 1], [1, 1, 2]],
                    "using": "1:2:3",
                    "title": "z = x + y",
                    "style": "with points pointtype 7",
                    "settings": {"grid": "", "view": "60, 35"},
                },
            },
            "uploaded_xyz_file": {
                "summary": "Plot a JSON-uploaded XYZ data file",
                "value": {
                    "output": "uploaded-xyz.png",
                    "upload": {
                        "filename": "points.dat",
                        "content": "0 0 0\n0 1 1\n1 0 1\n1 1 2\n",
                    },
                    "using": "1:2:3",
                    "title": "z = x + y",
                    "style": "with points pointtype 7",
                    "settings": {"grid": "", "view": "60, 35"},
                },
            }
        },
    ),
):
    """
    Plot 3D data from a safe existing file path or from inline data written to a file.
    """
    tool = GnuplotTool()
    result = tool.plot_file(data, request, "splot")
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@gnuplot_router.post(
    "/plot_data",
    response_model=GnuplotSuccessResponse,
    response_model_exclude_none=True,
    summary="Plot inline 2D data using py-gnuplot plot_data",
    description=(
        "Use this endpoint for generated inline 2D data when no reusable data file "
        "is needed. The plot items should omit a filename and start with clauses like "
        "`using 1:2 title ... with linespoints`."
    ),
    operation_id="gnuplot_plot_data",
    responses=COMMON_ERROR_RESPONSES,
)
async def gnuplot_plot_data(
    request: Request,
    data: DataPlotInput = Body(
        ...,
        openapi_examples={
            "plot_data": {
                "summary": "Use py-gnuplot plot_data with generated data",
                "value": {
                    "output": "plot-data.png",
                    "data": [[0, 0], [1, 1], [2, 4], [3, 9]],
                    "items": ['using 1:2 title "x squared" with linespoints'],
                    "settings": {"grid": ""},
                },
            },
            "scatter_with_trend": {
                "summary": "Plot generated points with a simple trend line",
                "value": {
                    "output": "scatter-trend.png",
                    "data": [[1, 2.1], [2, 2.9], [3, 3.7], [4, 4.2], [5, 5.1]],
                    "items": [
                        'using 1:2 title "measurements" with points pointtype 7',
                        'using 1:(0.75*$1 + 1.4) title "trend" with lines',
                    ],
                    "settings": {"grid": "", "xlabel": '"x"', "ylabel": '"y"'},
                },
            },
        },
    ),
):
    """
    Plot inline 2D data using py-gnuplot's plot_data() API.
    """
    tool = GnuplotTool()
    result = tool.plot_data(data, request, "plot_data")
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@gnuplot_router.post(
    "/splot_data",
    response_model=GnuplotSuccessResponse,
    response_model_exclude_none=True,
    summary="Plot inline 3D data using py-gnuplot splot_data",
    description=(
        "Use this endpoint for generated inline XYZ data when no reusable data file "
        "is needed. The plot items should omit a filename and usually use columns 1:2:3."
    ),
    operation_id="gnuplot_splot_data",
    responses=COMMON_ERROR_RESPONSES,
)
async def gnuplot_splot_data(
    request: Request,
    data: DataPlotInput = Body(
        ...,
        openapi_examples={
            "splot_data": {
                "summary": "Use py-gnuplot splot_data with generated XYZ data",
                "value": {
                    "output": "splot-data.png",
                    "data": [[0, 0, 0], [0, 1, 1], [1, 0, 1], [1, 1, 2]],
                    "items": ['using 1:2:3 title "z = x + y" with points'],
                    "settings": {"view": "60, 35", "grid": ""},
                },
            }
        },
    ),
):
    """
    Plot inline 3D data using py-gnuplot's splot_data() API.
    """
    tool = GnuplotTool()
    result = tool.plot_data(data, request, "splot_data")
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@gnuplot_router.post(
    "/multiplot",
    response_model=GnuplotSuccessResponse,
    response_model_exclude_none=True,
    summary="Create a gnuplot multiplot image",
    description=(
        "Use this endpoint when the user asks for multiple panels in one generated "
        "figure, such as comparing related functions above and below each other."
    ),
    operation_id="gnuplot_multiplot",
    responses=COMMON_ERROR_RESPONSES,
)
async def gnuplot_multiplot(
    request: Request,
    data: MultiplotInput = Body(
        ...,
        openapi_examples={
            "two_panels": {
                "summary": "Two stacked panels",
                "value": {
                    "output": "multiplot.png",
                    "layout": '2,1 title "Two Panels"',
                    "settings": {"grid": ""},
                    "panels": [
                        {
                            "kind": "plot",
                            "items": ['[-10:10] sin(x) title "sin(x)" with lines'],
                            "settings": {"title": '"Top Panel"'},
                        },
                        {
                            "kind": "plot",
                            "items": ['[-10:10] cos(x) title "cos(x)" with lines'],
                            "settings": {"title": '"Bottom Panel"'},
                        },
                    ],
                },
            }
        },
    ),
):
    """
    Create a multiplot output with one or more plot/splot panels.
    """
    tool = GnuplotTool()
    result = tool.multiplot(data, request)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@gnuplot_router.post(
    "/run_script",
    response_model=GnuplotSuccessResponse,
    response_model_exclude_none=True,
    summary="Run a .gnu/.gp gnuplot script file or inline script",
    description=(
        "Use this endpoint only when the user provides gnuplot script text or refers "
        "to an existing .gnu/.gp/.plt/.gplot file. Prefer higher-level plotting "
        "endpoints for ordinary function and data plots."
    ),
    operation_id="gnuplot_run_script",
    responses=COMMON_ERROR_RESPONSES,
)
async def gnuplot_run_script(
    request: Request,
    data: ScriptInput = Body(
        ...,
        openapi_examples={
            "inline_script": {
                "summary": "Run inline gnuplot script text",
                "value": {
                    "output": "script-plot.png",
                    "script": "\n".join(
                        [
                            'set title "Script Plot"',
                            "set grid",
                            'plot [-10:10] sin(x) title "sin(x)" with lines',
                        ]
                    ),
                },
            },
            "script_file": {
                "summary": "Run an existing .gnu script file",
                "value": {
                    "output": "from-script.png",
                    "script_path": "scripts/example.gnu",
                },
            },
        },
    ),
):
    """
    Run an existing .gnu/.gp script file or inline script text.
    """
    tool = GnuplotTool()
    result = tool.run_script(data, request)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@gnuplot_router.post(
    "/run_commands",
    response_model=GnuplotSuccessResponse,
    summary="Run raw gnuplot commands",
    description=(
        "Use this endpoint only when the user needs raw gnuplot command control. "
        "For normal formulas, data tables, files, surfaces, and multiplots, prefer "
        "the higher-level endpoints. Commands are checked for unsafe shell escapes "
        "unless allow_unsafe_commands=true."
    ),
    operation_id="gnuplot_run_commands",
    responses=COMMON_ERROR_RESPONSES,
)
async def gnuplot_run_commands(
    request: Request,
    data: CommandRunInput = Body(
        ...,
        openapi_examples={
            "raw_commands": {
                "summary": "Run raw plotting commands",
                "value": {
                    "output": "raw-commands.png",
                    "commands": [
                        'set title "Raw Commands"',
                        "set grid",
                        'plot [-10:10] sin(x) title "sin(x)" with lines',
                    ],
                },
            },
            "metadata_only": {
                "summary": "Run setup commands without requiring output",
                "value": {
                    "commands": ['set title "Metadata Only"'],
                    "expect_output": False,
                },
            },
        },
    ),
):
    """
    Run raw gnuplot commands after applying output/settings.
    """
    tool = GnuplotTool()
    result = tool.run_commands(data, request)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# Include gnuplot router in the app
app.include_router(gnuplot_router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s\n%s", str(exc), traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": f"Internal server error: {str(exc)}"},
    )
