// Self-serve account creation for the dashboard's "Create account" form.
//
// Why server-side: the project requires email confirmation, and Supabase's
// built-in mailer only delivers to the project's own team — a customer's
// confirmation email would never arrive and they'd be locked out. Creating the
// user here with the service role (email_confirm: true) lets them sign in
// immediately. The app then calls ensure_my_profile() on sign-in, which gives
// the account its private workspace on the Free plan.
//
// Called anonymously from the browser, so it is deployed with verify_jwt=false
// and does its own validation plus a light per-IP rate limit.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2?target=deno";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false, autoRefreshToken: false } },
);

const ALLOWED_ORIGINS = new Set([
  "https://taxdeed.app",
  "https://www.taxdeed.app",
  "http://taxdeed.app",
  "https://frankhatfellaaiagent-del.github.io",
]);
const MAX_PER_IP_PER_HOUR = 10;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

function corsHeaders(origin: string | null): Record<string, string> {
  const ok = origin && (ALLOWED_ORIGINS.has(origin) || /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin));
  return {
    "Access-Control-Allow-Origin": ok ? origin! : "https://taxdeed.app",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
  };
}

Deno.serve(async (req) => {
  const cors = corsHeaders(req.headers.get("origin"));
  const reply = (status: number, body: Record<string, unknown>) =>
    new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json" } });

  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  if (req.method !== "POST") return reply(405, { error: "method_not_allowed" });

  let body: { email?: unknown; password?: unknown; name?: unknown };
  try { body = await req.json(); } catch { return reply(400, { error: "bad_request", message: "Invalid request." }); }

  const email = String(body.email ?? "").trim().toLowerCase();
  const password = String(body.password ?? "");
  const name = String(body.name ?? "").trim().slice(0, 80);
  if (!EMAIL_RE.test(email) || email.length > 254) {
    return reply(400, { error: "invalid_email", message: "Enter a valid email address." });
  }
  if (password.length < 8 || password.length > 72) {
    return reply(400, { error: "weak_password", message: "Use a password of at least 8 characters." });
  }

  // Light abuse guard: cap account creations per client IP per hour.
  const ip = (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim() || "unknown";
  const since = new Date(Date.now() - 3600_000).toISOString();
  const { count } = await supabase.from("signup_attempts")
    .select("id", { count: "exact", head: true }).eq("ip", ip).gte("at", since);
  if ((count ?? 0) >= MAX_PER_IP_PER_HOUR) {
    return reply(429, { error: "rate_limited", message: "Too many sign-ups from this network — try again in an hour." });
  }
  await supabase.from("signup_attempts").insert({ ip });

  const { error } = await supabase.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
    user_metadata: name ? { name } : {},
  });
  if (error) {
    const msg = (error.message || "").toLowerCase();
    const code = (error as { code?: string }).code ?? "";
    if (code === "email_exists" || msg.includes("already") || msg.includes("registered")) {
      return reply(409, { error: "exists", message: "An account with this email already exists — sign in instead." });
    }
    if (code === "weak_password" || msg.includes("password")) {
      return reply(400, { error: "weak_password", message: error.message });
    }
    console.error("signup createUser failed:", error.message);
    return reply(500, { error: "server", message: "Couldn't create the account — please try again." });
  }
  return reply(200, { ok: true });
});
