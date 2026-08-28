// analyze-property — on-demand AI due-diligence research for ONE Florida
// tax-deed parcel. Invoked from the dashboard with the property's feed record;
// runs a bounded OpenAI Responses tool-use loop and writes a structured,
// source-cited result into public.parcel_analysis. Shared + cached, so a parcel
// is researched (and paid for) at most once per TTL window.
//
// The agent's most important job is LIENS. Its tools let it: resolve the
// parcel's clerk tax-deed case file live (get_case_documents), read the
// Ownership & Encumbrance / title report PDF inside it (read_pdf) — that report
// is the county's own list of recorded mortgages, judgments, IRS liens and
// encumbrances — search the web, read HTML pages (fetch_page), and locate the
// county Official Records search (official_records_search).
//
// Dormant with no OPENAI_API_KEY / ANALYSIS_MODEL secret. Auth is enforced
// in-function; deployed with verify_jwt disabled.

import { createClient } from "jsr:@supabase/supabase-js@2";

const OPENAI_API_KEY = Deno.env.get("OPENAI_API_KEY") ?? "";
const ANALYSIS_MODEL = Deno.env.get("ANALYSIS_MODEL") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";

const OPENAI_URL = "https://api.openai.com/v1/responses";
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TaxDeedRadar/1.0";

const TTL_DAYS = 30;        // a cached "ready" analysis is reused this long
const DAILY_CAP = 25;       // new analyses per team per day (spend guard)
const MAX_STEPS = 8;        // bounded agent loop
const MAX_TOKENS = 4500;
const MAX_PDF_CHARS = 16000;
const MAX_PAGE_CHARS = 12000;
const MAX_DOCS = 12;
const PENDING_STALE_MS = 3 * 60 * 1000;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

  if (!OPENAI_API_KEY || !ANALYSIS_MODEL) {
    return json({ status: "disabled", reason: "AI analysis not configured" });
  }

  // --- Auth: require a real signed-in user (custom check; verify_jwt is off). ---
  const authHeader = req.headers.get("Authorization") ?? "";
  const token = authHeader.replace(/^Bearer\s+/i, "").trim();
  if (!token) return json({ status: "unauthorized", reason: "sign in required" }, 401);
  const userClient = createClient(SUPABASE_URL, ANON_KEY, {
    global: { headers: { Authorization: `Bearer ${token}` } },
  });
  const { data: userData } = await userClient.auth.getUser();
  const user = userData?.user;
  if (!user) return json({ status: "unauthorized", reason: "sign in required" }, 401);

  const svc = createClient(SUPABASE_URL, SERVICE_ROLE);

  let body: any;
  try { body = await req.json(); } catch { return json({ error: "bad json" }, 400); }
  const record = body?.record ?? {};
  const refresh = !!body?.refresh;
  const county = String(record.county ?? "").trim();
  const parcelId = String(record.parcel_id ?? "").trim();
  const caseNumber = String(record.case_number ?? "").trim();
  if (!county || !parcelId) return json({ error: "record.county and record.parcel_id required" }, 400);
  const pkey = `${county}|${parcelId}|${caseNumber}`;

  // --- Cache-first ---
  const { data: existing } = await svc.from("parcel_analysis").select("*").eq("pkey", pkey).maybeSingle();
  if (existing && !refresh) {
    const ageMs = Date.now() - new Date(existing.updated_at ?? existing.created_at).getTime();
    if (existing.status === "ready" && ageMs < TTL_DAYS * 864e5) {
      return json({ status: "ready", pkey, cached: true, data: existing.data, generated_at: existing.updated_at });
    }
    if (existing.status === "pending" && ageMs < PENDING_STALE_MS) return json({ status: "pending", pkey });
  }

  // --- Per-team daily cap (spend guard) ---
  const { data: profile } = await svc.from("profiles").select("team_id").eq("id", user.id).maybeSingle();
  const teamId = profile?.team_id ?? "no-team";
  const today = new Date().toISOString().slice(0, 10);
  const { data: usage } = await svc.from("analysis_usage")
    .select("count").eq("team_id", teamId).eq("day", today).maybeSingle();
  if ((usage?.count ?? 0) >= DAILY_CAP) {
    return json({ status: "capped", reason: `Daily research limit reached (${DAILY_CAP}/day). Try again tomorrow.` });
  }

  await svc.from("parcel_analysis").upsert({
    pkey, status: "pending", model: ANALYSIS_MODEL, created_by: user.id, updated_at: new Date().toISOString(),
  }, { onConflict: "pkey" });
  await svc.from("analysis_usage").upsert(
    { team_id: teamId, day: today, count: (usage?.count ?? 0) + 1 }, { onConflict: "team_id,day" });

  const work = runAgent(record, pkey, svc).catch(async (e) => {
    await svc.from("parcel_analysis").update({
      status: "error", data: { error: String(e).slice(0, 500) }, updated_at: new Date().toISOString(),
    }).eq("pkey", pkey);
  });
  // @ts-ignore EdgeRuntime is provided by the Supabase runtime.
  if (typeof EdgeRuntime !== "undefined") EdgeRuntime.waitUntil(work); else await work;

  return json({ status: "pending", pkey });
});

