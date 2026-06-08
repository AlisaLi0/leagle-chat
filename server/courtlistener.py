"""CourtListener search client (anonymous search works; token raises limits).

A thin async client over the public CourtListener v4 API, reduced to what the
chat backend needs: full-text case-law search returning real opinions with
citations and links. Mirrors the legal-mcp source but standalone (no MCP deps).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

CL_API = "https://www.courtlistener.com/api/rest/v4"
CL_WEB = "https://www.courtlistener.com"
_USER_AGENT = "leagle-chat/0.1 (+https://github.com/AlisaLi0/leagle-chat)"
_RETRY_STATUS = {429, 500, 502, 503, 504}

_SORT_MAP = {
    "relevance": "score desc",
    "newest": "dateFiled desc",
    "oldest": "dateFiled asc",
    "most_cited": "citeCount desc",
}


@dataclass(slots=True)
class Case:
    """A real case-law hit retrieved from CourtListener (never model-generated)."""
    id: str
    title: str
    court: str = ""
    date: str = ""
    citations: list[str] = field(default_factory=list)
    docket_number: str = ""
    cite_count: int = 0
    status: str = ""
    snippet: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "court": self.court,
            "date": self.date,
            "citations": self.citations,
            "docket_number": self.docket_number,
            "cite_count": self.cite_count,
            "status": self.status,
            "snippet": self.snippet,
            "url": self.url,
        }


class CourtListener:
    def __init__(
        self,
        *,
        api_token: str | None = None,
        base_url: str = CL_API,
        timeout: float = 20.0,
        max_retries: int = 4,
    ) -> None:
        self._token = (api_token or "").strip() or None
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries

    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        court: str = "",
        sort_by: str = "relevance",
    ) -> list[Case]:
        q = (query or "").strip()
        if not q:
            return []
        params: dict = {
            "q": q,
            "type": "o",  # opinions / case law
            "order_by": _SORT_MAP.get(sort_by, "score desc"),
        }
        if court.strip():
            params["court"] = court.strip()
        data = await self._get("/search/", params)
        rows = (data.get("results") or [])[: max(1, min(max_results, 20))]
        return [self._parse(r) for r in rows]

    def _parse(self, r: dict) -> Case:
        abs_url = r.get("absolute_url") or ""
        opinions = r.get("opinions") or []
        snippet = ""
        if opinions and isinstance(opinions[0], dict):
            snippet = (opinions[0].get("snippet") or "").strip()
        return Case(
            id=str(r.get("cluster_id") or ""),
            title=r.get("caseName") or r.get("caseNameFull") or "(untitled)",
            court=r.get("court") or "",
            date=(r.get("dateFiled") or "")[:10],
            citations=list(r.get("citation") or []),
            docket_number=r.get("docketNumber") or "",
            cite_count=r.get("citeCount") or 0,
            status=r.get("status") or "",
            snippet=snippet,
            url=f"{CL_WEB}{abs_url}" if abs_url else "",
        )

    # -- HTTP with retry/backoff -------------------------------------------

    def _headers(self) -> dict:
        h = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Token {self._token}"
        return h

    async def _get(self, path: str, params: dict) -> dict:
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True, headers=self._headers()
        ) as client:
            for attempt in range(self._max_retries):
                backoff = min(3.0 * (attempt + 1), 12.0)
                try:
                    resp = await client.get(url, params=params)
                except httpx.TransportError as exc:
                    last_exc = exc
                    await asyncio.sleep(backoff)
                    continue
                if resp.status_code == 404:
                    return {}
                if resp.status_code in (401, 403):
                    # Search is anonymous; detail endpoints need a token.
                    return {}
                if resp.status_code in _RETRY_STATUS:
                    last_exc = httpx.HTTPStatusError(
                        f"CourtListener {resp.status_code}",
                        request=resp.request, response=resp)
                    await asyncio.sleep(backoff)
                    continue
                resp.raise_for_status()
                return resp.json()
        if last_exc:
            raise last_exc
        return {}
