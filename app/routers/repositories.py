import os
from datetime import date, datetime, timezone
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.firebase import (
    get_firestore_client,
    verify_id_token,
)


router = APIRouter(
    prefix="/repositories",
    tags=["repositories"],
)


REPOSITORY_COLLECTION = "repositories"
GENERAL_USERS_COLLECTION = "general_users"

GITHUB_API_VERSION = "2022-11-28"
GITHUB_USER_AGENT = "QA-Plain-AI-Builder"


class RepositoryRequest(BaseModel):
    repository_name: str = Field(
        min_length=1,
        max_length=100,
    )

    ui_repository_url: str = Field(
        min_length=1,
        max_length=500,
    )

    api_repository_url: str = Field(
        min_length=1,
        max_length=500,
    )

    github_token: Optional[str] = Field(
        default=None,
        max_length=1000,
    )


class RepositoryTestRequest(RepositoryRequest):
    repository_id: Optional[str] = None


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
            "email": email,
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
        "email": email,
    }


def parse_github_repository_url(
    repository_url: str,
) -> tuple[str, str]:
    value = normalize_text(
        repository_url
    )

    if not value:
        raise HTTPException(
            status_code=400,
            detail="リポジトリURLを入力してください。",
        )

    parsed = urlparse(
        value
    )

    if (
        parsed.scheme not in {
            "http",
            "https",
        }
        or parsed.netloc.lower()
        not in {
            "github.com",
            "www.github.com",
        }
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHubのリポジトリURLを"
                "入力してください。"
            ),
        )

    path_parts = [
        part
        for part in parsed.path.split("/")
        if part
    ]

    if len(path_parts) != 2:
        raise HTTPException(
            status_code=400,
            detail=(
                "リポジトリURLは"
                "https://github.com/所有者/リポジトリ名"
                "の形式で入力してください。"
            ),
        )

    owner = path_parts[0]
    repository_name = path_parts[1]

    if repository_name.endswith(
        ".git"
    ):
        repository_name = (
            repository_name[:-4]
        )

    return (
        owner,
        repository_name,
    )


def normalize_repository_url(
    repository_url: str,
) -> str:
    owner, repository_name = (
        parse_github_repository_url(
            repository_url
        )
    )

    return (
        f"https://github.com/"
        f"{owner}/{repository_name}"
    )


def get_fernet() -> Fernet:
    encryption_key = normalize_text(
        os.getenv(
            "REPOSITORY_TOKEN_ENCRYPTION_KEY",
            "",
        )
    )

    if not encryption_key:
        raise HTTPException(
            status_code=500,
            detail=(
                "REPOSITORY_TOKEN_ENCRYPTION_KEY "
                "is not configured"
            ),
        )

    try:
        return Fernet(
            encryption_key.encode(
                "utf-8"
            )
        )

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "REPOSITORY_TOKEN_ENCRYPTION_KEY "
                "is invalid"
            ),
        ) from error


def encrypt_token(
    token: str,
) -> str:
    return (
        get_fernet()
        .encrypt(
            token.encode(
                "utf-8"
            )
        )
        .decode(
            "utf-8"
        )
    )


def decrypt_token(
    encrypted_token: str,
) -> str:
    if not encrypted_token:
        return ""

    try:
        return (
            get_fernet()
            .decrypt(
                encrypted_token.encode(
                    "utf-8"
                )
            )
            .decode(
                "utf-8"
            )
        )

    except InvalidToken as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "保存済みGitHubトークンを"
                "復号できませんでした。"
            ),
        ) from error


def get_repository_document(
    repository_id: str,
    owner_email: str,
):
    document = (
        get_firestore_client()
        .collection(
            REPOSITORY_COLLECTION
        )
        .document(
            repository_id
        )
        .get()
    )

    if not document.exists:
        raise HTTPException(
            status_code=404,
            detail="リポジトリが見つかりません。",
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
    ):
        raise HTTPException(
            status_code=404,
            detail="リポジトリが見つかりません。",
        )

    return document


def check_duplicate_name(
    owner_email: str,
    repository_name: str,
    exclude_id: Optional[str] = None,
) -> None:
    documents = (
        get_firestore_client()
        .collection(
            REPOSITORY_COLLECTION
        )
        .where(
            "owner_email",
            "==",
            owner_email,
        )
        .where(
            "repository_name",
            "==",
            repository_name,
        )
        .limit(2)
        .stream()
    )

    for document in documents:
        if document.id != exclude_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "同じ名前のリポジトリが"
                    "既に登録されています。"
                ),
            )


