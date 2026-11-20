# litellm compat shim for RD-Agent factor mining on the Python 3.10 base image.
#
# Mounts into: /usr/local/lib/python3.10/site-packages/sitecustomize.py
# (see docker-compose.yml, quantmind.volumes). Loaded automatically by every
# python process at startup, before any user code imports litellm. Each block
# below is a harmless no-op when its underlying issue is absent.
#
# (1) typing backport -- litellm>=1.98 does `from typing import NotRequired`
#     (PEP 655, Python 3.11+). The container base is python:3.10-slim, so the
#     import raises ImportError and RDLoop.mount() dies at step 1. Backfill the
#     3.11-only typing names from typing_extensions before litellm is imported.
#     Root cause is also pinned in rd-agent/requirements.txt (litellm<1.98).
import os as _os

# avoid a network fetch on every python process (offline servers otherwise
# stall 20-30s at import; 2026-11-16 portable install test)
_os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
try:
    import typing as _typing

    import typing_extensions as _te

    for _name in (
        "NotRequired",
        "Required",
        "Self",
        "Never",
        "LiteralString",
        "assert_never",
        "assert_type",
        "TypeVarTuple",
        "Unpack",
        "dataclass_transform",
    ):
        if not hasattr(_typing, _name) and hasattr(_te, _name):
            setattr(_typing, _name, getattr(_te, _name))
except Exception:
    pass

# (2) litellm 1.97.x + pydantic 2.13 -- litellm.types.utils.Message references
#     ChatCompletionReasoningSummaryTextBlock, which pydantic 2.13 tries to
#     resolve in utils.py's namespace where the name is absent ->
#     PydanticUndefinedAnnotation ("Message is not fully defined"). Inject the
#     name from llms.openai where it is actually defined, then rebuild.
#
# Only import litellm for processes that need it. Utility scripts (pg_setup,
# health probes) set QM_SKIP_LITELLM_SHIM=1; otherwise every `python -c` on a
# server without internet pays the import/network cost at startup.
if _os.environ.get("QM_SKIP_LITELLM_SHIM") != "1":
    try:
        import litellm.types.utils as _U
        from litellm.types.llms.openai import ChatCompletionReasoningSummaryTextBlock

        _U.ChatCompletionReasoningSummaryTextBlock = (
            ChatCompletionReasoningSummaryTextBlock
        )
        _U.Message.model_rebuild()
    except Exception:
        pass