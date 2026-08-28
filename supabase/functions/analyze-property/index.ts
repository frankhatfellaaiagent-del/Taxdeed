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

LIENS ARE THE MOST IMPORTANT PART. Work the lien question hard and concretely:
1. Get the parcel's clerk tax-deed case file. If case documents were not provided, call get_case_documents.
2. Read the Ownership & Encumbrance (O&E) / title / current-owner-search report with read_pdf — that report is the county's own list of every recorded mortgage, judgment, IRS/federal tax lien, HOA claim and encumbrance on this parcel. Read the other case documents too if useful.
3. Call official_records_search to get where the county's Official Records live, and search the web, to corroborate or fill gaps.
Under Florida law a tax-deed sale extinguishes MOST private liens and mortgages (they are junior to the tax lien), but these SURVIVE the sale and are the real risk: IRS / federal tax liens, municipal & code-enforcement liens, other governmental liens and special assessments, certain easements, and title-marketability defects (a quiet-title action is usually needed). Call these out specifically.

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
  description: "Read the text of one of this parcel's case documents (e.g. the O&E / title report) by its URL. Only URLs from this parcel's case documents are allowed.",
  parameters: { type: "object", properties: { url: { type: "string" } }, required: ["url"] },
};
const FETCH_PAGE_TOOL = {
  type: "function",
  name: "fetch_page",
  description: "Fetch an allow-listed HTML page (the county appraiser page or the clerk case page) and return its readable text.",
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
  // Allow-list of URLs the agent may fetch/read: the parcel's own documents and
  // pages, growing as get_case_documents discovers more. Keeps it SSRF-safe.
  const allowed = new Set<string>();
  for (const u of [record.appraiser_url, record.clerk_case_url]) if (u) allowed.add(String(u));
  for (const d of (Array.isArray(record.case_docs) ? record.case_docs : [])) if (d?.url) allowed.add(String(d.url));

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
            for (const d of res.docs) allowed.add(d.url);
            if (res.caseUrl) allowed.add(res.caseUrl);
            output = JSON.stringify(res.docs.length
              ? { case_url: res.caseUrl, documents: res.docs }
              : { documents: [], note: "Could not resolve this parcel's case documents online. Use official_records_search and web_search, and tell the investor to pull the O&E from the clerk's tax-deed file." });
          } else if (fc.name === "read_pdf") {
            const url = String(args.url || "");
            output = allowed.has(url) ? await readDocText(url) : "(url not in this parcel's case documents — only this parcel's own documents can be read)";
          } else if (fc.name === "fetch_page") {
            const url = String(args.url || "");
            output = allowed.has(url) ? stripTags(await fetchTextRaw(url)).slice(0, MAX_PAGE_CHARS) : "(url not in this parcel's allow-list)";
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
