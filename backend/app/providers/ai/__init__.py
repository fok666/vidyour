"""AI provider selection interface."""

from typing import Any


def select_ai_provider(settings: Any) -> Any:
    from app.providers.ai.bedrock import BedrockProvider
    from app.providers.ai.gemini import GeminiProvider
    from app.providers.ai.heuristic import HeuristicFallbackProvider

    active = settings.capabilities()["active_ai_provider"]
    if active == "gemini":
        return GeminiProvider(settings)
    if active == "bedrock":
        import boto3

        client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        return BedrockProvider(settings, client=client)
    return HeuristicFallbackProvider()
