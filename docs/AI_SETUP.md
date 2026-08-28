# Turning on the AI property research agent

The property detail card has a **"Research with AI"** button. When someone
clicks it, an AI agent researches that one parcel — reads its clerk documents,
searches the web for zoning/GIS, flood/wetlands, news and comparable sales — and
writes back a structured report (value read, title & liens, site & environment,
red flags, opportunities, next steps, with sources).

It ships **dormant**: until you add an API key the button just says *"AI analysis
isn't configured yet."* Nothing else changes and nothing is charged. This mirrors
how the ReportAll parcel lookups stay off until their key is set.

## What you need to do (about 5 minutes)

### 1. Create an Anthropic API key
1. Go to **https://console.anthropic.com** and sign in (or sign up).
2. Open **Billing** and add a small amount of credit (even $5–$10 is plenty to
   start — each property researched costs a few cents, and results are cached so
   you never pay twice for the same parcel).
3. Open **API Keys → Create Key**, give it a name like `tax-deed-radar`, and
   **copy the key** (it starts with `sk-ant-…`). You only see it once.

### 2. Add two secrets to Supabase
The key is stored **only** on the server (the Supabase Edge Function) — it is
never in the website, never in GitHub, and never sent to a browser.

1. Go to **https://supabase.com/dashboard** → the **Tax Deed Radar** project.
2. **Project Settings → Edge Functions → Secrets** (or **Edge Functions →
   Secrets**).
3. Add these two secrets:

   | Name | Value |
   | --- | --- |
   | `ANTHROPIC_API_KEY` | the `sk-ant-…` key you copied |
   | `ANALYSIS_MODEL` | the Claude model to use (ask Claude for the current recommended model id to paste here) |

4. Save. The function picks the secrets up automatically — no redeploy needed.

That's it. Reload the dashboard, open a property, and click **Research with AI**.

## How the cost is controlled
- **On-demand only** — nothing runs until someone clicks the button.
- **Shared cache** — the result is saved and shown to everyone on your team (and
  reused for 30 days), so a property is researched **once**, not once per viewer.
- **Daily cap** — a safety limit of new analyses per team per day, so a mistake
  can't run up a bill. (Adjustable in the Edge Function if you want it higher or
  lower.)
- **Admin refresh** — only an admin sees a small "Refresh" control to force a
  fresh run on a property whose facts have changed.

## Notes
- The model name lives in the `ANALYSIS_MODEL` secret, so you can switch to a
  cheaper or newer model any time without touching code.
- The report is **research assistance to speed due diligence — not legal or
  title advice.** The agent is instructed to cite a source for every claim and to
  say "not found" rather than guess; still verify liens and title with a
  professional search before bidding.
- Turning it back off: delete the `ANTHROPIC_API_KEY` secret and the button goes
  dormant again.
