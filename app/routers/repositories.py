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

REPOSITORY_TYPES = {
    "UI",
    "API",
}

GITHUB_API_VERSION = "2022-11-28"
GITHUB_USER_AGENT = "QA-Plain-AI-Builder"


class RepositoryRequest(BaseModel):
    repository_type: str = Field(
        min_length=1,
        max_length=10,
    )

    repository_url: str = Field(
        min_length=1,
        max_length=500,
    )

    github_token: Optional[str] = Field(
        default=None,
        max_length=1000,
    )


class RepositoryTestRequest(BaseModel):
    repository_type: str = Field(
        min_length=1,
        max_length=10,
    )

    repository_url: str = Field(
        min_length=1,
        max_length=500,
    )

    github_token: Optional[str] = Field(
        default=None,
        max_length=1000,
    )


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


def normalize_repository_type(
    value,
) -> str:
    repository_type = normalize_text(
        value
    ).upper()

    if repository_type not in REPOSITORY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "リポジトリ種別は"
                "UIまたはAPIを指定してください。"
            ),
        )

    return repository_type


def normalize_repository_url(
    value,
) -> str:
    repository_url = normalize_text(
        value
    )

    try:
        owner, repository_name = (
            parse_github_repository_url(
                repository_url
            )
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return (
        f"https://github.com/"
        f"{owner}/{repository_name}"
    )


def parse_github_repository_url(
    repository_url: str,
) -> tuple[str, str]:
    value = normalize_text(
        repository_url
    )

    if not value:
        raise ValueError(
            "リポジトリURLを入力してください。"
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
        raise ValueError(
            "GitHubのリポジトリURLを入力してください。"
        )

    path_parts = [
        part
        for part in parsed.path.split("/")
        if part
    ]

    if len(path_parts) != 2:
        raise ValueError(
            "リポジトリURLは"
            "https://github.com/所有者/リポジトリ名"
            "の形式で入力してください。"
        )

    owner = path_parts[0]
    repository_name = path_parts[1]

    if repository_name.endswith(
        ".git"
    ):
        repository_name = (
            repository_name[:-4]
        )

    if (
        not owner
        or not repository_name
    ):
        raise ValueError(
            "GitHubの所有者と"
            "リポジトリ名を確認してください。"
        )

    return (
        owner,
        repository_name,
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


def get_repository_by_type(
    owner_email: str,
    repository_type: str,
    exclude_id: Optional[str] = None,
):
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
            "repository_type",
            "==",
            repository_type,
        )
        .limit(2)
        .stream()
    )

    for document in documents:
        if document.id != exclude_id:
            return document

    return None


def check_duplicate_repository_type(
    owner_email: str,
    repository_type: str,
    exclude_id: Optional[str] = None,
) -> None:
    document = get_repository_by_type(
        owner_email,
        repository_type,
        exclude_id,
    )

    if document:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{repository_type}用リポジトリは"
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
        "repository_type":
            data.get(
                "repository_type",
                "",
            ),
        "repository_url":
            data.get(
                "repository_url",
                "",
            ),
        "connection_status":
            data.get(
                "connection_status",
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
    repository_type: str,
    supplied_token: Optional[str],
    repository_id: Optional[str] = None,
) -> str:
    normalized_token = normalize_text(
        supplied_token
    )

    if normalized_token:
        return normalized_token

    document = None

    if repository_id:
        document = get_repository_document(
            repository_id,
            owner_email,
        )

    if not document:
        document = get_repository_by_type(
            owner_email,
            repository_type,
        )

    if not document:
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub Personal Access Tokenを"
                "入力してください。"
            ),
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
) -> dict:
    owner, repository_name = (
        parse_github_repository_url(
            repository_url
        )
    )

    api_url = (
        "https://api.github.com/repos/"
        f"{owner}/{repository_name}"
    )

    headers = {
        "Accept":
            "application/vnd.github+json",
        "Authorization":
            f"Bearer {github_token}",
        "X-GitHub-Api-Version":
            GITHUB_API_VERSION,
        "User-Agent":
            GITHUB_USER_AGENT,
    }

    request = Request(
        api_url,
        headers=headers,
        method="GET",
    )

    try:
        with urlopen(
            request,
            timeout=15,
        ) as response:
            status_code = response.status

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

    if status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                "GitHub APIから"
                "正常な応答を取得できませんでした。"
            ),
        )

    return {
        "connected": True,
        "owner": owner,
        "repository_name": repository_name,
    }


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
        key=lambda item: (
            0
            if item.get(
                "repository_type"
            ) == "UI"
            else 1,
            item.get(
                "repository_url",
                "",
            ),
        )
    )

    return {
        "repositories":
            repositories,
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

    repository_type = (
        normalize_repository_type(
            request.repository_type
        )
    )

    repository_url = (
        normalize_repository_url(
            request.repository_url
        )
    )

    check_duplicate_repository_type(
        owner_email,
        repository_type,
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

    test_github_repository(
        repository_url,
        github_token,
    )

    now = now_iso()

    data = {
        "owner_email":
            owner_email,
        "repository_type":
            repository_type,
        "repository_url":
            repository_url,
        "github_token_enc":
            encrypt_token(
                github_token
            ),
        "connection_status":
            "SUCCESS",
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

    repository_type = (
        normalize_repository_type(
            request.repository_type
        )
    )

    repository_url = (
        normalize_repository_url(
            request.repository_url
        )
    )

    check_duplicate_repository_type(
        owner_email,
        repository_type,
        repository_id,
    )

    github_token = resolve_github_token(
        owner_email=owner_email,
        repository_type=repository_type,
        supplied_token=request.github_token,
        repository_id=repository_id,
    )

    update_data = {
        "repository_type":
            repository_type,
        "repository_url":
            repository_url,
        "updated_at":
            now_iso(),
    }

    if normalize_text(
        request.github_token
    ):
        update_data["github_token_enc"] = (
            encrypt_token(
                github_token
            )
        )

    if (
        repository_type
        != current.get(
            "repository_type"
        )
        or repository_url
        != current.get(
            "repository_url"
        )
        or normalize_text(
            request.github_token
        )
    ):
        update_data["connection_status"] = (
            "NOT_TESTED"
        )

        update_data["last_tested_at"] = None

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
        "status": "deleted",
        "id": repository_id,
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

    repository_type = (
        normalize_repository_type(
            request.repository_type
        )
    )

    repository_url = (
        normalize_repository_url(
            request.repository_url
        )
    )

    github_token = resolve_github_token(
        owner_email=owner_email,
        repository_type=repository_type,
        supplied_token=request.github_token,
    )

    result = test_github_repository(
        repository_url,
        github_token,
    )

    document = get_repository_by_type(
        owner_email,
        repository_type,
    )

    tested_at = now_iso()

    if document:
        document.reference.update({
            "repository_url":
                repository_url,
            "connection_status":
                "SUCCESS",
            "last_tested_at":
                tested_at,
            "updated_at":
                tested_at,
        })

        if normalize_text(
            request.github_token
        ):
            document.reference.update({
                "github_token_enc":
                    encrypt_token(
                        github_token
                    )
            })

    return {
        **result,
        "repository_type":
            repository_type,
        "repository_url":
            repository_url,
        "connection_status":
            "SUCCESS",
        "last_tested_at":
            tested_at,
    }
