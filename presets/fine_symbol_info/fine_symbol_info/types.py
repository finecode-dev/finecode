from __future__ import annotations

import dataclasses

from finecode_extension_api.resource_uri import ResourceUri
from finecode_extension_api.common_types import Range


@dataclasses.dataclass
class Location:
    uri: ResourceUri
    range: Range
