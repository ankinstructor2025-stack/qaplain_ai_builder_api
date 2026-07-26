from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException, Request, status
from google.cloud import firestore
from pydantic import BaseModel, Field, field_validator


db = firestore.Client()

COLLECTION_NAME = "github_structures"
RepositoryType = Literal["ui", "api"]


class GitHubStructureRequest(BaseModel):
    directory_name: str = Field(..., min_length=1, max_length=100)
    directory_path: str = Field(..., min_length=1, max_length=500)
    extensions: list[str] = Field(..., min_length=1)
    display_order: int = Field(..., ge=1, le=9999)
    enabled: bool = True

    @field_validator("directory_name")
    @classmethod
    def validate_directory_name(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("階層名を入力してください。")

        return value

    @field_validator("directory_path")
    @classmethod
    def validate_directory_path(cls, value: str) -> str:
        value = normalize_directory_path(value)

        if not value:
            raise ValueError("GitHub上のパスを入力してください。")

        if ".." in value.split("/"):
            raise ValueError("GitHub上のパスに「..」は使用できません。")

        return value

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []

        for raw_value in values:
            extension = str(raw_value).strip().lower()

            if not extension:
                continue

            if not extension.startswith("."):
                extension = f".{extension}"

            body = extension[1:]

            if not body or not body.replace("_", "").replace("-", "").isalnum():
                raise ValueError(
                    f"対象拡張子「{extension}」の形式が不正です。"
                )

            if extension not in normalized:
                normalized.append(extension)

        if not normalized:
            raise ValueError("対象拡張子を1件以上指定してください。")

        return normalized


def normalize_directory_path(value: str) -> str:
    value = value.strip().replace("\\", "/")

    if value == ".":
        return "."

    parts = [
        part.strip()
        for part in value.split("/")
        if part.strip()
    ]

    return "/".join(parts)


def get_tenant_id(request: Request) -> str:
    """
    認証処理で request.state.tenant_id を設定している前提。

    現在の認証方式が異なる場合は、この関数だけ既存処理に合わせて
    差し替えてください。
    """
    tenant_id = getattr(request.state, "tenant_id", None)

    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="テナント情報を取得できません。",
        )

    return str(tenant_id)


def get_collection(tenant_id: str):
    return (
        db.collection("tenants")
        .document(tenant_id)
        .collection(COLLECTION_NAME)
    )


def serialize_document(
    document: firestore.DocumentSnapshot,
) -> dict[str, Any]:
    data = document.to_dict() or {}

    return {
        "id": document.id,
        "repository_type": data.get("repository_type", ""),
        "directory_name": data.get("directory_name", ""),
        "directory_path": data.get("directory_path", ""),
        "extensions": data.get("extensions", []),
        "display_order": data.get("display_order", 1),
        "enabled": data.get("enabled", True),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }


def ensure_unique_path(
    *,
    tenant_id: str,
    repository_type: RepositoryType,
    directory_path: str,
    exclude_id: str | None = None,
) -> None:
    query = (
        get_collection(tenant_id)
        .where("repository_type", "==", repository_type)
        .where("directory_path", "==", directory_path)
    )

    for document in query.stream():
        if document.id != exclude_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="同じGitHubパスが既に登録されています。",
            )


def list_structures(
    *,
    request: Request,
    repository_type: RepositoryType,
) -> list[dict[str, Any]]:
    tenant_id = get_tenant_id(request)

    query = (
        get_collection(tenant_id)
        .where("repository_type", "==", repository_type)
        .order_by("display_order")
    )

    return [
        serialize_document(document)
        for document in query.stream()
    ]


def get_structure(
    *,
    request: Request,
    repository_type: RepositoryType,
    structure_id: str,
) -> dict[str, Any]:
    tenant_id = get_tenant_id(request)
    document = get_collection(tenant_id).document(structure_id).get()

    if not document.exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="GitHub構成が見つかりません。",
        )

    data = document.to_dict() or {}

    if data.get("repository_type") != repository_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="GitHub構成が見つかりません。",
        )

    return serialize_document(document)


def create_structure(
    *,
    request: Request,
    repository_type: RepositoryType,
    payload: GitHubStructureRequest,
) -> dict[str, Any]:
    tenant_id = get_tenant_id(request)

    ensure_unique_path(
        tenant_id=tenant_id,
        repository_type=repository_type,
        directory_path=payload.directory_path,
    )

    document_reference = get_collection(tenant_id).document()

    document_reference.set(
        {
            "repository_type": repository_type,
            "directory_name": payload.directory_name,
            "directory_path": payload.directory_path,
            "extensions": payload.extensions,
            "display_order": payload.display_order,
            "enabled": payload.enabled,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )

    return serialize_document(document_reference.get())


def update_structure(
    *,
    request: Request,
    repository_type: RepositoryType,
    structure_id: str,
    payload: GitHubStructureRequest,
) -> dict[str, Any]:
    tenant_id = get_tenant_id(request)
    document_reference = get_collection(tenant_id).document(structure_id)
    current_document = document_reference.get()

    if not current_document.exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="GitHub構成が見つかりません。",
        )

    current_data = current_document.to_dict() or {}

    if current_data.get("repository_type") != repository_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="GitHub構成が見つかりません。",
        )

    ensure_unique_path(
        tenant_id=tenant_id,
        repository_type=repository_type,
        directory_path=payload.directory_path,
        exclude_id=structure_id,
    )

    document_reference.update(
        {
            "directory_name": payload.directory_name,
            "directory_path": payload.directory_path,
            "extensions": payload.extensions,
            "display_order": payload.display_order,
            "enabled": payload.enabled,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )

    return serialize_document(document_reference.get())


def delete_structure(
    *,
    request: Request,
    repository_type: RepositoryType,
    structure_id: str,
) -> None:
    tenant_id = get_tenant_id(request)
    document_reference = get_collection(tenant_id).document(structure_id)
    current_document = document_reference.get()

    if not current_document.exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="GitHub構成が見つかりません。",
        )

    current_data = current_document.to_dict() or {}

    if current_data.get("repository_type") != repository_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="GitHub構成が見つかりません。",
        )

    document_reference.delete()