def document_to_dict(
    document,
) -> dict:
    data = document.to_dict() or {}

    return {
        "id":
            document.id,
        "repository_name":
            data.get(
                "repository_name",
                "",
            ),
        "ui_repository_url":
            data.get(
                "ui_repository_url",
                "",
            ),
        "api_repository_url":
            data.get(
                "api_repository_url",
                "",
            ),
        "ui_connection_status":
            data.get(
                "ui_connection_status",
                "NOT_TESTED",
            ),
        "api_connection_status":
            data.get(
                "api_connection_status",
                "NOT_TESTED",
            ),
        "last_tested_at":
            data.get(
                "last_tested_at"
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


def resolve_github_token(
    *,
    owner_email: str,
    supplied_token: Optional[str],
    repository_id: Optional[str] = None,
) -> str:
    supplied_token = normalize_text(
        supplied_token
    )

    if supplied_token:
        return supplied_token

    if not repository_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub Personal Access Tokenを"
                "入力してください。"
            ),
        )

    document = get_repository_document(
        repository_id,
        owner_email,
    )

    data = document.to_dict() or {}

    encrypted_token = normalize_text(
        data.get(
            "github_token_enc",
            "",
        )
    )

    if not encrypted_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub Personal Access Tokenを"
                "入力してください。"
            ),
        )

    return decrypt_token(
        encrypted_token
    )


def test_github_repository(
    repository_url: str,
    github_token: str,
) -> bool:
    owner, repository_name = (
        parse_github_repository_url(
            repository_url
        )
    )

    api_url = (
        "https://api.github.com/repos/"
        f"{owner}/{repository_name}"
    )

    request = Request(
        api_url,
        headers={
            "Accept":
                "application/vnd.github+json",
            "Authorization":
                f"Bearer {github_token}",
            "X-GitHub-Api-Version":
                GITHUB_API_VERSION,
            "User-Agent":
                GITHUB_USER_AGENT,
        },
        method="GET",
    )

    try:
        with urlopen(
            request,
            timeout=15,
        ) as response:
            return response.status == 200

    except HTTPError as error:
        if error.code == 401:
            raise HTTPException(
                status_code=400,
                detail=(
                    "GitHubトークンが正しくありません。"
                ),
            ) from error

        if error.code == 403:
            raise HTTPException(
                status_code=400,
                detail=(
                    "GitHubトークンに"
                    "リポジトリ参照権限がありません。"
                ),
            ) from error

        if error.code == 404:
            raise HTTPException(
                status_code=400,
                detail=(
                    "リポジトリが見つからないか、"
                    "参照権限がありません。"
                ),
            ) from error

        raise HTTPException(
            status_code=502,
            detail=(
                "GitHub APIとの通信に失敗しました。"
                f" HTTP {error.code}"
            ),
        ) from error

    except URLError as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "GitHub APIへ接続できませんでした。"
            ),
        ) from error


@router.get("")
def get_repositories(
    authorization: str = Header(...),
):
    user = authenticate_user(
        authorization
    )

    owner_email = user["email"]

    documents = (
        get_firestore_client()
        .collection(
            REPOSITORY_COLLECTION
        )
        .where(
            "owner_email",
            "==",
            owner_email,
        )
        .stream()
    )

    repositories = [
        document_to_dict(
            document
        )
        for document in documents
    ]

    repositories.sort(
        key=lambda item: item.get(
            "repository_name",
            "",
        )
    )

    return {
        "repositories":
            repositories,
    }


@router.post(
    "/test"
)
def test_repository(
    request: RepositoryTestRequest,
    authorization: str = Header(...),
):
    user = authenticate_user(
        authorization
    )

    owner_email = user["email"]

    ui_repository_url = (
        normalize_repository_url(
            request.ui_repository_url
        )
    )

    api_repository_url = (
        normalize_repository_url(
            request.api_repository_url
        )
    )

    github_token = resolve_github_token(
        owner_email=owner_email,
        supplied_token=request.github_token,
        repository_id=request.repository_id,
    )

    ui_connected = test_github_repository(
        ui_repository_url,
        github_token,
    )

    api_connected = test_github_repository(
        api_repository_url,
        github_token,
    )

    tested_at = now_iso()

    if request.repository_id:
        document = get_repository_document(
            request.repository_id,
            owner_email,
        )

        update_data = {
            "ui_repository_url":
                ui_repository_url,
            "api_repository_url":
                api_repository_url,
            "ui_connection_status":
                "SUCCESS"
                if ui_connected
                else "FAILED",
            "api_connection_status":
                "SUCCESS"
                if api_connected
                else "FAILED",
            "last_tested_at":
                tested_at,
            "updated_at":
                tested_at,
        }

        if normalize_text(
            request.github_token
        ):
            update_data[
                "github_token_enc"
            ] = encrypt_token(
                github_token
            )

        document.reference.update(
            update_data
        )

    return {
        "ui_repository_connected":
            ui_connected,
        "api_repository_connected":
            api_connected,
        "ui_connection_status":
            "SUCCESS"
            if ui_connected
            else "FAILED",
        "api_connection_status":
            "SUCCESS"
            if api_connected
            else "FAILED",
        "last_tested_at":
            tested_at,
    }


