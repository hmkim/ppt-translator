"""
AWS Bedrock client wrapper with connection management
"""
import os
import logging
from typing import Optional, Any
from .dependencies import DependencyManager
from .retry import bedrock_retry

logger = logging.getLogger(__name__)


def _build_boto_config():
    """Build a botocore Config with a minimal adaptive retry layer.

    Adaptive mode gives us the client-side throttle token bucket which smooths
    bursts across concurrent calls. We cap max_attempts at 2 here because
    tenacity handles the retry count (keeping the two layers from multiplying).
    """
    try:
        from botocore.config import Config as BotoConfig
    except ImportError:
        return None
    return BotoConfig(retries={'mode': 'adaptive', 'max_attempts': 2})


# Substrings identifying models that reject the `temperature` inference
# parameter (adaptive-thinking models such as Claude Sonnet 5, whose reasoning
# is always on). Extend this tuple as more such models ship.
_NO_TEMPERATURE_MODEL_MARKERS = ('claude-sonnet-5',)


def _has_temperature(kwargs) -> bool:
    """True if the converse kwargs carry a `temperature` inference param."""
    inference_config = kwargs.get('inferenceConfig')
    return isinstance(inference_config, dict) and 'temperature' in inference_config


def _without_temperature(kwargs) -> dict:
    """Return a shallow copy of kwargs with `temperature` removed."""
    inference_config = {
        k: v for k, v in kwargs['inferenceConfig'].items() if k != 'temperature'
    }
    return {**kwargs, 'inferenceConfig': inference_config}


def _strip_unsupported_temperature(kwargs) -> dict:
    """Proactively drop `temperature` for models known to reject it.

    Avoids a wasted first API call on every request for those models.
    """
    if not _has_temperature(kwargs):
        return kwargs
    model_id = (kwargs.get('modelId') or '').lower()
    if any(marker in model_id for marker in _NO_TEMPERATURE_MODEL_MARKERS):
        return _without_temperature(kwargs)
    return kwargs


def _is_temperature_rejected_error(error: Exception) -> bool:
    """True if a Bedrock error indicates the `temperature` param was rejected."""
    return 'temperature' in str(error).lower()


class BedrockClient:
    """AWS Bedrock client wrapper with connection management"""

    def __init__(self, region: str = None):
        self._client = None
        self._initialized = False
        self.region = region or os.getenv('AWS_REGION', 'us-east-1')
        self.deps = DependencyManager()

    @property
    def client(self) -> Optional[Any]:
        """Lazy initialization of Bedrock client"""
        if not self._initialized:
            self._initialize()
        return self._client

    def _initialize(self) -> bool:
        """Initialize the AWS Bedrock client"""
        try:
            boto3 = self.deps.require('boto3')
            logger.info(f"Initializing Bedrock client with region: {self.region}")
            boto_config = _build_boto_config()
            client_kwargs = {'region_name': self.region}
            if boto_config is not None:
                client_kwargs['config'] = boto_config

            # Try default credential chain first
            try:
                self._client = boto3.client('bedrock-runtime', **client_kwargs)
                logger.info("✅ Bedrock client initialized with default credentials")
                self._initialized = True
                return True
            except Exception as e:
                logger.warning(f"Default credentials failed: {str(e)}")

            # Fallback to explicit credentials
            access_key = os.getenv('AWS_ACCESS_KEY_ID')
            secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')

            if access_key and secret_key and not access_key.startswith('${'):
                self._client = boto3.client(
                    'bedrock-runtime',
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    **client_kwargs,
                )
                logger.info("✅ Bedrock client initialized with explicit credentials")
                self._initialized = True
                return True
            else:
                logger.error("❌ AWS credentials not properly configured")
                return False

        except Exception as e:
            logger.error(f"❌ Failed to initialize AWS Bedrock client: {str(e)}")
            return False

    def is_ready(self) -> bool:
        """Check if client is ready"""
        return self.client is not None

    @bedrock_retry
    def converse(self, **kwargs) -> Any:
        """Wrapper for converse API call with automatic retry on transient errors.

        Some newer adaptive-thinking models (e.g., Claude Sonnet 5) reject the
        `temperature` inference parameter. We proactively drop it for models
        known to reject it, and also fall back to a temperature-free retry if
        any model rejects it at call time.
        """
        if not self.is_ready():
            raise Exception("AWS Bedrock client not initialized")

        kwargs = _strip_unsupported_temperature(kwargs)
        try:
            return self.client.converse(**kwargs)
        except Exception as e:
            if _is_temperature_rejected_error(e) and _has_temperature(kwargs):
                logger.warning(
                    "Model '%s' rejected 'temperature'; retrying without it.",
                    kwargs.get('modelId', '?'),
                )
                return self.client.converse(**_without_temperature(kwargs))
            raise
