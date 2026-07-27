from fastapi import APIRouter, Request, Response, status

from .github_structure_common import (
    GitHubStructureRequest,
    create_structure,
    delete_structure,
    get_structure,
    list_structures,
    update_structure,
)


router = APIRouter(
    prefix="/v1/github-structures/api",
    tags=["GitHub API Structures"],
)


@router.get("")
def list_api_structures(request: Request):
    return list_structures(
        request=request,
        repository_type="api",
    )


@router.get("/{structure_id}")
def get_api_structure(
    structure_id: str,
    request: Request,
):
    return get_structure(
        request=request,
        repository_type="api",
        structure_id=structure_id,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
def create_api_structure(
    payload: GitHubStructureRequest,
    request: Request,
):
    return create_structure(
        request=request,
        repository_type="api",
        payload=payload,
    )


@router.put("/{structure_id}")
def update_api_structure(
    structure_id: str,
    payload: GitHubStructureRequest,
    request: Request,
):
    return update_structure(
        request=request,
        repository_type="api",
        structure_id=structure_id,
        payload=payload,
    )


@router.delete(
    "/{structure_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_api_structure(
    structure_id: str,
    request: Request,
):
    delete_structure(
        request=request,
        repository_type="api",
        structure_id=structure_id,
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
