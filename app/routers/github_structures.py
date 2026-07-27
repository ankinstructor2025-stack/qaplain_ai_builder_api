import os
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.core.firebase import (
    get_firestore_client,
    verify_id_token,
)


router = APIRouter(
    prefix="/github-structures",
    tags=["github-structures"],
)


COLLECTION_NAME = "github_structures"
GENERAL_USERS_COLLECTION = "general_users"

RepositoryType = Literal["ui", "api"]


class GitHubStructureRequest(BaseModel):
    directory_path: str = Field(
        min_length=1,
        max_length=500,
    )

    extensions: list[str] = Field(
        min_length=1,
    )

    @field_validator(
        "directory_path"
    )
    @classmethod
    def validate_directory_path(
        cls,
        value: str,
    ) -> str:
        normalized = normalize_directory_path(
            value
        )

        if not normalized:
            raise ValueError(
                "GitHub上のパスを入力してください。"
            )

        if ".." in normalized.split("/"):
            raise ValueError(
                "GitHub上のパスに「..」は使用できません。"
            )

        return normalized

    @field_validator(
        "extensions"
    )
    @classmethod
    def validate_extensions(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized: list[str] = []

        for raw_value in values:
            extension = normalize_text(
                raw_value
            ).lower()

            if not extension:
                continue

            if not extension.startswith(
                "."
            ):
                extension = (
                    f".{extension}"
                )

            extension_body = extension[1:]

            if (
                not extension_body
                or not extension_body
                    .replace("_", "")
                    .replace("-", "")
                    .isalnum()
            ):
                raise ValueError(
                    f"対象拡張子「{extension}」"
                    "の形式が不正です。"
                )

            if extension not in normalized:
                normalized.append(
                    extension
                )

        if not normalized:
            raise ValueError(
                "対象拡張子を入力してください。"
            )

        return normalized


def now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_text(
    value,
) -> str:
    return str(
        value or ""
    ).strip()


def normalize_email(
    value,
) -> str:
    return normalize_text(
        value
    ).lower()


def normalize_directory_path(
    value,
) -> str:
    normalized = normalize_text(
        value
    ).replace(
        "\\",
        "/",
    )

    if normalized == ".":
        return "."

    return "/".join(
        part
        for part in normalized.split("/")
        if part
    )


def authenticate_user(
    authorization: str,
) -> dict:
    if not authorization.startswith(
        "Bearer "
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header",
        )

    id_token = authorization.replace(
        "Bearer ",
        "",
        1,
    ).strip()

    try:
        decoded_token = verify_id_token(
            id_token
        )

    except Exception as error:
        print(
            "verify_id_token error: "
            f"{type(error).__name__}: "
            f"{error}"
        )

        raise HTTPException(
            status_code=401,
            detail="認証情報を確認できませんでした。",
        ) from error

    email = normalize_email(
        decoded_token.get(
            "email",
            "",
        )
    )

    if not email:
        raise HTTPException(
            status_code=401,
            detail="メールアドレスを取得できませんでした。",
        )

    system_administrator = normalize_email(
        os.getenv(
            "SYSTEM_ADMINISTRATOR",
            "",
        )
    )

    if (
        system_administrator
        and email == system_administrator
    ):
        return {
            **decoded_token,
            "email":
                email,
        }

    documents = (
        get_firestore_client()
        .collection(
            GENERAL_USERS_COLLECTION
        )
        .where(
            "email",
            "==",
            email,
        )
        .limit(1)
        .stream()
    )

    document = next(
        documents,
        None,
    )

    if not document:
        raise HTTPException(
            status_code=403,
            detail="利用者として登録されていません。",
        )

    data = document.to_dict() or {}

    start_date = normalize_text(
        data.get(
            "start_date",
            "",
        )
    )

    end_date = normalize_text(
        data.get(
            "end_date",
            "",
        )
    )

    today = date.today().isoformat()

    is_active = (
        (
            not start_date
            or start_date <= today
        )
        and
        (
            not end_date
            or end_date >= today
        )
    )

    if not is_active:
        raise HTTPException(
            status_code=403,
            detail="利用期間外です。",
        )

    return {
        **decoded_token,
        "email":
            email,
    }


def document_to_dict(
    document,
) -> dict:
    data = document.to_dict() or {}

    return {
        "id":
            document.id,
        "repository_type":
            data.get(
                "repository_type",
                "",
            ),
        "directory_path":
            data.get(
                "directory_path",
                "",
            ),
        "extensions":
            data.get(
                "extensions",
                [],
            ),
        "created_at":
            data.get(
                "created_at"
            ),
        "updated_at":
            data.get(
                "updated_at"
            ),
    }


def get_structure_document(
    *,
    structure_id: str,
    owner_email: str,
    repository_type: RepositoryType,
):
    document = (
        get_firestore_client()
        .collection(
            COLLECTION_NAME
        )
        .document(
            structure_id
        )
        .get()
    )

    if not document.exists:
        raise HTTPException(
            status_code=404,
            detail="GitHub構成が見つかりません。",
        )

    data = document.to_dict() or {}

    if (
        normalize_email(
            data.get(
                "owner_email",
                "",
            )
        )
        != owner_email
        or data.get(
            "repository_type"
        )
        != repository_type
    ):
        raise HTTPException(
            status_code=404,
            detail="GitHub構成が見つかりません。",
        )

    return document


def check_duplicate_path(
    *,
    owner_email: str,
    repository_type: RepositoryType,
    directory_path: str,
    exclude_id: str | None = None,
) -> None:
    documents = (
        get_firestore_client()
        .collection(
            COLLECTION_NAME
        )
        .where(
            "owner_email",
            "==",
            owner_email,
        )
        .where(
            "repository_type",
            "==",
            repository_type,
        )
        .where(
            "directory_path",
            "==",
            directory_path,
        )
        .stream()
    )

    for document in documents:
        if document.id != exclude_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "同じGitHub上のパスが"
                    "既に登録されています。"
                ),
            )


