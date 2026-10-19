"""Local task builder. Export definitions without executing author-supplied code."""

import ast
import io
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from sprint.studio_runs import StudioRuns, tail, tail_text


class Draft(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    objective: str = Field(min_length=1, max_length=20000)
    minutes: int = Field(ge=1, le=120, default=60)
    actions: int = Field(ge=1, le=200, default=200)
    internet: Literal["full", "none"] = "full"
    model: str = Field(min_length=1, max_length=120, default="gpt-6-astra")
    harness: Literal["bounded-codex", "claude-code"] = "bounded-codex"
    evaluation: Literal["exact", "custom"] = "custom"
    expected: str = Field(default="42", max_length=10000)
    verifier: str = Field(default="", max_length=50000)
    files: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_files(self):
        total = 0
        if len(self.files) > 100:
            raise ValueError("Upload at most 100 text files")
        for name, content in self.files.items():
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.name
                or str(path) != name
                or "\\" in name
                or len(name) > 240
                or any(part.startswith(".") for part in path.parts)
            ):
                raise ValueError("Workspace files must have relative, non-hidden paths")
            total += len(content.encode())
        if total > 2 * 1024**2:
            raise ValueError("Workspace text exceeds 2 MiB")
        return self


def render(draft: Draft):
    """Produce the same two-file Python API as CLI authors; never import it here."""
    options = (
        {
            "version": "0.162.1",
            "max_turns": draft.actions,
            "reasoning_effort": "medium",
            "reasoning_summary": "detailed",
        }
        if draft.harness == "bounded-codex"
        else {}
    )
    source = (
        '"""Authored in Sprint Studio. Review validation before launching."""\n'
        "from pathlib import Path\n"
        "from sprint import Agent, Budget, Env, Experiment, Sandbox, Task, Validation\n\n"
        "ROOT = Path(__file__).resolve().parent\n"
        "experiment = Experiment(\n"
        f"    task=Task({draft.objective!r}, name={draft.name!r}),\n"
        '    validation=Validation(ROOT / "validation"),\n'
        f"    env=Env(files=ROOT / 'workspace', sandbox=Sandbox(internet={draft.internet!r})),\n"
        f"    agent=Agent({draft.model!r}, harness={draft.harness!r}, options={options!r}),\n"
        f"    budget=Budget(seconds={draft.minutes * 60}),\n"
        ")\n"
    )
    verifier = draft.verifier
    if draft.evaluation == "exact":
        verifier = (
            "def verify(context):\n"
            "    answer = context.submission / 'answer.txt'\n"
            "    expected = (context.private / 'expected.txt').read_text().strip()\n"
            "    correct = answer.is_file() and answer.read_text().strip() == expected\n"
            "    return {'score': float(correct), 'metrics': {'correct': correct}}\n"
        )
    try:
        tree = ast.parse(verifier)
    except SyntaxError as error:
        raise ValueError(
            f"Verifier syntax at line {error.lineno}: {error.msg}"
        ) from error
    if not any(
        isinstance(node, ast.FunctionDef) and node.name == "verify"
        for node in tree.body
    ):
        raise ValueError("The verifier must define verify(context)")
    files = {
        "environment.py": source,
        "validation/evaluate.py": verifier,
        ".gitignore": ".sprint/\n__pycache__/\n",
        "studio.json": draft.model_dump_json(indent=2),
    }
    if draft.evaluation == "exact":
        files["validation/expected.txt"] = draft.expected
    files.update({"workspace/" + name: text for name, text in draft.files.items()})
    if not draft.files:
        files["workspace/README.md"] = "Add learner-visible starting files here.\n"
    warnings = []
    if draft.internet == "none":
        warnings.append(
            "Offline learner inference transport is not configured by this builder; configure it before running."
        )
    if draft.evaluation == "custom" and any(
        isinstance(node, ast.Raise) for node in ast.walk(tree)
    ):
        warnings.append(
            "The verifier contains a raise; review unfinished checks before running."
        )
    if draft.harness != "bounded-codex":
        warnings.append(
            "The action limit is enforced only by the bounded Codex adapter in this export."
        )
    return {
        "files": files,
        "warnings": warnings,
        "validation": "Syntax checked only. No code executed or cloud resources launched.",
    }


class Launch(BaseModel):
    draft: Draft
    backend: Literal["modal", "docker"] = "modal"


def create_app(directory=None):
    runs = StudioRuns(directory or Path(".sprint/studio/runs"))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await runs.close()

    app = FastAPI(
        title="Sprint Studio", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.runs = runs
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # No cross-origin browser writes and no unbounded request-body buffering.
        if request.method == "POST":
            if request.headers.get("origin") not in (
                None,
                str(request.base_url).rstrip("/"),
            ):
                return Response("Cross-origin requests are disabled", status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 3 * 1024**2:
                    return Response("Draft exceeds 3 MiB", status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/preview")
    async def preview(draft: Draft):
        try:
            return render(draft)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/export")
    async def export(draft: Draft):
        try:
            result = render(draft)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, text in result["files"].items():
                archive.writestr(draft.name + "/" + name, text)
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{draft.name}.zip"'},
        )

    @app.get("/api/runs")
    async def list_runs():
        return runs.list()

    @app.post("/api/runs")
    async def launch(request: Launch):
        try:
            result = render(request.draft)
            if (
                request.draft.evaluation == "custom"
                and "NotImplementedError" in request.draft.verifier
            ):
                raise ValueError("Finish the private verifier before launching")
            return runs.start(request.draft, result["files"], request.backend)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    def known_run(identity):
        try:
            return runs.snapshot(identity)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/runs/{identity}")
    async def run_status(identity: str):
        return known_run(identity)

    @app.get("/api/runs/{identity}/events")
    async def run_events(identity: str, cursors: str = "{}"):
        known_run(identity)
        try:
            offsets = json.loads(cursors)
            if not isinstance(offsets, dict) or len(offsets) > 100:
                raise ValueError("invalid cursors")
            if any(type(value) is not int or value < 0 for value in offsets.values()):
                raise ValueError("invalid cursor values")
            return {
                name: (tail_text if name.startswith("native:") else tail)(
                    path, offsets.get(name, 0)
                )
                for name, path in runs.sources(identity).items()
            }
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/runs/{identity}/cancel")
    async def cancel_run(identity: str):
        known_run(identity)
        try:
            return runs.cancel(identity)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/runs/{identity}/log")
    async def run_log(identity: str, channel: str):
        known_run(identity)
        path = runs.sources(identity).get(channel)
        if path is None or path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Log channel not available")
        return FileResponse(
            path, media_type="application/octet-stream", filename=path.name
        )

    @app.get("/api/runs/{identity}/review")
    async def run_review(identity: str):
        known_run(identity)
        path = runs.root(identity) / "records/review.html"
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "The saved review is not available yet")
        return FileResponse(
            path,
            media_type="text/html",
            headers={
                "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'"
            },
        )

    app.mount(
        "/", StaticFiles(directory=Path(__file__).with_name("studio_assets"), html=True)
    )
    return app


def main():
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
