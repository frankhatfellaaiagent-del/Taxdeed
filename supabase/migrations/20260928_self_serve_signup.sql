-- Self-serve signup + paid access that can't get lost.
--
-- Before this, accounts were operator-created only and the Stripe webhook could
-- only upgrade an account that already existed — so a new customer who paid
-- (or used a 100%-off code) ended up with no login and no access. Now:
--   * the `signup` edge function creates a confirmed account (no email needed);
--   * the app calls ensure_my_profile() on sign-in, which gives any account
--     without a profile its own private workspace (team) on the Free plan;
--   * the webhook matches a checkout to the exact account that started it
--     (set_plan_by_user via Stripe client_reference_id), falling back to email;
--   * a payment for an email with no account yet is parked in
--     pending_entitlements and applied the moment that account signs in.

-- Payments that arrived before the payer had an app account.
create table if not exists public.pending_entitlements (
  email text primary key,                  -- lower-cased
  plan text not null check (plan in ('free', 'pro')),
  stripe_customer text,
  created_at timestamptz not null default now()
);
alter table public.pending_entitlements enable row level security;
-- No policies on purpose: only the service role / SECURITY DEFINER code touches it.

-- Signup attempts, for a light per-IP rate limit in the signup function.
create table if not exists public.signup_attempts (
  id bigint generated always as identity primary key,
  ip text not null,
  at timestamptz not null default now()
);
create index if not exists signup_attempts_ip_at_idx on public.signup_attempts (ip, at);
alter table public.signup_attempts enable row level security;

-- Set a plan by the email the customer paid with. If there's no account (or no
-- profile) for it yet, park the entitlement instead of dropping it.
create or replace function public.set_plan_by_email(p_email text, p_plan text, p_customer text default null)
returns void language plpgsql security definer set search_path = public as $$
declare v_id uuid;
begin
  if p_plan not in ('free', 'pro') then raise exception 'invalid plan %', p_plan; end if;
  if p_email is null or btrim(p_email) = '' then return; end if;
  select id into v_id from auth.users where lower(email) = lower(btrim(p_email)) limit 1;
  if v_id is not null then
    update public.profiles
       set plan = p_plan,
           stripe_customer = coalesce(p_customer, stripe_customer)
     where id = v_id;
    if found then return; end if;
  end if;
  insert into public.pending_entitlements (email, plan, stripe_customer)
  values (lower(btrim(p_email)), p_plan, p_customer)
  on conflict (email) do update
     set plan = excluded.plan,
         stripe_customer = coalesce(excluded.stripe_customer, public.pending_entitlements.stripe_customer),
         created_at = now();
end $$;

-- Set a plan for the exact account that started checkout (Stripe
-- client_reference_id). Returns false when that account has no profile, so the
-- caller can fall back to the email path.
create or replace function public.set_plan_by_user(p_user uuid, p_plan text, p_customer text default null)
returns boolean language plpgsql security definer set search_path = public as $$
begin
  if p_plan not in ('free', 'pro') then raise exception 'invalid plan %', p_plan; end if;
  update public.profiles
     set plan = p_plan,
         stripe_customer = coalesce(p_customer, stripe_customer)
   where id = p_user;
  return found;
end $$;

-- Subscription lifecycle by Stripe customer id — also keeps a parked
-- entitlement in step (e.g. cancelled before the account was ever created).
create or replace function public.set_plan_by_customer(p_customer text, p_plan text)
returns void language plpgsql security definer set search_path = public as $$
begin
  if p_plan not in ('free', 'pro') then raise exception 'invalid plan %', p_plan; end if;
  update public.profiles set plan = p_plan where stripe_customer = p_customer;
  update public.pending_entitlements set plan = p_plan where stripe_customer = p_customer;
end $$;

-- Called by the signed-in app. Gives an account without a profile its own
-- private workspace on the Free plan, applies any parked payment for its email,
-- and returns the profile. Only ever acts on the caller's own account, and can
-- only raise the plan from a real (webhook-parked) payment — never self-upgrade.
create or replace function public.ensure_my_profile(p_name text default null)
returns table (team_id text, team_name text, plan text, is_admin boolean)
language plpgsql security definer set search_path = public as $$
#variable_conflict use_column
declare
  v_uid uuid := auth.uid();
  v_email text;
  v_team text;
  v_pending public.pending_entitlements%rowtype;
begin
  if v_uid is null then raise exception 'not signed in'; end if;
  select u.email into v_email from auth.users u where u.id = v_uid;

  if not exists (select 1 from public.profiles p where p.id = v_uid and p.team_id is not null) then
    v_team := 'u-' || left(replace(v_uid::text, '-', ''), 16);
    insert into public.teams (id, name)
    values (v_team, left(coalesce(nullif(btrim(p_name), ''), v_email, 'My workspace'), 80))
    on conflict (id) do nothing;
    insert into public.profiles (id, team_id, is_admin, email, plan)
    values (v_uid, v_team, false, v_email, 'free')
    on conflict (id) do update set team_id = excluded.team_id
      where public.profiles.team_id is null;
  end if;

  select * into v_pending from public.pending_entitlements pe where pe.email = lower(v_email);
  if found then
    update public.profiles p
       set plan = v_pending.plan,
           stripe_customer = coalesce(v_pending.stripe_customer, p.stripe_customer)
     where p.id = v_uid;
    delete from public.pending_entitlements pe where pe.email = lower(v_email);
  end if;

  return query
    select p.team_id, t.name, p.plan, p.is_admin
      from public.profiles p left join public.teams t on t.id = p.team_id
     where p.id = v_uid;
end $$;

revoke all on function public.set_plan_by_email(text, text, text) from public, anon, authenticated;
revoke all on function public.set_plan_by_user(uuid, text, text) from public, anon, authenticated;
revoke all on function public.set_plan_by_customer(text, text) from public, anon, authenticated;
revoke all on function public.ensure_my_profile(text) from public, anon;
grant execute on function public.set_plan_by_email(text, text, text) to service_role;
grant execute on function public.set_plan_by_user(uuid, text, text) to service_role;
grant execute on function public.set_plan_by_customer(text, text) to service_role;
grant execute on function public.ensure_my_profile(text) to authenticated;
