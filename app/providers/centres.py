import json
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.config import Settings
from app.core.errors import DomainError


class Place(BaseModel):
    id: str
    name: str
    description: str = ""


class Node(BaseModel):
    id: str
    place_id: str


class Edge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    source: str = Field(alias="from")
    to: str
    distance_m: float = Field(ge=0, allow_inf_nan=False)
    accessible: bool = True


class Graph(BaseModel):
    nodes: list[Node]
    edges: list[Edge]


class CenterContext(BaseModel):
    schema_version: str
    center_id: str
    name: str
    updated_at: str
    places: list[Place]
    graph: Graph

    @model_validator(mode="after")
    def references_exist(self):
        places = {place.id for place in self.places}
        nodes = {node.id for node in self.graph.nodes}
        if len(places) != len(self.places) or len(nodes) != len(self.graph.nodes):
            raise ValueError("Duplicate graph/place identifiers")
        if any(node.place_id not in places for node in self.graph.nodes):
            raise ValueError("Unknown place reference")
        if any(edge.source not in nodes or edge.to not in nodes for edge in self.graph.edges):
            raise ValueError("Unknown graph node reference")
        return self


class CenterProvider(Protocol):
    async def context(self, center_id: str) -> dict: ...


class ContextProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def context(self, center_id: str) -> dict:
        try:
            if self.settings.centres_provider == "mock":
                # Controller restricts identifiers to letters, digits, underscore and hyphen.
                path = self.settings.fixtures_dir / "centres" / f"{center_id}.json"
                if not path.is_file():
                    raise DomainError(
                        404, "center_not_found", "Centre inconnu dans les données de démonstration."
                    )
                raw = json.loads(path.read_text())
            else:
                headers = (
                    {"Authorization": f"Bearer {self.settings.centres_api_key}"}
                    if self.settings.centres_api_key
                    else {}
                )
                async with httpx.AsyncClient(
                    timeout=self.settings.centres_timeout_seconds, follow_redirects=False
                ) as client:
                    response = await client.get(
                        f"{self.settings.centres_base_url.rstrip('/')}/api/v1/centres/{quote(center_id, safe='')}/context",
                        headers=headers,
                    )
                    if response.status_code == 404:
                        raise DomainError(404, "center_not_found", "Centre inconnu.")
                    response.raise_for_status()
                    raw = response.json()
            context = CenterContext.model_validate(raw)
            if context.center_id != center_id or context.schema_version != "1.0":
                raise ValueError("Mismatched center/version")
            return context.model_dump(by_alias=True)
        except (httpx.HTTPError, ValidationError, ValueError, OSError) as exc:
            raise DomainError(
                503, "center_unavailable", "Le contexte du centre est indisponible ou invalide."
            ) from exc