// ---------------------------------------------------------------------------

const SYSTEM = `You are a tax-deed due-diligence researcher analyzing ONE specific Florida parcel for an investor deciding whether to bid at a tax-deed auction.

LIENS ARE THE MOST IMPORTANT PART. Work the lien question hard and concretely — do not give up after one tool call:
1. Get the parcel's clerk tax-deed case file. Call get_case_documents first. If it doesn't return the documents, use fetch_page on the county clerk tax-deed FILE search portal named in the facts to navigate to this parcel's file, and web_search for the parcel's tax-deed file — each county keeps it on a different system (e.g. Volusia at app02.clerk.org/or_td/). You may read any Florida county clerk, official-records or appraiser document or page you find with read_pdf / fetch_page.
2. Read the Ownership & Encumbrance (O&E) / title / current-owner-search report with read_pdf — that report is the county's own list of every recorded mortgage, judgment, IRS/federal tax lien, HOA claim and encumbrance on this parcel. Read the other case documents too if useful.
3. Call official_records_search to get where the county's Official Records live, and search the web, to corroborate or fill gaps.
Only conclude "O&E not found" after you have actually tried get_case_documents, the county file-search portal, and a web search. Some Florida counties are IN-PERSON (the facts will say so): their O&E is a physical courthouse file, not online — in that case report that the O&E must be requested from the clerk and was not available online, and do NOT state there are no liens. Under Florida law a tax-deed sale extinguishes MOST private liens and mortgages (they are junior to the tax lien), but these SURVIVE the sale and are the real risk: IRS / federal tax liens, municipal & code-enforcement liens, other governmental liens and special assessments, certain easements, and title-marketability defects (a quiet-title action is usually needed). Call these out specifically.

Ground every claim in a source you actually retrieved. When you cannot find something, say "not found" — never invent a lien, a value, an owner, or a clean bill of health. If you could not read the O&E, say so and tell the investor to pull it from the clerk.

Also cover: opening bid vs assessed value and any comparable/nearby sales; the site itself (land use/zoning, access, flood zone, wetlands); clear red flags; genuine opportunities; concrete next steps.

When done, call emit_analysis EXACTLY ONCE. This is research assistance to speed due diligence — it is NOT legal or title advice; say so in your summary.`;

