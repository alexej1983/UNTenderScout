"""
UNGM (UN Global Marketplace) tender scraper.
Fetches public procurement notices from https://www.ungm.org/Public/Notice
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

UNGM_BASE = "https://www.ungm.org"
UNGM_NOTICE_URL = f"{UNGM_BASE}/Public/Notice"

# Search API used by the UNGM Angular frontend
UNGM_SEARCH_URL = f"{UNGM_BASE}/Public/Notice"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": UNGM_NOTICE_URL,
}

# JSON payload to the undocumented UNGM search API
SEARCH_PAYLOAD = {
    "pageIndex": 0,
    "pageSize": 50,
    "sortField": "DatePosted",
    "sortOrder": "Descending",
    "keyword": "",
    "UNSPSCCodes": [],
    "AgencyGovId": [],
    "StatusId": 1,   # 1 = Active/Open
    "DeadlineDateFrom": None,
    "DeadlineDateTo": None,
}


@dataclass
class Tender:
    id: str
    title: str
    description: str
    organization: str
    deadline: Optional[str]
    posted_date: Optional[str]
    url: str
    reference: str = ""
    categories: list[str] = field(default_factory=list)
    country: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "organization": self.organization,
            "deadline": self.deadline,
            "posted_date": self.posted_date,
            "url": self.url,
            "reference": self.reference,
            "categories": self.categories,
            "country": self.country,
        }


class UNGMScraper:
    """Fetches open procurement notices from the UN Global Marketplace."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    async def fetch_tenders(self, page_size: int = 50) -> list[Tender]:
        """Return a list of open tenders."""
        tenders: list[Tender] = []

        async with httpx.AsyncClient(
            headers=HEADERS, timeout=self.timeout, follow_redirects=True
        ) as client:
            # Strategy 1: try the JSON search API
            try:
                tenders = await self._fetch_via_json_api(client, page_size)
                if tenders:
                    logger.info("Fetched %d tenders via JSON API", len(tenders))
                    return tenders
            except Exception as exc:
                logger.warning("JSON API strategy failed: %s", exc)

            # Strategy 2: parse the HTML listing page
            try:
                tenders = await self._fetch_via_html(client)
                if tenders:
                    logger.info("Fetched %d tenders via HTML scrape", len(tenders))
                    return tenders
            except Exception as exc:
                logger.warning("HTML scrape strategy failed: %s", exc)

        logger.error("All scraping strategies failed")
        return tenders

    # ------------------------------------------------------------------
    # Strategy 1 – JSON API (Angular XHR endpoint)
    # ------------------------------------------------------------------
    async def _fetch_via_json_api(
        self, client: httpx.AsyncClient, page_size: int
    ) -> list[Tender]:
        payload = {**SEARCH_PAYLOAD, "pageSize": page_size}

        # UNGM uses a POST endpoint that returns JSON
        api_url = f"{UNGM_BASE}/api/UNNotice/search"
        resp = await client.post(
            api_url,
            json=payload,
            headers={**HEADERS, "Content-Type": "application/json"},
        )

        if resp.status_code != 200:
            # Try alternative endpoint patterns
            api_url = f"{UNGM_BASE}/Public/Notice/Search"
            resp = await client.post(
                api_url,
                json=payload,
                headers={**HEADERS, "Content-Type": "application/json"},
            )

        resp.raise_for_status()
        data = resp.json()

        # Handle various response shapes
        notices = []
        if isinstance(data, list):
            notices = data
        elif isinstance(data, dict):
            notices = (
                data.get("notices")
                or data.get("Notices")
                or data.get("data")
                or data.get("results")
                or data.get("items")
                or []
            )

        return [self._parse_json_notice(n) for n in notices if n]

    def _parse_json_notice(self, n: dict) -> Tender:
        notice_id = str(
            n.get("id") or n.get("Id") or n.get("noticeId") or n.get("NoticeId") or ""
        )
        return Tender(
            id=notice_id,
            title=n.get("title") or n.get("Title") or "Untitled",
            description=n.get("description") or n.get("Description") or n.get("summary") or "",
            organization=n.get("agencyName") or n.get("AgencyName") or n.get("organization") or "",
            deadline=_normalise_date(n.get("deadline") or n.get("Deadline") or n.get("deadlineDate")),
            posted_date=_normalise_date(n.get("datePosted") or n.get("DatePosted") or n.get("postedDate")),
            url=f"{UNGM_NOTICE_URL}/{notice_id}" if notice_id else UNGM_NOTICE_URL,
            reference=n.get("reference") or n.get("Reference") or n.get("noticeNumber") or "",
            categories=[
                c.get("description") or c.get("Description") or c
                for c in (n.get("unspscCodes") or n.get("UNSPSCCodes") or [])
                if c
            ],
            country=n.get("country") or n.get("Country") or "",
        )

    # ------------------------------------------------------------------
    # Strategy 2 – HTML scraping
    # ------------------------------------------------------------------
    async def _fetch_via_html(self, client: httpx.AsyncClient) -> list[Tender]:
        resp = await client.get(UNGM_NOTICE_URL, headers={**HEADERS, "Accept": "text/html"})
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        tenders: list[Tender] = []

        # Try common table / list structures found on procurement portals
        rows = (
            soup.select("table#noticeTable tbody tr")
            or soup.select("table.notice-table tbody tr")
            or soup.select("tr.notice-row")
            or soup.select("div.notice-item")
            or soup.select("div.tender-item")
            or soup.select("li.notice")
        )

        for row in rows:
            tender = self._parse_html_row(row)
            if tender:
                tenders.append(tender)

        # Fallback: look for any <a> tags pointing to notice URLs
        if not tenders:
            tenders = self._fallback_link_parse(soup)

        return tenders

    def _parse_html_row(self, row) -> Optional[Tender]:
        try:
            link = row.find("a", href=True)
            if not link:
                return None

            href = link["href"]
            notice_id = href.rstrip("/").split("/")[-1]
            if not notice_id.isdigit():
                return None

            cells = row.find_all(["td", "div"])
            texts = [c.get_text(strip=True) for c in cells]

            title = link.get_text(strip=True) or (texts[0] if texts else "Untitled")
            organization = texts[1] if len(texts) > 1 else ""
            deadline = texts[2] if len(texts) > 2 else None

            return Tender(
                id=notice_id,
                title=title,
                description="",
                organization=organization,
                deadline=deadline,
                posted_date=None,
                url=f"{UNGM_BASE}{href}" if href.startswith("/") else href,
            )
        except Exception:
            return None

    def _fallback_link_parse(self, soup: BeautifulSoup) -> list[Tender]:
        tenders = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/Public/Notice/" in href:
                notice_id = href.rstrip("/").split("/")[-1]
                if notice_id.isdigit():
                    tenders.append(
                        Tender(
                            id=notice_id,
                            title=a.get_text(strip=True) or f"Notice {notice_id}",
                            description="",
                            organization="",
                            deadline=None,
                            posted_date=None,
                            url=f"{UNGM_BASE}{href}" if href.startswith("/") else href,
                        )
                    )
        return tenders

    # ------------------------------------------------------------------
    # Fetch individual tender detail page to enrich description
    # ------------------------------------------------------------------
    async def enrich_tender(self, tender: Tender) -> Tender:
        """Fetch individual notice page to get full description."""
        if tender.description:
            return tender

        async with httpx.AsyncClient(
            headers={**HEADERS, "Accept": "text/html"},
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            try:
                resp = await client.get(tender.url)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "lxml")

                # Try common selectors for description text
                desc_el = (
                    soup.select_one("div.notice-description")
                    or soup.select_one("div#description")
                    or soup.select_one("div.description")
                    or soup.select_one("section.tender-details")
                    or soup.select_one("div.tender-description")
                    or soup.select_one("main")
                )
                if desc_el:
                    tender.description = desc_el.get_text(separator=" ", strip=True)[:3000]
            except Exception as exc:
                logger.debug("Could not enrich tender %s: %s", tender.id, exc)

        return tender


def _normalise_date(value) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        # Unix timestamp in milliseconds
        try:
            return datetime.utcfromtimestamp(value / 1000).strftime("%Y-%m-%d")
        except Exception:
            pass
    if isinstance(value, str):
        # Already a string – return as-is (might be ISO or formatted)
        return value[:10] if len(value) >= 10 else value
    return str(value)
