import logging

from azure.core.exceptions import AzureError
from openai import OpenAIError

from app.llm.models import AdvisoryInput
from app.llm.providers import create_provider
from config.settings import Settings

logger = logging.getLogger(__name__)


def generate_advisory(settings: Settings, advisory: AdvisoryInput) -> dict:
    try:
        provider = create_provider(settings)
        try:
            output = provider.generate(advisory)
        finally:
            provider.close()
    except (OpenAIError, AzureError, ValueError) as exc:
        logger.warning("Model advisory failed (%s); deterministic result retained", type(exc).__name__)
        return {"status": "failed", "error": f"Model advisory failed ({type(exc).__name__})"}
    return {"status": "generated", "content": output.model_dump()}
