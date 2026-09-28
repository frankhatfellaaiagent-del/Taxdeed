-- Free-month trials that don't need Stripe.
--
-- The operator shares https://taxdeed.app/app/?signup=1&code=FEEDBACK. The
-- signup edge function validates the code against trial_codes and stamps the
-- new user's app_metadata (service-role only — users can't edit it) with
-- trial_days. On first sign-in ensure_my_profile() opens the account on Pro
-- with plan_expires_at = now() + trial_days, and on every later sign-in / app
-- load drops an expired trial back to Free. Paid access (any webhook plan
-- change) clears plan_expires_at, so it never expires.

alter table public.profiles add column if not exists plan_expires_at timestamptz;

create table if not exists public.trial_codes (
  code text primary key,                   -- stored upper-case
  days integer not null check (days between 1 and 365),
  active boolean not null default true,
  note text,
  created_at timestamptz not null default now()
);
alter table public.trial_codes enable row level security;
-- No policies: only the signup function (service role) reads it.
insert into public.trial_codes (code, days, note)
values ('FEEDBACK', 30, 'Free month for people the operator shares the app with')
on conflict (code) do nothing;

-- Payments grant access with no expiry.
create or replace function public.set_plan_by_email(p_email text, p_plan text, p_customer text default null)
returns void language plpgsql security definer set search_path = public as $$
declare v_id uuid;
begin
  if p_plan not in ('free', 'pro') then raise exception 'invalid plan %', p_plan; end if;
  if p_email is null or btrim(p_email) = '' then return; end if;
  select id into v_id from auth.users where lower(email) = lower(btrim(p_email)) limit 1;
  if v_id is not null then
    update public.profiles
       set plan = p_plan, plan_expires_at = null,
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

create or replace function public.set_plan_by_user(p_user uuid, p_plan text, p_customer text default null)
returns boolean language plpgsql security definer set search_path = public as $$
begin
  if p_plan not in ('free', 'pro') then raise exception 'invalid plan %', p_plan; end if;
  update public.profiles
     set plan = p_plan, plan_expires_at = null,
         stripe_customer = coalesce(p_customer, stripe_customer)
   where id = p_user;
  return found;
end $$;

create or replace function public.set_plan_by_customer(p_customer text, p_plan text)
returns void language plpgsql security definer set search_path = public as $$
begin
  if p_plan not in ('free', 'pro') then raise exception 'invalid plan %', p_plan; end if;
  update public.profiles set plan = p_plan, plan_expires_at = null where stripe_customer = p_customer;
  update public.pending_entitlements set plan = p_plan where stripe_customer = p_customer;
end $$;

-- Return type gains plan_expires_at, so the function is recreated.
drop function if exists public.ensure_my_profile(text);
create function public.ensure_my_profile(p_name text default null)
returns table (team_id text, team_name text, plan text, is_admin boolean, plan_expires_at timestamptz)
language plpgsql security definer set search_path = public as $$
#variable_conflict use_column
declare
  v_uid uuid := auth.uid();
  v_email text;
  v_trial text;
  v_trial_days integer := 0;
  v_team text;
  v_pending public.pending_entitlements%rowtype;
begin
  if v_uid is null then raise exception 'not signed in'; end if;
  select u.email, u.raw_app_meta_data->>'trial_days' into v_email, v_trial
    from auth.users u where u.id = v_uid;
  if v_trial ~ '^[0-9]{1,3}$' then v_trial_days := least(v_trial::integer, 365); end if;

  -- First sign-in: a private workspace, on a free month if the signup code
  -- granted one, otherwise on Free.
  if not exists (select 1 from public.profiles p where p.id = v_uid and p.team_id is not null) then
    v_team := 'u-' || left(replace(v_uid::text, '-', ''), 16);
    insert into public.teams (id, name)
    values (v_team, left(coalesce(nullif(btrim(p_name), ''), v_email, 'My workspace'), 80))
    on conflict (id) do nothing;
    insert into public.profiles (id, team_id, is_admin, email, plan, plan_expires_at)
    values (v_uid, v_team, false, v_email,
            case when v_trial_days > 0 then 'pro' else 'free' end,
            case when v_trial_days > 0 then now() + make_interval(days => v_trial_days) end)
    on conflict (id) do update set team_id = excluded.team_id
      where public.profiles.team_id is null;
  end if;

  -- A payment that arrived before the account existed (paid = no expiry).
  select * into v_pending from public.pending_entitlements pe where pe.email = lower(v_email);
  if found then
    update public.profiles p
       set plan = v_pending.plan, plan_expires_at = null,
           stripe_customer = coalesce(v_pending.stripe_customer, p.stripe_customer)
     where p.id = v_uid;
    delete from public.pending_entitlements pe where pe.email = lower(v_email);
  end if;

  -- A free month that has run out drops to Free (expiry kept, so the app can
  -- say when it ended).
  update public.profiles p set plan = 'free'
   where p.id = v_uid and p.plan = 'pro'
     and p.plan_expires_at is not null and p.plan_expires_at <= now();

  return query
    select p.team_id, t.name, p.plan, p.is_admin, p.plan_expires_at
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