const EMIT_TOOL = {
  type: "function",
  name: "emit_analysis",
  description: "Return the final structured due-diligence analysis for this parcel. Call exactly once, at the end.",
  parameters: {
    type: "object",
    properties: {
      summary: { type: "string", description: "2-4 sentence plain-English read of the property and the opportunity/risk. Include the not-legal-advice caveat." },
      value_read: { type: "string", description: "Opening bid vs assessed value vs any comparables found." },
      title_and_liens: { type: "string", description: "What the O&E/title report and records actually show: recorded mortgages, judgments, IRS/federal tax liens, municipal/code liens, special assessments, lis pendens, HOA, easements — and which survive the tax deed. 'not found' / 'could not read the O&E' if so." },
      site_and_environment: { type: "string", description: "Land use/zoning, access, flood zone, wetlands, environmental notes." },
      red_flags: { type: "array", items: { type: "string" } },
      opportunities: { type: "array", items: { type: "string" } },
      suggested_next_steps: { type: "array", items: { type: "string" } },
      sources: {
        type: "array",
        description: "Every source used, each with the URL and what it supported.",
        items: { type: "object", properties: { url: { type: "string" }, supports: { type: "string" } }, required: ["url", "supports"] },
      },
      confidence: { type: "string", enum: ["low", "medium", "high"] },
    },
    required: ["summary", "value_read", "title_and_liens", "site_and_environment",
      "red_flags", "opportunities", "suggested_next_steps", "sources", "confidence"],
  },
};
const GET_CASE_DOCS_TOOL = {
  type: "function",
  name: "get_case_documents",
  description: "Resolve THIS parcel's clerk tax-deed case file and return its document list (name + url), including any Ownership & Encumbrance / title report. Call when case documents weren't already provided.",
  parameters: { type: "object", properties: {}, required: [] },
};
const READ_PDF_TOOL = {
  type: "function",
  name: "read_pdf",
  description: "Read the text of a document PDF by URL — the parcel's case documents, or any Florida county clerk / official-records / appraiser document (e.g. the O&E / title report, wherever the county keeps it).",
  parameters: { type: "object", properties: { url: { type: "string" } }, required: ["url"] },
};
const FETCH_PAGE_TOOL = {
  type: "function",
  name: "fetch_page",
  description: "Fetch an HTML page and return its readable text — the county appraiser page, the clerk case page, or the county tax-deed file-search portal (any Florida county clerk/records/appraiser site).",
  parameters: { type: "object", properties: { url: { type: "string" } }, required: ["url"] },
};
const OFFICIAL_RECORDS_TOOL = {
  type: "function",
  name: "official_records_search",
  description: "Get where to search this county's Official Records for recorded liens/mortgages/judgments against the owner. Returns the county's official-records search URL and guidance.",
  parameters: { type: "object", properties: { owner_name: { type: "string" } }, required: [] },
};
const WEB_SEARCH_TOOL = { type: "web_search" };

// County Official Records search entry points. Where we don't have a verified
// portal, the agent still gets a working path via a search fallback.
const OR_REGISTRY: Record<string, string> = {
  hillsborough: "https://publicaccess.hillsclerk.com/oripublicaccess/",
};

