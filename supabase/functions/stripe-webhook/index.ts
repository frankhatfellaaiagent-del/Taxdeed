// Stripe -> app access webhook.
//
// When someone subscribes (with the same email as their app login), flip their
// account to Paid so they get in right away; when a subscription is cancelled or
// lapses, drop them back to Free. All mapping happens through two SECURITY
// DEFINER RPCs (set_plan_by_email / set_plan_by_customer) so this function never
// needs to read auth.users directly.
//
// Auth: this endpoint is called by Stripe, unauthenticated, so it is deployed
// with verify_jwt = false and instead verifies Stripe's signature on every
// request. Requires the STRIPE_WEBHOOK_SECRET function secret (the whsec_... from
// the Stripe webhook endpoint). SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are
// injected by the Supabase runtime.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import Stripe from "https://esm.sh/stripe@17.7.0?target=deno";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2?target=deno";

const WEBHOOK_SECRET = Deno.env.get("STRIPE_WEBHOOK_SECRET") ?? "";
// No Stripe API calls are made (verification + RPCs only), so the API key is not
// required; pass whatever is set to satisfy the constructor.
const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY") ?? "sk_unused", {
  apiVersion: "2024-06-20",
  httpClient: Stripe.createFetchHttpClient(),
});
const cryptoProvider = Stripe.createSubtleCryptoProvider();

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

const customerId = (c: unknown): string | null =>
  typeof c === "string" ? c : (c && typeof c === "object" && "id" in c ? (c as { id: string }).id : null);

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const sig = req.headers.get("stripe-signature");
  const body = await req.text();
  if (!sig || !WEBHOOK_SECRET) return new Response("missing signature or secret", { status: 400 });

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(body, sig, WEBHOOK_SECRET, undefined, cryptoProvider);
  } catch (e) {
    return new Response(`signature verification failed: ${(e as Error).message}`, { status: 400 });
  }

  try {
    if (event.type === "checkout.session.completed") {
      const s = event.data.object as Stripe.Checkout.Session;
      const email = s.customer_details?.email ?? s.customer_email ?? null;
      const customer = customerId(s.customer);
      if (email && (s.mode === "subscription" || s.payment_status === "paid")) {
        await supabase.rpc("set_plan_by_email", { p_email: email, p_plan: "pro", p_customer: customer });
      }
    } else if (event.type === "customer.subscription.created" || event.type === "customer.subscription.updated") {
      const sub = event.data.object as Stripe.Subscription;
      const customer = customerId(sub.customer);
      const active = ["active", "trialing", "past_due"].includes(sub.status);
      if (customer) await supabase.rpc("set_plan_by_customer", { p_customer: customer, p_plan: active ? "pro" : "free" });
    } else if (event.type === "customer.subscription.deleted") {
      const sub = event.data.object as Stripe.Subscription;
      const customer = customerId(sub.customer);
      if (customer) await supabase.rpc("set_plan_by_customer", { p_customer: customer, p_plan: "free" });
    }
  } catch (e) {
    console.error("webhook handler error:", (e as Error).message);
    return new Response("handler error", { status: 500 });
  }

  return new Response(JSON.stringify({ received: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});
