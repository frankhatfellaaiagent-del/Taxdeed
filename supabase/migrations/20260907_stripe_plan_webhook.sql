-- Support the Stripe -> app access webhook (supabase/functions/stripe-webhook).
-- Maps a Stripe payer back to their app account so paying flips them to Paid
-- right away, and a cancellation drops them back to Free.

-- Stripe customer id on the profile, stamped on first checkout so later
-- subscription lifecycle events map back without a Stripe API call.
alter table public.profiles
  add column if not exists stripe_customer text;
create index if not exists profiles_stripe_customer_idx on public.profiles (stripe_customer);

-- Set a profile's plan by the email they paid with (must match an auth user).
-- SECURITY DEFINER so it can read auth.users; only the service role may call it
-- (revoked from anon/authenticated so a customer can't self-upgrade via RPC).
create or replace function public.set_plan_by_email(p_email text, p_plan text, p_customer text default null)
returns void language plpgsql security definer set search_path = public as $$
declare v_id uuid;
begin
  if p_plan not in ('free','pro') then raise exception 'invalid plan %', p_plan; end if;
  select id into v_id from auth.users where lower(email) = lower(p_email) limit 1;
  if v_id is null then return; end if;   -- no login with that email yet: no-op
  update public.profiles
     set plan = p_plan,
         stripe_customer = coalesce(p_customer, stripe_customer)
   where id = v_id;
end $$;

-- Set a profile's plan by Stripe customer id (subscription updated/deleted).
create or replace function public.set_plan_by_customer(p_customer text, p_plan text)
returns void language plpgsql security definer set search_path = public as $$
begin
  if p_plan not in ('free','pro') then raise exception 'invalid plan %', p_plan; end if;
  update public.profiles set plan = p_plan where stripe_customer = p_customer;
end $$;

revoke all on function public.set_plan_by_email(text,text,text) from public, anon, authenticated;
revoke all on function public.set_plan_by_customer(text,text) from public, anon, authenticated;
grant execute on function public.set_plan_by_email(text,text,text) to service_role;
grant execute on function public.set_plan_by_customer(text,text) to service_role;