@router.get(
    "/{repository_id}"
)
def get_repository(
    repository_id: str,
    authorization: str = Header(...),
):
    user = authenticate_user(
        authorization
    )

    document = get_repository_document(
        repository_id,
        user["email"],
    )

    return document_to_dict(
        document
    )


@router.post(
    "",
    status_code=201,
)
def create_repository(
    request: RepositoryRequest,
    authorization: str = Header(...),
):
    user = authenticate_user(
        authorization
    )

    owner_email = user["email"]

    repository_name = normalize_text(
        request.repository_name
    )

    ui_repository_url = (
        normalize_repository_url(
            request.ui_repository_url
        )
    )

    api_repository_url = (
        normalize_repository_url(
            request.api_repository_url
        )
    )

    check_duplicate_name(
        owner_email,
        repository_name,
    )

    github_token = normalize_text(
        request.github_token
    )

    if not github_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub Personal Access Tokenを"
                "入力してください。"
            ),
        )

    ui_connected = test_github_repository(
        ui_repository_url,
        github_token,
    )

    api_connected = test_github_repository(
        api_repository_url,
        github_token,
    )

    now = now_iso()

    data = {
        "owner_email":
            owner_email,
        "repository_name":
            repository_name,
        "ui_repository_url":
            ui_repository_url,
        "api_repository_url":
            api_repository_url,
        "github_token_enc":
            encrypt_token(
                github_token
            ),
        "ui_connection_status":
            "SUCCESS"
            if ui_connected
            else "FAILED",
        "api_connection_status":
            "SUCCESS"
            if api_connected
            else "FAILED",
        "last_tested_at":
            now,
        "created_at":
            now,
        "updated_at":
            now,
    }

    document_reference = (
        get_firestore_client()
        .collection(
            REPOSITORY_COLLECTION
        )
        .document()
    )

    document_reference.set(
        data
    )

    return document_to_dict(
        document_reference.get()
    )


@router.put(
    "/{repository_id}"
)
def update_repository(
    repository_id: str,
    request: RepositoryRequest,
    authorization: str = Header(...),
):
    user = authenticate_user(
        authorization
    )

    owner_email = user["email"]

    document = get_repository_document(
        repository_id,
        owner_email,
    )

    current = document.to_dict() or {}

    repository_name = normalize_text(
        request.repository_name
    )

    ui_repository_url = (
        normalize_repository_url(
            request.ui_repository_url
        )
    )

    api_repository_url = (
        normalize_repository_url(
            request.api_repository_url
        )
    )

    check_duplicate_name(
        owner_email,
        repository_name,
        repository_id,
    )

    update_data = {
        "repository_name":
            repository_name,
        "ui_repository_url":
            ui_repository_url,
        "api_repository_url":
            api_repository_url,
        "updated_at":
            now_iso(),
    }

    if normalize_text(
        request.github_token
    ):
        update_data[
            "github_token_enc"
        ] = encrypt_token(
            normalize_text(
                request.github_token
            )
        )

    if (
        repository_name
        != current.get(
            "repository_name"
        )
        or ui_repository_url
        != current.get(
            "ui_repository_url"
        )
        or api_repository_url
        != current.get(
            "api_repository_url"
        )
        or normalize_text(
            request.github_token
        )
    ):
        update_data[
            "ui_connection_status"
        ] = "NOT_TESTED"

        update_data[
            "api_connection_status"
        ] = "NOT_TESTED"

        update_data[
            "last_tested_at"
        ] = None

    document.reference.update(
        update_data
    )

    return document_to_dict(
        document.reference.get()
    )


@router.delete(
    "/{repository_id}"
)
def delete_repository(
    repository_id: str,
    authorization: str = Header(...),
):
    user = authenticate_user(
        authorization
    )

    document = get_repository_document(
        repository_id,
        user["email"],
    )

    document.reference.delete()

    return {
        "status":
            "deleted",
        "id":
            repository_id,
    }
