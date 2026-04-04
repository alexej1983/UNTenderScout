/**
 * POST /api/analyse
 *
 * Cloudflare Pages Function – replaces the Python FastAPI backend for CF Pages deployments.
 *
 * Environment variables (set in Cloudflare Pages → Settings → Environment Variables,
 * or locally in .dev.vars):
 *   ANTHROPIC_API_KEY  – required, your Anthropic API key
 *
 * Request body (JSON):
 *   { company_url: string, top_n?: number, page_size?: number }
 *
 * Response body (JSON):
 *   { company_name, company_description, company_sectors, total_tenders_checked, matches }
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const UNGM_BASE = "https://www.ungm.org";
const UNGM_NOTICE_URL = `${UNGM_BASE}/Public/Notice`;

const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
  "Accept-Language": "en-US,en;q=0.9",
  Referer: UNGM_NOTICE_URL,
};

const PAGES_TO_CRAWL = [
  "",
  "/about",
  "/about-us",
  "/services",
  "/products",
  "/solutions",
  "/what-we-do",
  "/capabilities",
];

const MAX_PAGE_TEXT = 8_000;   // chars per page
const MAX_TOTAL_TEXT = 20_000; // total chars sent to Claude

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function normaliseDate(value) {
  if (value == null) return null;
  if (typeof value === "number") {
    try {
      return new Date(value).toISOString().slice(0, 10);
    } catch {
      return null;
    }
  }
  if (typeof value === "string") {
    return value.length >= 10 ? value.slice(0, 10) : value;
  }
  return String(value);
}

/**
 * Strip HTML tags and boilerplate elements, returning plain text.
 * Mirrors the BeautifulSoup-based _extract_text() from matcher.py.
 */
function extractText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<nav[\s\S]*?<\/nav>/gi, "")
    .replace(/<footer[\s\S]*?<\/footer>/gi, "")
    .replace(/<header[\s\S]*?<\/header>/gi, "")
    .replace(/<aside[\s\S]*?<\/aside>/gi, "")
    .replace(/<noscript[\s\S]*?<\/noscript>/gi, "")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s{2,}/g, " ")
    .trim();
}

// ---------------------------------------------------------------------------
// UNGM scraper
// ---------------------------------------------------------------------------

function parseJsonNotice(n) {
  const id = String(n.id ?? n.Id ?? n.noticeId ?? n.NoticeId ?? "");
  return {
    id,
    title: n.title ?? n.Title ?? "Untitled",
    description: n.description ?? n.Description ?? n.summary ?? "",
    organization: n.agencyName ?? n.AgencyName ?? n.organization ?? "",
    deadline: normaliseDate(n.deadline ?? n.Deadline ?? n.deadlineDate),
    posted_date: normaliseDate(n.datePosted ?? n.DatePosted ?? n.postedDate),
    url: id ? `${UNGM_NOTICE_URL}/${id}` : UNGM_NOTICE_URL,
    reference: n.reference ?? n.Reference ?? n.noticeNumber ?? "",
    categories: (n.unspscCodes ?? n.UNSPSCCodes ?? [])
      .map((c) => c.description ?? c.Description ?? c)
      .filter(Boolean),
    country: n.country ?? n.Country ?? "",
  };
}

function parseHtmlTenders(html) {
  const tenders = [];
  const seen = new Set();
  const linkRegex = /href="([^"]*\/Public\/Notice\/(\d+)[^"]*)"/gi;
  let match;

  while ((match = linkRegex.exec(html)) !== null) {
    const href = match[1];
    const id = match[2];
    if (seen.has(id)) continue;
    seen.add(id);

    // Grab a snippet of surrounding text to extract a title
    const pos = html.indexOf(match[0]);
    const nearby = html.slice(Math.max(0, pos - 100), pos + 300);
    const titleMatch = nearby.match(/>([^<]{5,200})</);
    const title = titleMatch ? titleMatch[1].trim() : `Notice ${id}`;

    tenders.push({
      id,
      title,
      description: "",
      organization: "",
      deadline: null,
      posted_date: null,
      url: href.startsWith("/") ? `${UNGM_BASE}${href}` : href,
      reference: "",
      categories: [],
      country: "",
    });
  }

  return tenders;
}