// Per-county tax-deed file portal / clerk tax-deed page (from
// config/clerk_sites.yaml + config/florida_counties.json). This is where each
// county keeps the case file + Ownership & Encumbrance report; the systems
// differ per county (RealTDM, TaxSmart, Landmark, clerk.org, …), and the
// in-person counties keep it only as a physical courthouse file.
const CLERK_SEARCH: Record<string, string> = {"alachua":"https://alachuacounty.us/Depts/Clerk/TaxDeeds/pages/taxdeedsales.aspx","bay":"http://records2.baycoclerk.com/TaxDeed/","bradford":"https://bradfordclerk.com/tax-deeds-and-foreclosure-sales/","brevard":"https://www.brevardclerk.us/tax-deed-sales","broward":"https://www.broward.org/RecordsTaxesTreasury/","citrus":"https://search.citrusclerk.org/TaxSmartWeb","clay":"https://landmark.clayclerk.com/TaxDeed/","collier":"https://www.collierclerk.com/tax-deed-sales/search-upcoming-sales-list/","columbia":"https://columbiaclerk.com/clerk-services/tax-deeds/upcoming-tax-deed-sales/","desoto":"https://www.desotoclerk.com/public-sales/tax-deeds/","dixie":"https://dixieclerk.com/departments-services/court-services/tax-deed-sales/","duval":"https://taxdeed.duvalclerk.com/","escambia":"https://www.escambiaclerk.com/362/Tax-Deeds","flagler":"https://flaglerclerk.gov/sales/tax-deeds-sales/","franklin":"https://www.franklinclerk.com/public-sales/tax-deeds/","gadsden":"https://www.gadsdenclerk.com/Tax_deeds/Tax_deeds.htm","gilchrist":"https://gilchristclerk.com/tax-deeds/","glades":"https://gladesclerk.com/clerk-services/tax-deeds/","gulf":"https://www.gulfclerk.com/courts/tax-deeds/","hamilton":"https://hamiltonclerk.com/tax-deeds/","hardee":"https://www.hardeeclerk.com/departments/tax-deeds/tax-deed-sales/","hendry":"https://www.hendryclerk.org/tax-deeds/","hernando":"https://hernandoclerk.com/additional-services/tax-deeds/tax-deed-file-search/","highlands":"https://highlands.realtdm.com/public/cases/list","hillsborough":"https://www.hillsclerk.com/taxdeeds","holmes":"https://holmesclerk.com/courts/foreclosures-tax-deeds/tax-deeds/","indianriver":"https://taxdeeds.indian-river.org/TaxDeeds","jackson":"https://www.jacksonclerk.com/clerk-services/tax-deed-sales/","jefferson":"https://www.jeffersonclerk.com/clerk-services/property-sales/tax-deed-sales/","lafayette":"https://www.lafayetteclerk.com/tax-deeds/","lake":"https://taxdeeds.lakecountyclerk.org/","lee":"https://www.leeclerk.org/departments/courts/property-sales/tax-deed-sales","leon":"https://cvweb.leonclerk.com/public/clerk_services/finance/tax_deeds/tax_deeds.asp","levy":"https://online.levyclerk.com/TaxSmartWeb","liberty":"https://libertyclerk.com/courts/tax-deeds/","madison":"https://www.madisonclerk.com/departments-services/property-sales/tax-deed-sales/","marion":"https://nvweb.marioncountyclerk.org/browserviewtd/","martin":"https://www.martinclerk.com/308/Tax-Deed-Files","monroe":"https://monroe-clerk.com/","nassau":"https://www.nassauclerk.com/190/View-Tax-Deed-Sales-and-Foreclosures","okaloosa":"https://www.bid4assets.com/OkaloosaFLTax/listings","orange":"https://or.occompt.com/recorder/tdsmweb/applicationSearch.jsp?guest=true","osceola":"https://osceolaclerk.com/tax-deeds/","palmbeach":"https://taxdeed.mypalmbeachclerk.com/","pasco":"https://www.pascoclerk.com/201/Tax-Deed-Sales","pinellas":"https://taxdeedsales.mypinellasclerk.gov/","polk":"https://www.polkclerkfl.gov/189/Tax-Deeds","putnam":"https://apps.putnam-fl.com/coc/taxdeeds/public/disclaimer.php","santarosa":"https://santarosaclerk.com/courts/foreclosures-tax-deeds/","sarasota":"https://www.sarasotaclerk.com/Home-and-Property/Tax-Deeds","seminole":"https://webapps.seminoleclerk.org/TaxDeedSales/","stjohns":"https://apps.stjohnsclerk.com/TaxSmart","sumter":"https://www.sumterclerk.com/tax-deed-sales","suwannee":"https://www.suwgov.org/tax-deed-sales/","taylor":"https://taylorclerk.com/departments/tax-deeds/","union":"https://unionclerk.com/tax-deed-sales/","volusia":"https://app02.clerk.org/or_td/","wakulla":"https://wakullaclerk.org/official_records/tax_deed_sales.php","washington":"https://www.washingtonclerk.com/public-sales/tax-deeds/"};

