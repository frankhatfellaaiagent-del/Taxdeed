-- Access tier per account, for the Free-vs-Paid paywall.
--
-- 'free' (default) or 'pro'. The dashboard reads profiles.plan on sign-in and
-- gates features (date horizon, land check, downloads, lists) on it; a
-- signed-out visitor is treated as free. The operator sets 'pro' after a Stripe
-- payment (or for a comp / feedback account) and back to 'free' when it lapses.
--
-- SECURITY: customers must NOT be able to change their own plan. Existing
-- profiles RLS should let a user SELECT their own row but never UPDATE it — the
-- operator sets `plan` from the Supabase dashboard / service role. If an UPDATE
-- policy on profiles exists for end users, make sure it excludes `plan` (or
-- keep profiles writable only by the service role). The browser gate is soft
-- anyway (the data feed is a public file); this column is the access source of
-- truth, not a hard data wall.

alter table public.profiles
  add column if not exists plan text not null default 'free';

alter table public.profiles
  drop constraint if exists profiles_plan_check;
alter table public.profiles
  add constraint profiles_plan_check check (plan in ('free', 'pro'));
