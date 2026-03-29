"""
UNTenderScout – FastAPI backend
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl

from matcher import CompanyAnalyzer, TenderMatcher
from scraper import UNGMScraper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not ANTHROPIC_API_KEY:
        logger.warning(
            "ANTHROPIC_API_KEY is not set. Matching will fail without it."
        )
    yield


app = FastAPI(
    title="UNTenderScout",
    description="Match UN procurement tenders to your company profile",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AnalyseRequest(BaseModel):
    company_url: str
    top_n: int = 10
    page_size: int = 50


class AnalyseResponse(BaseModel):
    company_name: str
    company_description: str
    company_sectors: list[str]
    total_tenders_checked: int
    matches: list[dict]


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.post("/api/analyse", response_model=AnalyseResponse)
async def analyse(req: AnalyseRequest):
    """
    1. Scrape open tenders from UNGM
    2. Analyse the company website
    3. Match tenders to company profile using Claude
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured. Please add it to the .env file.",
        )

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # 1. Fetch tenders
    scraper = UNGMScraper()
    logger.info("Fetching UNGM tenders (page_size=%d)…", req.page_size)
    tenders = await scraper.fetch_tenders(page_size=req.page_size)

    if not tenders:
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not retrieve tenders from UNGM. The site may be temporarily "
                "unavailable or may require JavaScript rendering. Please try again later."
            ),
        )

    logger.info("Fetched %d tenders", len(tenders))

    # 2. Analyse company website
    analyzer = CompanyAnalyzer()
    logger.info("Analysing company website: %s", req.company_url)
    try:
        profile = await analyzer.build_profile(req.company_url, client)
    except Exception as exc:
        logger.error("Company analysis failed: %s", exc)
        raise HTTPException(
            status_code=422,
            detail=f"Could not analyse company website: {exc}",
        )

    logger.info("Company profile built for: %s", profile.name)

    # 3. Match tenders
    matcher = TenderMatcher(client)
    logger.info("Matching %d tenders against company profile…", len(tenders))
    try:
        results = await matcher.match(profile, tenders, top_n=req.top_n)
    except Exception as exc:
        logger.error("Matching failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Tender matching failed: {exc}",
        )

    logger.info("Returning %d matches", len(results))

    return AnalyseResponse(
        company_name=profile.name,
        company_description=profile.description,
        company_sectors=profile.sectors,
        total_tenders_checked=len(tenders),
        matches=[r.to_dict() for r in results],
    )


@app.get("/api/health")
async def health():
    return {"status": "ok", "api_key_configured": bool(ANTHROPIC_API_KEY)}


# ---------------------------------------------------------------------------
# Serve the frontend
# ---------------------------------------------------------------------------

# Mount static dir if it exists (JS, CSS assets)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=True,
    )
