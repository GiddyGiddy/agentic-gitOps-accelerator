import json
import logging
from collections.abc import Callable
from contextlib import ExitStack
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from azure.identity import ManagedIdentityCredential, WorkloadIdentityCredential, get_bearer_token_provider
from openai import OpenAI
from pydantic import ValidationError

from app.llm.models import AdvisoryInput, AdvisoryOutput, QueryIntentOutput
from config.settings import Settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a GitOps advisory assistant. The user message contains only predefined finding "
    "categories, not source material. Explain the categories generally without inventing "
    "specific repositories, paths, tenants, versions, jobs, or results. Provide advice for "
    "human review, not instructions to bypass policy or perform deployments. "
    "Return JSON with exactly two fields: summary (a nonempty string of at most 1200 characters) "
    "and recommendations (an array of 1 to 4 nonempty strings, each at most 400 characters)."
)


class AdvisoryProvider(Protocol):
    def generate(self, advisory: AdvisoryInput) -> AdvisoryOutput: ...

    def classify_query(self, keywords: list[str]) -> QueryIntentOutput: ...

    def close(self) -> None: ...


def validate_url(url: str, *, azure: bool, allow_loopback: bool = False) -> str:
    parsed = urlsplit(url)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback and allow_loopback))
    ):
        raise ValueError("Model endpoint must be HTTPS, or explicitly permitted loopback HTTP")
    if azure and not parsed.hostname.endswith((".openai.azure.com", ".services.ai.azure.com")):
        raise ValueError("Azure OpenAI endpoint must use an Azure OpenAI or Foundry hostname")
    if azure and parsed.path not in ("", "/"):
        raise ValueError("Azure OpenAI endpoint must be a resource root URL")
    return url.rstrip("/")


class OpenAICompatibleProvider:
    def __init__(
        self, client: OpenAI, model: str, credential_close: Callable[[], None] | None = None
    ):
        if not model:
            raise ValueError("Model deployment name is required")
        self.client = client
        self.model = model
        self.credential_close = credential_close

    def generate(self, advisory: AdvisoryInput) -> AdvisoryOutput:
        for attempt in range(2):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(advisory.model_dump(mode="json"), sort_keys=True)},
                ],
                response_format={"type": "json_object"},
                max_completion_tokens=400,
            )
            if not response.choices or response.choices[0].finish_reason != "stop":
                raise ValueError("Model returned incomplete or empty output")
            message = response.choices[0].message
            if message.refusal or not message.content:
                raise ValueError("Model refused or returned no content")
            try:
                return AdvisoryOutput.model_validate_json(message.content)
            except ValidationError:
                if attempt:
                    raise
                logger.warning("Model advisory failed validation; retrying once")
        raise RuntimeError("Advisory retry loop exhausted unexpectedly")

    def classify_query(self, keywords: list[str]) -> QueryIntentOutput:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": (
                    "Classify the supplied unordered generic keywords as exactly one of "
                    "deployed_version, branch_comparison, jira_fix (which chart contains a fix), "
                    "release_origin (planned release or emergency hotfix), or unsupported. If ambiguous, use "
                    "unsupported. Do not infer resource names. Return JSON with only intent."
                )},
                {"role": "user", "content": json.dumps({"keywords": keywords})},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=80,
        )
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise ValueError("Model returned incomplete or empty output")
        message = response.choices[0].message
        if message.refusal or not message.content:
            raise ValueError("Model refused or returned no content")
        return QueryIntentOutput.model_validate_json(message.content)

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            if self.credential_close:
                self.credential_close()


def create_provider(settings: Settings) -> AdvisoryProvider:
    if not settings.llm_model:
        raise ValueError("GITOPS_LLM_MODEL is required")
    credential: ManagedIdentityCredential | WorkloadIdentityCredential | None = None
    api_key: str | Callable[[], str]
    if settings.llm_provider == "azure_openai":
        azure_endpoint = validate_url(settings.llm_azure_endpoint, azure=True)
        base_url = azure_endpoint + "/openai/v1/"
        if settings.llm_azure_auth == "api_key":
            if not settings.llm_azure_api_key:
                raise ValueError("Azure OpenAI API key is required")
            api_key = settings.llm_azure_api_key
        else:
            if settings.llm_azure_auth == "workload_identity":
                credential = WorkloadIdentityCredential()
            else:
                credential = ManagedIdentityCredential(client_id=settings.llm_azure_client_id or None)
            endpoint_host = urlsplit(azure_endpoint).hostname
            token_scope = (
                "https://cognitiveservices.azure.com/.default"
                if endpoint_host and endpoint_host.endswith(".openai.azure.com")
                else "https://ai.azure.com/.default"
            )
            api_key = get_bearer_token_provider(credential, token_scope)
    else:
        base_url = validate_url(
            settings.llm_self_hosted_base_url, azure=False, allow_loopback=settings.llm_allow_insecure_loopback
        ) + "/"
        if not settings.llm_self_hosted_api_key:
            raise ValueError("Self-hosted API key is required")
        api_key = settings.llm_self_hosted_api_key
    with ExitStack() as stack:
        if credential:
            stack.callback(credential.close)
        http_client = httpx.Client(
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=min(5, settings.llm_timeout_seconds)),
            follow_redirects=False,
            trust_env=False,
        )
        stack.callback(http_client.close)
        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=min(5, settings.llm_timeout_seconds)),
            max_retries=0,
            http_client=http_client,
        )
        stack.pop_all()
    return OpenAICompatibleProvider(client, settings.llm_model, credential.close if credential else None)