def list_structures(
    *,
    repository_type: RepositoryType,
    authorization: str,
):
    user = authenticate_user(
        authorization
    )

    owner_email = user["email"]

    documents = (
        get_firestore_client()
        .collection(
            COLLECTION_NAME
        )
        .where(
            "owner_email",
            "==",
            owner_email,
        )
        .where(
            "repository_type",
            "==",
            repository_type,
        )
        .stream()
    )

    structures = [
        document_to_dict(
            document
        )
        for document in documents
    ]

    structures.sort(
        key=lambda item:
            item.get(
                "directory_path",
                "",
            )
    )

    return {
        "structures":
            structures,
    }


def get_structure(
    *,
    repository_type: RepositoryType,
    structure_id: str,
    authorization: str,
):
    user = authenticate_user(
        authorization
    )

    document = get_structure_document(
        structure_id=structure_id,
        owner_email=user["email"],
        repository_type=repository_type,
    )

    return document_to_dict(
        document
    )


def create_structure(
    *,
    repository_type: RepositoryType,
    request: GitHubStructureRequest,
    authorization: str,
):
    user = authenticate_user(
        authorization
    )

    owner_email = user["email"]

    check_duplicate_path(
        owner_email=owner_email,
        repository_type=repository_type,
        directory_path=request.directory_path,
    )

    now = now_iso()

    data = {
        "owner_email":
            owner_email,
        "repository_type":
            repository_type,
        "directory_path":
            request.directory_path,
        "extensions":
            request.extensions,
        "created_at":
            now,
        "updated_at":
            now,
    }

    document_reference = (
        get_firestore_client()
        .collection(
            COLLECTION_NAME
        )
        .document()
    )

    document_reference.set(
        data
    )

    return document_to_dict(
        document_reference.get()
    )


def update_structure(
    *,
    repository_type: RepositoryType,
    structure_id: str,
    request: GitHubStructureRequest,
    authorization: str,
):
    user = authenticate_user(
        authorization
    )

    owner_email = user["email"]

    document = get_structure_document(
        structure_id=structure_id,
        owner_email=owner_email,
        repository_type=repository_type,
    )

    check_duplicate_path(
        owner_email=owner_email,
        repository_type=repository_type,
        directory_path=request.directory_path,
        exclude_id=structure_id,
    )

    document.reference.update({
        "directory_path":
            request.directory_path,
        "extensions":
            request.extensions,
        "updated_at":
            now_iso(),
    })

    return document_to_dict(
        document.reference.get()
    )


def delete_structure(
    *,
    repository_type: RepositoryType,
    structure_id: str,
    authorization: str,
):
    user = authenticate_user(
        authorization
    )

    document = get_structure_document(
        structure_id=structure_id,
        owner_email=user["email"],
        repository_type=repository_type,
    )

    document.reference.delete()

    return {
        "status":
            "deleted",
        "id":
            structure_id,
    }


@router.get(
    "/ui"
)
def get_ui_structures(
    authorization: str = Header(...),
):
    return list_structures(
        repository_type="ui",
        authorization=authorization,
    )


@router.get(
    "/ui/{structure_id}"
)
def get_ui_structure(
    structure_id: str,
    authorization: str = Header(...),
):
    return get_structure(
        repository_type="ui",
        structure_id=structure_id,
        authorization=authorization,
    )


@router.post(
    "/ui",
    status_code=201,
)
def create_ui_structure(
    request: GitHubStructureRequest,
    authorization: str = Header(...),
):
    return create_structure(
        repository_type="ui",
        request=request,
        authorization=authorization,
    )


@router.put(
    "/ui/{structure_id}"
)
def update_ui_structure(
    structure_id: str,
    request: GitHubStructureRequest,
    authorization: str = Header(...),
):
    return update_structure(
        repository_type="ui",
        structure_id=structure_id,
        request=request,
        authorization=authorization,
    )


@router.delete(
    "/ui/{structure_id}"
)
def delete_ui_structure(
    structure_id: str,
    authorization: str = Header(...),
):
    return delete_structure(
        repository_type="ui",
        structure_id=structure_id,
        authorization=authorization,
    )


@router.get(
    "/api"
)
def get_api_structures(
    authorization: str = Header(...),
):
    return list_structures(
        repository_type="api",
        authorization=authorization,
    )


@router.get(
    "/api/{structure_id}"
)
def get_api_structure(
    structure_id: str,
    authorization: str = Header(...),
):
    return get_structure(
        repository_type="api",
        structure_id=structure_id,
        authorization=authorization,
    )


@router.post(
    "/api",
    status_code=201,
)
def create_api_structure(
    request: GitHubStructureRequest,
    authorization: str = Header(...),
):
    return create_structure(
        repository_type="api",
        request=request,
        authorization=authorization,
    )


@router.put(
    "/api/{structure_id}"
)
def update_api_structure(
    structure_id: str,
    request: GitHubStructureRequest,
    authorization: str = Header(...),
):
    return update_structure(
        repository_type="api",
        structure_id=structure_id,
        request=request,
        authorization=authorization,
    )


@router.delete(
    "/api/{structure_id}"
)
def delete_api_structure(
    structure_id: str,
    authorization: str = Header(...),
):
    return delete_structure(
        repository_type="api",
        structure_id=structure_id,
        authorization=authorization,
    )
