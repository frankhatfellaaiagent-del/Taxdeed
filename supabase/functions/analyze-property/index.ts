// analyze-property — on-demand AI due-diligence research for ONE Florida
// tax-deed parcel. Invoked from the dashboard with the property's feed record;
// runs a bounded Anthropic tool-use loop (native web search + reading the
// parcel's own clerk PDFs and appraiser page), then writes a structured,
// source-cited result into public.parcel_analysis. The result is shared across
// all teams and cached, so a parcel is researched (and paid for) at most once
// per TTL window no matter how many people open it.
//
// Dormant by design: with no ANTHROPIC_API_KEY / ANALYSIS_MODEL secret set, it
// returns {status:"disabled"} and never calls out — same posture as the
// ReportAll integration. The model name lives in a secret, never in code.
//
// Auth is enforced in-function (a real signed-in user is required), so the
// function is deployed with verify_jwt disabled and does its own check.

import { createClient } from "jsr:@supabase/supabase-js@2";

const ANTHROPIC_API_KEY = Deno.env.get("ANTHROPIC_API_KEY") ?? "";
const ANALYSIS_MODEL = Deno.env.get("ANALYSIS_MODEL") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";

const ANTHROPIC_URL = "https://api.anthropic.com/v1/messages";
const ANTHROPIC_VERSION = "2023-06-01";

