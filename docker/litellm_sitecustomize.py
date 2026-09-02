# litellm compat shim (RD-Agent factor mining).
#
# Part 1 - Python 3.10 typing compat:
#   litellm 1.98.0+ imports NotRequired from `typing` at import time
#   (a 3.11+ stdlib name, broken even though litellm declares Requires-Python
#   >=3.10). Inject the missing names from typing_extensions so the import of
#   litellm does not crash. Root fix lives in rd-agent/requirements.txt
#   (litellm<1.98); this shim keeps already-installed 1.98/1.99 containers
#   working without a rebuild and guards against future pip drift. No-op on
#   3.11+ (the names already exist) and once upstream fixes their imports.
#
# Part 2 - pydantic 2.13 compat:
#   litellm.types.utils.Message references ChatCompletionReasoningSummaryTextBlock,
#   which pydantic 2.13 tries to resolve in utils.py's namespace where the name is
#   absent -> PydanticUndefinedAnnotation ("Message is not fully defined").
#   Inject the name from llms.openai where it is actually defined, then rebuild.
#
# Mounts into: /usr/local/lib/python3.10/site-packages/sitecustomize.py
# (see docker-compose.yml, quantmind.volumes). Loaded automatically by every
# python process at startup; harmless no-op if the structure changes upstream.

import sys

if sys.version_info < (3, 11):
    try:
        from typing_extensions import NotRequired, Required

        import typing as _typing

        _typing.NotRequired = NotRequired
        _typing.Required = Required
    except Exception:
        pass

try:
    import litellm.types.utils as _U
    from litellm.types.llms.openai import ChatCompletionReasoningSummaryTextBlock

    _U.ChatCompletionReasoningSummaryTextBlock = ChatCompletionReasoningSummaryTextBlock
    _U.Message.model_rebuild()
except Exception:
    pass