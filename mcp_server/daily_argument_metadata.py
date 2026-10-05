"""Keep daily input literal until its bounded, receipt-aware facade parses it."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata


class DailyArgumentMetadata(FuncMetadata):
    """Disable only the SDK's opportunistic JSON decoding for remember_memory.

    The actual argument model still validates every field. The daily facade owns
    compatible keyword decoding, original length limits and receipt matching;
    converting a JSON string here would bypass those boundaries.
    """

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        return dict(data)


def literal_daily_argument_metadata(metadata: FuncMetadata) -> DailyArgumentMetadata:
    """Preserve the already-built models and output metadata by identity."""
    return DailyArgumentMetadata.model_construct(
        arg_model=metadata.arg_model,
        output_schema=metadata.output_schema,
        output_model=metadata.output_model,
        wrap_output=metadata.wrap_output,
    )