async function fetchTenders(pageSize = 50) {
  const payload = {
    pageIndex: 0,
    pageSize,
    sortField: "DatePosted",
    sortOrder: "Descending",
    keyword: "",
    UNSPSCCodes: [],
    AgencyGovId: [],
    StatusId: 1,
    DeadlineDateFrom: null,
    DeadlineDateTo: null,
  };

  // Strategy 1: primary JSON search API
  for (const apiUrl of [
    `${UNGM_BASE}/api/UNNotice/search`,
    `${UNGM_BASE}/Public/Notice/Search`,
  ]) {
    try {
      const resp = await fetch(apiUrl, {
        method: "POST",
        headers: { ...BROWSER_HEADERS, "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) continue;
      const data = await resp.json();
      const notices = Array.isArray(data)
        ? data
        : (data.notices ?? data.Notices ?? data.data ?? data.results ?? data.items ?? []);
      if (notices.length > 0) return notices.map(parseJsonNotice).filter(Boolean);
    } catch {
      // try next strategy
    }
  }

  // Strategy 2: HTML scraping
  try {
    const resp = await fetch(UNGM_NOTICE_URL, {
      headers: { ...BROWSER_HEADERS, Accept: "text/html" },
    });
    if (resp.ok) {
      const html = await resp.text();
      const tenders = parseHtmlTenders(html);
      if (tenders.length > 0) return tenders;
    }
  } catch {
    // fall through
  }

  return [];
}

// ---------------------------------------------------------------------------
// Company analyser
// ---------------------------------------------------------------------------

async function collectWebsiteText(baseUrl) {
  if (!baseUrl.startsWith("http")) baseUrl = "https://" + baseUrl;
  baseUrl = baseUrl.replace(/\/$/, "");

  const collected = [];
  let total = 0;

  for (const path of PAGES_TO_CRAWL) {
    if (total >= MAX_TOTAL_TEXT) break;
    const url = baseUrl + path;
    try {
      const resp = await fetch(url, {
        headers: { ...BROWSER_HEADERS, Accept: "text/html" },
        redirect: "follow",
      });
      if (!resp.ok) continue;
      const html = await resp.text();
      const text = extractText(html);
      if (text) {
        const snippet = text.slice(0, MAX_PAGE_TEXT);
        collected.push(`[Page: ${url}]\n${snippet}`);
        total += snippet.length;
      }
    } catch {
      // skip unreachable pages
    }
  }

  return collected.join("\n\n");
}

async function buildCompanyProfile(url, rawText, apiKey) {
  const content = rawText.trim() || `Company website: ${url}\n(No text content could be retrieved)`;

  const prompt = `You are analysing a company's website content to build a procurement profile.

WEBSITE URL: ${url}

WEBSITE CONTENT:
${content.slice(0, MAX_TOTAL_TEXT)}

Extract the following and reply in JSON (no markdown fences):
{
  "company_name": "...",
  "description": "2-4 sentence summary of what the company does",
  "sectors": ["list", "of", "industry", "sectors"],
  "keywords": ["list", "of", "25-40", "procurement", "relevant", "keywords", "and", "phrases"]
}

Focus keywords on: products/services offered, technical capabilities, industries served, geographic focus.`;

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: "claude-opus-4-6",
      max_tokens: 1024,
      messages: [{ role: "user", content: prompt }],
    }),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Anthropic API error (${resp.status}): ${err}`);
  }

  const data = await resp.json();
  let text = data.content[0].text.trim()
    .replace(/^```[a-z]*\n?/, "")
    .replace(/\n?```$/, "");

  let profile = {};
  try {
    profile = JSON.parse(text);
  } catch {
    // use defaults
  }

  return {
    url,
    name: profile.company_name || url,
    description: profile.description || "",
    sectors: profile.sectors || [],
    keywords: profile.keywords || [],
  };
}

// ---------------------------------------------------------------------------
// Tender matcher
// ---------------------------------------------------------------------------

function buildCatalogue(tenders) {
  return tenders
    .map((t) => {
      const desc = t.description ? t.description.slice(0, 500) : "(no description)";
      const cats = t.categories.slice(0, 5).join(", ");
      return [
        `ID: ${t.id}`,
        `Title: ${t.title}`,
        `Organization: ${t.organization}`,
        `Categories: ${cats}`,
        `Description: ${desc}`,
        `Deadline: ${t.deadline || "N/A"}`,
      ].join("\n");
    })
    .join("\n---\n");
}

async function matchTenders(profile, tenders, topN = 10, apiKey) {
  if (!tenders.length) return [];

  const prompt = `You are a UN procurement specialist. Score how relevant each tender is for the company described below.

COMPANY PROFILE:
Name: ${profile.name}
Description: ${profile.description}
Sectors: ${profile.sectors.join(", ")}
Keywords: ${profile.keywords.join(", ")}

TENDERS:
${buildCatalogue(tenders)}

For EACH tender, reply with a JSON object in this exact array:
[
  {
    "id": "<tender id>",
    "score": <1-10>,
    "rationale": "<1-2 sentence explanation>",
    "matched_keywords": ["keyword1", "keyword2"]
  },
  ...
]

Scoring guide:
9-10 = Strong direct match – company's core offering directly addresses the tender
7-8  = Good match – significant overlap in expertise or sector
5-6  = Moderate match – some relevant capabilities
3-4  = Weak match – tangential relevance
1-2  = Poor match – little to no relevance

Return ONLY the JSON array, no markdown fences.`;

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: "claude-opus-4-6",
      max_tokens: 4096,
      messages: [{ role: "user", content: prompt }],
    }),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Anthropic API error (${resp.status}): ${err}`);
  }

  const data = await resp.json();
  let text = data.content[0].text.trim()
    .replace(/^```[a-z]*\n?/, "")
    .replace(/\n?```$/, "");

  let scores = [];
  try {
    scores = JSON.parse(text);
  } catch {
    // return empty on parse failure
  }

  const tenderMap = Object.fromEntries(tenders.map((t) => [t.id, t]));
  const results = [];

  for (const s of scores) {
    const tid = String(s.id ?? "");
    const tender = tenderMap[tid];
    if (!tender) continue;
    results.push({
      ...tender,
      match_score: parseInt(s.score, 10) || 0,
      match_rationale: s.rationale || "",
      matched_keywords: s.matched_keywords || [],
    });
  }

  results.sort((a, b) => b.match_score - a.match_score);
  return results.slice(0, topN);
}