// In-person tax-deed counties: no online case-file/O&E system — the file is
// physical at the courthouse. Tell the investor to request it, don't say "no liens."
const IN_PERSON = new Set(["bradford","collier","columbia","desoto","dixie","franklin","gadsden","glades","hamilton","hardee","holmes","jefferson","lafayette","levy","liberty","madison","stjohns","sumter","taylor","union","wakulla"]);
function countySlug(county: string): string { return (county || "").toLowerCase().replace(/[^a-z]/g, ""); }

// Domain suffixes the agent may READ documents/pages from — Florida county
// clerk / official-records / appraiser systems. Combined with a generic gov
// pattern and per-parcel URLs, this lets the agent open the O&E wherever a
// county keeps it (e.g. Volusia's app02.clerk.org) while staying SSRF-safe.
const SAFE_SUFFIXES = ["alachuacounty.us","baycoclerk.com","brevardclerk.us","broward.org","citrusclerk.org","clayclerk.com","clerk.org","duvalclerk.com","escambiaclerk.com","flaglerclerk.gov","gilchristclerk.com","gulfclerk.com","hendryclerk.org","hernandoclerk.com","highlandsclerkfl.gov","hillsclerk.com","indian-river.org","indianriverclerk.com","jacksonclerk.com","lakecountyclerk.org","lakecountyclerkfl.gov","leeclerk.org","leonclerk.com","marioncountyclerk.org","martinclerk.com","monroe-clerk.com","mypalmbeachclerk.com","mypinellasclerk.gov","nassauclerk.com","occompt.com","osceolaclerk.com","pascoclerk.com","polkclerkfl.gov","putnam-fl.com","putnamclerk.com","realtaxdeed.com","realtdm.com","santarosaclerk.com","sarasotaclerk.com","seminoleclerk.org","suwgov.org","washingtonclerk.com"];

// May the agent fetch/read this URL? Its own parcel documents always; otherwise
// only public clerk/records/appraiser/government hosts — never private/internal.
function canRead(url: string, explicit: Set<string>): boolean {
  if (explicit.has(url)) return true;
  let host = "";
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    host = u.hostname.toLowerCase();
  } catch { return false; }
  if (!host.includes(".") || host === "localhost") return false;
  if (/^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|::1|0\.)/.test(host)) return false;
  if (SAFE_SUFFIXES.some((s) => host === s || host.endsWith("." + s))) return true;
  return /(clerk|appraiser|officialrecord|realtdm|realtaxdeed|taxdeed|county|\.gov$|\.fl\.us$)/i.test(host);
}

const DOC_INTEREST = ["all forms", "tax deed", "notice of publication", "clerk",
  "affidavit", "513", "certificate", "title", "search", "sale", "receipt",
  "statement", "lien", "notice", "ownership", "encumbrance", "o&e", "o & e",
  "owner search", "property information", "property info", "current owner"];

function normNum(s: any): string { return String(s ?? "").replace(/[^A-Za-z0-9]/g, "").toUpperCase(); }
function stripTags(html: string): string {
  return html.replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
}
function abs(href: string, base: string): string { try { return new URL(href, base).href; } catch { return ""; } }

async function fetchTextRaw(url: string): Promise<string> {
  try {
    const r = await fetch(url, { headers: { "User-Agent": UA } });
    if (!r.ok) return "";
    return await r.text();
  } catch { return ""; }
}

function extractCaseDocs(html: string, baseUrl: string): { name: string; url: string }[] {
  const docs: { name: string; url: string }[] = [];
  const seen = new Set<string>();
  const re = /<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) && docs.length < MAX_DOCS) {
    const href = m[1];
    const name0 = stripTags(m[2]);
    const hay = (name0 + " " + href).toLowerCase();
    if (!DOC_INTEREST.some((k) => hay.includes(k))) continue;
    if (!/(image|document|doc|pdf|view|form|getdoc|download)/i.test(href)) continue;
    const url = abs(href, baseUrl);
    if (!url || seen.has(url)) continue;
    seen.add(url);
    let name = name0.replace(/^\s*(view|image|open|download|pdf)\b[\s:|-]*/i, "").trim();
    if (name.length < 3) name = "case document";
    docs.push({ name: name.slice(0, 80), url });
  }
  return docs;
}