const TTL_DAYS = 30;        // a cached "ready" analysis is reused this long
const DAILY_CAP = 25;       // new analyses per team per day (spend guard)
const MAX_STEPS = 6;        // bounded agent loop
const MAX_DOCS = 2;         // clerk PDFs handed to the model as documents
const MAX_TOKENS = 4000;
const WEB_SEARCH_MAX_USES = 5;
const PENDING_STALE_MS = 3 * 60 * 1000;   // treat a pending row older than this as abandoned

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

  // Dormant until both secrets are set.
  if (!ANTHROPIC_API_KEY || !ANALYSIS_MODEL) {
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

  // --- Input ---
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
    if (existing.status === "pending" && ageMs < PENDING_STALE_MS) {
      return json({ status: "pending", pkey });   // already running elsewhere
    }
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

  // Claim the slot: mark pending, count the run.
  await svc.from("parcel_analysis").upsert({
    pkey, status: "pending", model: ANALYSIS_MODEL, created_by: user.id, updated_at: new Date().toISOString(),
  }, { onConflict: "pkey" });
  await svc.from("analysis_usage").upsert(
    { team_id: teamId, day: today, count: (usage?.count ?? 0) + 1 }, { onConflict: "team_id,day" });

  // Run the agent in the background; the client gets the result via realtime.
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

Ground every claim in a source you actually retrieved — a document you read or a web search result. When you cannot find something, say "not found" rather than guessing. Never invent liens, values, owners, restrictions, or facts. Be concrete and specific to THIS parcel.

Cover, as the evidence allows: how the opening bid compares to assessed value and any comparable/nearby sales you can find; what the clerk's case documents actually say about title, liens, mortgages, judgments, homestead, bankruptcy, or code enforcement; the site itself (land use/zoning, access, flood zone, wetlands, environmental concerns); clear red flags; genuine opportunities; and concrete next steps for the investor.

Use web_search for zoning/GIS, flood/wetlands, news, and comparable sales. Use fetch_page to read the county appraiser page or a clerk case page (HTML). The parcel's clerk PDFs are provided to you directly as documents.

When your research is complete, call the emit_analysis tool EXACTLY ONCE with your findings. This is research assistance to speed due diligence — it is NOT legal or title advice; say so in your summary.`;

const EMIT_TOOL = {
  name: "emit_analysis",
  description: "Return the final structured due-diligence analysis for this parcel. Call exactly once, at the end.",
  input_schema: {
    type: "object",
    properties: {
      summary: { type: "string", description: "2-4 sentence plain-English read of the property and the opportunity/risk. Include the not-legal-advice caveat." },
      value_read: { type: "string", description: "Opening bid vs assessed value vs any comparables found." },
      title_and_liens: { type: "string", description: "What the case documents actually say about title, liens, mortgages, judgments, homestead, bankruptcy. 'not found' if none seen." },
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

const FETCH_TOOL = {
  name: "fetch_page",
  description: "Fetch an allow-listed HTML page (the county appraiser page or a clerk case page) and return its readable text.",
  input_schema: { type: "object", properties: { url: { type: "string" } }, required: ["url"] },
};

const WEB_SEARCH_TOOL = { type: "web_search_20250305", name: "web_search", max_uses: WEB_SEARCH_MAX_USES };

function docUrls(record: any): string[] {
  const docs = Array.isArray(record.case_docs) ? record.case_docs : [];
  return docs.map((d: any) => String(d?.url || "")).filter((u: string) => /^https?:\/\//i.test(u)).slice(0, MAX_DOCS);
}

function allowList(record: any): Set<string> {
  const urls: string[] = [];
  if (record.appraiser_url) urls.push(String(record.appraiser_url));
  if (record.clerk_case_url) urls.push(String(record.clerk_case_url));
  for (const d of (Array.isArray(record.case_docs) ? record.case_docs : [])) if (d?.url) urls.push(String(d.url));
  return new Set(urls.filter((u) => /^https?:\/\//i.test(u)));
}

function factsText(record: any): string {
  const f = (k: string, v: any) => (v === undefined || v === null || v === "" ? "" : `- ${k}: ${v}\n`);
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
    f("Case documents", (Array.isArray(record.case_docs) ? record.case_docs : [])
      .map((d: any) => `${d?.name || "doc"} (${d?.url || ""})`).join(" | "));
}

async function callAnthropic(payload: unknown) {
  const r = await fetch(ANTHROPIC_URL, {
    method: "POST",
    headers: {
      "x-api-key": ANTHROPIC_API_KEY,
      "anthropic-version": ANTHROPIC_VERSION,
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(`anthropic ${r.status}: ${(await r.text()).slice(0, 300)}`);
  return await r.json();
}

async function fetchPageText(url: string): Promise<string> {
  try {
    const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 TaxDeedRadar/1.0" } });
    if (!r.ok) return `(fetch failed: HTTP ${r.status})`;
    const html = await r.text();
    const text = html
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim();
    return text.slice(0, 12000) || "(no readable text)";
  } catch (e) {
    return `(fetch error: ${String(e).slice(0, 120)})`;
  }
}

function buildUserContent(record: any, withDocs: boolean): any[] {
  const content: any[] = [{ type: "text", text: factsText(record) }];
  if (withDocs) {
    for (const url of docUrls(record)) {
      content.push({ type: "document", source: { type: "url", url }, title: "Clerk case document" });
    }
  }
  content.push({ type: "text", text: "Research this parcel, then call emit_analysis exactly once." });
  return content;
}

async function runAgent(record: any, pkey: string, svc: any) {
  const allowed = allowList(record);
  const baseTools = [WEB_SEARCH_TOOL, FETCH_TOOL, EMIT_TOOL];

  // Two attempts: full (with clerk PDFs as documents), then a degraded retry
  // (facts + web only) if the documents/tooling trip the API.
  for (let attempt = 0; attempt < 2; attempt++) {
    const withDocs = attempt === 0;
    const messages: any[] = [{ role: "user", content: buildUserContent(record, withDocs) }];
    try {
      for (let step = 0; step < MAX_STEPS; step++) {
        const resp = await callAnthropic({
          model: ANALYSIS_MODEL,
          max_tokens: MAX_TOKENS,
          system: SYSTEM,
          tools: baseTools,
          messages,
        });
        const blocks: any[] = resp.content ?? [];
        const emit = blocks.find((b) => b.type === "tool_use" && b.name === "emit_analysis");
        if (emit) {
          await svc.from("parcel_analysis").update({
            status: "ready",
            data: { ...emit.input, generated_at: new Date().toISOString() },
            updated_at: new Date().toISOString(),
          }).eq("pkey", pkey);
          return;
        }
        const fetches = blocks.filter((b) => b.type === "tool_use" && b.name === "fetch_page");
        if (fetches.length) {
          messages.push({ role: "assistant", content: blocks });
          const results = [];
          for (const fb of fetches) {
            const url = String(fb.input?.url || "");
            const text = allowed.has(url) ? await fetchPageText(url) : "(url not in this parcel's allow-list)";
            results.push({ type: "tool_result", tool_use_id: fb.id, content: text });
          }
          messages.push({ role: "user", content: results });
          continue;   // let the model keep going
        }
        // Model stopped without emitting — salvage any text as a summary.
        if (resp.stop_reason === "end_turn") {
          const text = blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n").trim();
          await svc.from("parcel_analysis").update({
            status: "ready",
            data: { summary: text || "No analysis produced.", value_read: "", title_and_liens: "",
              site_and_environment: "", red_flags: [], opportunities: [], suggested_next_steps: [],
              sources: [], confidence: "low", generated_at: new Date().toISOString() },
            updated_at: new Date().toISOString(),
          }).eq("pkey", pkey);
          return;
        }
        // Otherwise (e.g. pause_turn) append and loop again.
        messages.push({ role: "assistant", content: blocks });
      }
      // Ran out of steps this attempt — stop trying.
      throw new Error("agent did not converge within step budget");
    } catch (e) {
      if (attempt === 1) throw e;   // both attempts failed → surface the error
      // else fall through to the degraded retry
    }
  }
}
