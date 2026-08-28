-- Per-property AI research cache (shared across all teams) + a daily usage
-- counter that caps spend. Written only by the analyze-property Edge Function
-- (service role, which bypasses RLS); read by any authenticated dashboard user.
--
-- Applied to the "Tax Deed Radar" Supabase project. Kept here for version
-- control; the project's own migration history is the source of truth at runtime.

create table if not exists public.parcel_analysis (
  pkey        text primary key,                       -- county|parcel_id|case_number
  status      text not null default 'pending',        -- pending | ready | error
  data        jsonb not null default '{}'::jsonb,      -- structured analysis
  model       text,
  created_by  uuid references auth.users(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

alter table public.parcel_analysis enable row level security;

-- Shared read: any signed-in user sees any cached analysis (like the shared feed).
drop policy if exists parcel_analysis_read_authenticated on public.parcel_analysis;
create policy parcel_analysis_read_authenticated
  on public.parcel_analysis for select to authenticated using (true);
-- No write policies: only the service role (Edge Function) may insert/update.

-- Per-team, per-day counter so a runaway can't burn the API balance.
create table if not exists public.analysis_usage (
  team_id  text not null,
  day      date not null,
  count    integer not null default 0,
  primary key (team_id, day)
);
alter table public.analysis_usage enable row level security;
-- No policies: service-role only.

-- Push cache rows to subscribed clients when the async agent finishes.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'parcel_analysis'
  ) then
    alter publication supabase_realtime add table public.parcel_analysis;
  end if;
end $$;
