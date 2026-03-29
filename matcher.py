"""
Company website analyzer and Claude-powered tender matcher.
"""
import asyncio
import logging
from dataclasses import dataclass

import httpx
from anthropic import AsyncAnthropic
from bs4 import BeautifulSoup

from scraper import Tender

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Pages to crawl within the company website for richer profile data
PAGES_TO_CHECK = [
    "",           # homepage
    "/about",
    "/about-us",
    "/services",
    "/products",
    "/solutions",
    "/what-we-do",
    "/capabilities",
]

MAX_PAGE_TEXT = 8000   # characters per page
MAX_TOTAL_TEXT = 20000  # total characters fed to Claude for profiling


@dataclass
class CompanyProfile:
    url: str
    name: str
    description: str
    sectors: list[str]
    keywords: list[str]
    raw_text: str


@dataclass
class MatchResult:
    tender: Tender
    score: int          # 1-10
    rationale: str
    matched_keywords: list[str]

    def to_dict(self) -> dict:
        return {
            **self.tender.to_dict(),
            "match_score": self.score,
            "match_rationale": self.rationale,
            "matched_keywords": self.matched_keywords,
        }


class CompanyAnalyzer:
    """Fetches and analyses a company website to build a business profile."""

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    async def build_profile(self, url: str, client: AsyncAnthropic) -> CompanyProfile:
        raw_text = await self._collect_website_text(url)
        return await self._extract_profile_with_claude(url, raw_text, client)

    # ------------------------------------------------------------------

    async def _collect_website_text(self, base_url: str) -> str:
        base_url = base_url.rstrip("/")
        if not base_url.startswith("http"):
            base_url = "https://" + base_url

        collected: list[str] = []
        total = 0

        async with httpx.AsyncClient(
            headers=HEADERS, timeout=self.timeout, follow_redirects=True
        ) as http:
            for path in PAGES_TO_CHECK:
                if total >= MAX_TOTAL_TEXT:
                    break
                url = base_url + path
                try:
                    resp = await http.get(url)
                    if resp.status_code != 200:
                        continue
                    text = _extract_text(resp.text)
                    if text:
                        snippet = text[: MAX_PAGE_TEXT - total]
                        collected.append(f"[Page: {url}]\n{snippet}")
                        total += len(snippet)
                except Exception as exc:
                    logger.debug("Could not fetch %s: %s", url, exc)

        return "\n\n".join(collected)

    async def _extract_profile_with_claude(
        self, url: str, raw_text: str, client: AsyncAnthropic
    ) -> CompanyProfile:
        if not raw_text.strip():
            raw_text = f"Company website: {url}\n(No text content could be retrieved)"

        prompt = f"""You are analysing a company's website content to build a procurement profile.

WEBSITE URL: {url}

WEBSITE CONTENT:
{raw_text[:MAX_TOTAL_TEXT]}

Extract the following and reply in JSON (no markdown fences):
{{
  "company_name": "...",
  "description": "2-4 sentence summary of what the company does",
  "sectors": ["list", "of", "industry", "sectors"],
  "keywords": ["list", "of", "25-40", "procurement", "relevant", "keywords", "and", "phrases"]
}}

Focus keywords on: products/services offered, technical capabilities, industries served, geographic focus."""

        msg = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        import json, re

        text = msg.content[0].text.strip()
        # Strip any accidental markdown fences
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Claude returned non-JSON profile; using defaults")
            data = {}

        return CompanyProfile(
            url=url,
            name=data.get("company_name", url),
            description=data.get("description", ""),
            sectors=data.get("sectors", []),
            keywords=data.get("keywords", []),
            raw_text=raw_text,
        )


class TenderMatcher:
    """Uses Claude to score how relevant each tender is for a company."""

    def __init__(self, client: AsyncAnthropic):
        self.client = client

    async def match(
        self, profile: CompanyProfile, tenders: list[Tender], top_n: int = 10
    ) -> list[MatchResult]:
        if not tenders:
            return []

        # Build a compact tender catalogue to send to Claude in one request
        catalogue = self._build_catalogue(tenders)

        prompt = f"""You are a UN procurement specialist. Score how relevant each tender is for the company described below.

COMPANY PROFILE:
Name: {profile.name}
Description: {profile.description}
Sectors: {', '.join(profile.sectors)}
Keywords: {', '.join(profile.keywords)}

TENDERS:
{catalogue}

For EACH tender, reply with a JSON object in this exact array:
[
  {{
    "id": "<tender id>",
    "score": <1-10>,
    "rationale": "<1-2 sentence explanation>",
    "matched_keywords": ["keyword1", "keyword2"]
  }},
  ...
]

Scoring guide:
9-10 = Strong direct match – company's core offering directly addresses the tender
7-8  = Good match – significant overlap in expertise or sector
5-6  = Moderate match – some relevant capabilities
3-4  = Weak match – tangential relevance
1-2  = Poor match – little to no relevance

Return ONLY the JSON array, no markdown fences."""

        msg = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        import json, re

        text = msg.content[0].text.strip()
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

        try:
            scores = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Claude returned non-JSON scores")
            scores = []

        # Build id -> Tender map
        tender_map = {t.id: t for t in tenders}
        results: list[MatchResult] = []

        for s in scores:
            tid = str(s.get("id", ""))
            tender = tender_map.get(tid)
            if not tender:
                continue
            results.append(
                MatchResult(
                    tender=tender,
                    score=int(s.get("score", 0)),
                    rationale=s.get("rationale", ""),
                    matched_keywords=s.get("matched_keywords", []),
                )
            )

        # Sort by score descending, return top N
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_n]

    def _build_catalogue(self, tenders: list[Tender]) -> str:
        lines = []
        for t in tenders:
            desc = t.description[:500] if t.description else "(no description)"
            cats = ", ".join(t.categories[:5]) if t.categories else ""
            lines.append(
                f"ID: {t.id}\n"
                f"Title: {t.title}\n"
                f"Organization: {t.organization}\n"
                f"Categories: {cats}\n"
                f"Description: {desc}\n"
                f"Deadline: {t.deadline or 'N/A'}\n"
            )
        return "\n---\n".join(lines)


def _extract_text(html: str) -> str:
    """Extract clean readable text from HTML."""
    soup = BeautifulSoup(html, "lxml")
    # Remove boilerplate elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    # Collapse whitespace
    import re
    text = re.sub(r"\s{2,}", " ", text)
    return text