function realtdmBase(county: string): string {
  const slug = (county || "").toLowerCase().replace(/[^a-z]/g, "");
  return slug ? `https://${slug}.realtdm.com` : "";
}

// Best-effort: find the parcel's case-detail page and pull its document list.
// Uses the clerk case URL when the feed already has one, else derives the
// RealAuction/RealTDM case list from the county and matches by number.
async function resolveCaseDocs(record: any): Promise<{ docs: { name: string; url: string }[]; caseUrl: string }> {
  if (record.clerk_case_url) {
    const html = await fetchTextRaw(String(record.clerk_case_url));
    if (html) {
      const docs = extractCaseDocs(html, String(record.clerk_case_url));
      if (docs.length) return { docs, caseUrl: String(record.clerk_case_url) };
    }
  }
  const base = realtdmBase(record.county);
  if (!base) return { docs: [], caseUrl: "" };
  const listHtml = await fetchTextRaw(base + "/public/cases/list");
  if (!listHtml) return { docs: [], caseUrl: "" };
  const wanted = new Set([record.case_number, record.certificate_number, record.parcel_id].map(normNum).filter(Boolean));
  let detailUrl = "";
  for (const row of listHtml.split(/<tr[\s>]/i)) {
    const hm = row.match(/href=["']([^"']*cases\/getCase\/caseid\/\d+[^"']*)["']/i);
    if (!hm) continue;
    const cells = (row.match(/<t[dh][\s\S]*?<\/t[dh]>/gi) || []).map((c) => normNum(stripTags(c)));
    if (cells.some((c) => c.length >= 5 && wanted.has(c))) { detailUrl = abs(hm[1], base); break; }
  }
  if (!detailUrl) return { docs: [], caseUrl: "" };
  const detailHtml = await fetchTextRaw(detailUrl);
  return { docs: detailHtml ? extractCaseDocs(detailHtml, detailUrl) : [], caseUrl: detailUrl };
}

async function readDocText(url: string): Promise<string> {
  try {
    const resp = await fetch(url, { headers: { "User-Agent": UA } });
    if (!resp.ok) return `(could not fetch document: HTTP ${resp.status})`;
    const buf = new Uint8Array(await resp.arrayBuffer());
    if (buf[0] === 0x25 && buf[1] === 0x50 && buf[2] === 0x44 && buf[3] === 0x46) {   // %PDF
      try {
        const { extractText, getDocumentProxy } = await import("npm:unpdf");
        const pdf = await getDocumentProxy(buf);
        const res: any = await extractText(pdf, { mergePages: true });
        const t = (Array.isArray(res?.text) ? res.text.join("\n") : String(res?.text ?? "")).trim();
        return t ? t.slice(0, MAX_PDF_CHARS)
          : "(the document has no readable text layer — likely a scan; a human must open it at the clerk)";
      } catch (e) {
        return `(could not read the PDF text: ${String(e).slice(0, 140)})`;
      }
    }
    return stripTags(new TextDecoder().decode(buf)).slice(0, MAX_PAGE_CHARS) || "(no readable text)";
  } catch (e) {
    return `(fetch error: ${String(e).slice(0, 120)})`;
  }
}

function officialRecords(county: string, owner: string): any {
  const slug = (county || "").toLowerCase().replace(/[^a-z]/g, "");
  const known = OR_REGISTRY[slug];
  const url = known || `https://www.google.com/search?q=${encodeURIComponent((county || "") + " county florida clerk official records search")}`;
  return {
    official_records_url: url,
    known_endpoint: !!known,
    guidance: (owner ? `Search this county's Official Records index for the owner name "${owner}". ` : "Search the owner's name in this county's Official Records index. ") +
      "Look for recorded mortgages, judgments, IRS/federal tax liens, code-enforcement/municipal liens, special assessments and lis pendens. Under Florida law a tax-deed sale extinguishes most private liens, but IRS/federal, municipal/code and other governmental liens, special assessments and title defects survive.",
  };
}

function factsText(record: any): string {
  const f = (k: string, v: any) => (v === undefined || v === null || v === "" ? "" : `- ${k}: ${v}\n`);
  const docs = Array.isArray(record.case_docs) ? record.case_docs : [];
  return "PARCEL FACTS (from the auction feed):\n" +
    f("County", record.county) + f("Parcel ID", record.parcel_id) +
    f("Case #", record.case_number) + f("Certificate #", record.certificate_number) +
    f("Property address", record.property_address) + f("Owner of record", record.owner_name) +
    f("Mailing address", record.mailing_address) + f("Property use", record.property_use) +
    f("Acreage", record.acreage) + f("Assessed value", record.assessed_value) +
    f("Opening bid", record.opening_bid) + f("Bid-to-value %", record.bid_to_value_pct) +
    f("Deed status", record.deed_status) + f("Applicant (forced the sale)", record.applicant) +
    f("Existing data flags", Array.isArray(record.case_flags) ? record.case_flags.join("; ") : "") +
    f("Latitude", record.lat) + f("Longitude", record.lng) +
    f("Appraiser page", record.appraiser_url) + f("Clerk case page", record.clerk_case_url) +
    f("County clerk tax-deed page (where the O&E lives)", CLERK_SEARCH[countySlug(record.county)]) +
    (IN_PERSON.has(countySlug(record.county))
      ? "NOTE: This is an IN-PERSON tax-deed county — the case file and O&E are a PHYSICAL file at the clerk's office, generally not online. Report that the O&E must be requested from the clerk; do NOT conclude there are no liens.\n"
      : "") +
    (docs.length
      ? "Case documents already on file (read the O&E/title one first with read_pdf):\n" +
        docs.map((d: any) => `  - ${d?.name || "doc"}: ${d?.url || ""}`).join("\n") + "\n"
      : "No case documents were pre-attached — call get_case_documents to find them.\n");
}

async function callOpenAI(payload: unknown) {
  const r = await fetch(OPENAI_URL, {
    method: "POST",
    headers: { "Authorization": `Bearer ${OPENAI_API_KEY}`, "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(`openai ${r.status}: ${(await r.text()).slice(0, 300)}`);
  return await r.json();
}

async function writeReady(svc: any, pkey: string, data: any) {
  await svc.from("parcel_analysis").update({
    status: "ready", data: { ...data, generated_at: new Date().toISOString() }, updated_at: new Date().toISOString(),
  }).eq("pkey", pkey);
}

function outputText(resp: any): string {
  if (typeof resp.output_text === "string" && resp.output_text.trim()) return resp.output_text.trim();
  const parts: string[] = [];
  for (const item of (resp.output ?? [])) {
    if (item.type === "message") for (const c of (item.content ?? [])) if (c.type === "output_text" && c.text) parts.push(c.text);
  }
  return parts.join("\n").trim();
}

async function runAgent(record: any, pkey: string, svc: any) {
  // Explicit per-parcel URLs the agent may always read; canRead() additionally
  // permits public FL clerk/records/appraiser hosts (so the O&E is readable
  // wherever the county keeps it), while blocking private/internal hosts.
  const explicit = new Set<string>();
  for (const u of [record.appraiser_url, record.clerk_case_url]) if (u) explicit.add(String(u));
  for (const d of (Array.isArray(record.case_docs) ? record.case_docs : [])) if (d?.url) explicit.add(String(d.url));
  const clerkSearch = CLERK_SEARCH[countySlug(record.county)] || "";
  const inPerson = IN_PERSON.has(countySlug(record.county));

  const fullTools = [WEB_SEARCH_TOOL, GET_CASE_DOCS_TOOL, READ_PDF_TOOL, FETCH_PAGE_TOOL, OFFICIAL_RECORDS_TOOL, EMIT_TOOL];
  const noWebTools = [GET_CASE_DOCS_TOOL, READ_PDF_TOOL, FETCH_PAGE_TOOL, OFFICIAL_RECORDS_TOOL, EMIT_TOOL];

  for (let attempt = 0; attempt < 2; attempt++) {
    const tools = attempt === 0 ? fullTools : noWebTools;
    try {
      let resp = await callOpenAI({
        model: ANALYSIS_MODEL, instructions: SYSTEM, max_output_tokens: MAX_TOKENS, tools,
        input: [{ role: "user", content: [{ type: "input_text", text: factsText(record) + "\nResearch this parcel — liens first — then call emit_analysis exactly once." }] }],
      });
      for (let step = 0; step < MAX_STEPS; step++) {
        const items: any[] = resp.output ?? [];
        const calls = items.filter((it) => it.type === "function_call");
        const emit = calls.find((c) => c.name === "emit_analysis");
        if (emit) {
          let parsed: any = {};
          try { parsed = JSON.parse(emit.arguments || "{}"); } catch { parsed = {}; }
          await writeReady(svc, pkey, parsed);
          return;
        }
        if (!calls.length) {
          const text = outputText(resp);
          await writeReady(svc, pkey, {
            summary: text || "No analysis produced.", value_read: "", title_and_liens: "",
            site_and_environment: "", red_flags: [], opportunities: [], suggested_next_steps: [],
            sources: [], confidence: "low",
          });
          return;
        }
        const outputs: any[] = [];
        for (const fc of calls) {
          let args: any = {};
          try { args = JSON.parse(fc.arguments || "{}"); } catch { args = {}; }
          let output = "";
          if (fc.name === "get_case_documents") {
            const res = await resolveCaseDocs(record);
            for (const d of res.docs) explicit.add(d.url);
            if (res.caseUrl) explicit.add(res.caseUrl);
            output = JSON.stringify(res.docs.length
              ? { case_url: res.caseUrl, documents: res.docs, clerk_file_search: clerkSearch }
              : { documents: [], clerk_file_search: clerkSearch, in_person: inPerson,
                  note: inPerson
                    ? `${record.county} is an IN-PERSON tax-deed county: the case file and Ownership & Encumbrance report are a physical file at the clerk's office (${clerkSearch || "see the county clerk"}), not online. In title_and_liens, report that the O&E must be requested from the clerk and could not be read online — do NOT say there are no liens. Still give the survivable-lien guidance and next steps.`
                    : `Could not auto-resolve the case documents. Open the county clerk tax-deed page (${clerkSearch || "search the web for it"}) with fetch_page to find this parcel's file and its Ownership & Encumbrance report, then read_pdf it. You may also web_search for the parcel's tax-deed file and read any county clerk/records PDF you find.` });
          } else if (fc.name === "read_pdf") {
            const url = String(args.url || "");
            output = canRead(url, explicit) ? await readDocText(url) : "(that host isn't a county clerk/records/appraiser site — only this parcel's own documents and public county records can be read)";
          } else if (fc.name === "fetch_page") {
            const url = String(args.url || "");
            output = canRead(url, explicit) ? stripTags(await fetchTextRaw(url)).slice(0, MAX_PAGE_CHARS) : "(that host isn't a county clerk/records/appraiser site)";
          } else if (fc.name === "official_records_search") {
            output = JSON.stringify(officialRecords(String(record.county || ""), String(args.owner_name || record.owner_name || "")));
          } else {
            output = "(unknown tool)";
          }
          outputs.push({ type: "function_call_output", call_id: fc.call_id, output });
        }
        resp = await callOpenAI({
          model: ANALYSIS_MODEL, instructions: SYSTEM, max_output_tokens: MAX_TOKENS, tools,
          previous_response_id: resp.id, input: outputs,
        });
      }
      throw new Error("agent did not converge within step budget");
    } catch (e) {
      if (attempt === 1) throw e;   // both attempts failed → surface the error
    }
  }
}