// ---------------------------------------------------------------------------
// Handler
// ---------------------------------------------------------------------------

export async function onRequestPost(context) {
  const { request, env } = context;

  const apiKey = env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return jsonResponse(
      {
        detail:
          "ANTHROPIC_API_KEY is not configured. " +
          "Go to Cloudflare Pages → your project → Settings → Environment Variables " +
          "and add ANTHROPIC_API_KEY, then redeploy.",
      },
      503
    );
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ detail: "Invalid JSON body" }, 400);
  }

  const { company_url: companyUrl, top_n: topN = 10, page_size: pageSize = 50 } = body;

  if (!companyUrl) {
    return jsonResponse({ detail: "company_url is required" }, 400);
  }

  try {
    // 1. Fetch tenders from UNGM
    const tenders = await fetchTenders(pageSize);
    if (!tenders.length) {
      return jsonResponse(
        {
          detail:
            "Could not retrieve tenders from UNGM. " +
            "The site may be temporarily unavailable. Please try again later.",
        },
        502
      );
    }

    // 2. Analyse company website
    const rawText = await collectWebsiteText(companyUrl);
    const profile = await buildCompanyProfile(companyUrl, rawText, apiKey);

    // 3. Match tenders to company profile
    const matches = await matchTenders(profile, tenders, topN, apiKey);

    return jsonResponse({
      company_name: profile.name,
      company_description: profile.description,
      company_sectors: profile.sectors,
      total_tenders_checked: tenders.length,
      matches,
    });
  } catch (err) {
    console.error("Analysis error:", err);
    return jsonResponse({ detail: err.message || "An unexpected error occurred." }, 500);
  }
}
