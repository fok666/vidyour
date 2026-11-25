"""Project CRUD router interfaces."""

from typing import Any

from fastapi import APIRouter, HTTPException, Response

from app.api import AppContext, context_value


def create_projects_router(
    repository: Any = None,
    *,
    context: AppContext | Any | None = None,
) -> APIRouter:
    api = APIRouter()
    repo = context_value(context, "repository", repository)

    @api.post("/projects", status_code=201)
    def create_project(payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=422, detail="Project name is required")
        project = repo.create_project(name.strip()) if repo is not None else None
        if project is None:
            raise HTTPException(status_code=503, detail="Project repository unavailable")
        return project.model_dump()

    @api.get("/projects")
    def list_projects() -> list[Any]:
        if repo is None:
            raise HTTPException(status_code=503, detail="Project repository unavailable")
        return [project.model_dump() for project in repo.list_projects()]

    @api.get("/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        project = repo.get_project(project_id) if repo is not None else None
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project.model_dump()

    @api.patch("/projects/{project_id}")
    def update_project(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            raise HTTPException(status_code=503, detail="Project repository unavailable")
        try:
            project = repo.update_project(project_id, **payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid project update") from exc
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project.model_dump()

    @api.delete("/projects/{project_id}")
    def delete_project(project_id: str) -> Response:
        if repo is None:
            raise HTTPException(status_code=503, detail="Project repository unavailable")
        if not repo.delete_project(project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        return Response(status_code=204)

    return api


router = create_projects_router()
