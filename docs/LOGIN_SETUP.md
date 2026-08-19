# Team login + cross-device sync (Supabase)

Login and cross-device sync are **live**, backed by a dedicated Supabase project
("Tax Deed Radar"). Each person signs in with an email + password, and their
lists, notes, max bids and Interested/Watching/Pass marks follow them to any
device. Lists marked **Shared with team** and the team's buy box are live for
everyone on the team — a parcel one teammate adds shows up on another's screen
within a second or two.

Signup is **closed**: there is no sign-up form. The operator creates every
account, so the accounts that exist are the entire access list.

## How it's wired

- **Auth**: Supabase email/password. Sessions persist per browser.
- **Data**: two Postgres tables — `user_state` (per person: `interest`, `prefs`,
  `lists`) and `team_state` (per team: `buybox`, shared `lists` + membership).
- **Isolation**: row-level security. A user can read/write only their own
  `user_state` rows and only their team's `team_state` rows; `profiles.team_id`
  (set by the operator) decides which team that is. One customer can never see
  another's data. Verified: a signed-in user sees only their team's rows and
  cannot write to another team's.
- **Live updates**: Supabase Realtime on both tables, filtered to the signed-in
  user / their team.
- The dashboard config (`SUPABASE_URL`, `SUPABASE_ANON_KEY` in
  `dashboard/index.html`) is public by design — security lives in the RLS
  policies and the closed account list, not in hiding the key.

## Onboarding a customer (operator, via the Supabase MCP or SQL editor)

Everything below is run against the **Tax Deed Radar** project. Replace the
example values. This is the whole per-customer setup — no code changes.

1. **Create the team** (skip if it exists):

   ```sql
   insert into public.teams (id, name) values ('acme', 'ACME Land Co')
   on conflict (id) do nothing;
   ```

2. **Create each person's account** (repeat per member). This makes a
   confirmed email/password user and their profile row pointing at the team.
   Set `is_admin => true` only for the platform operator (it unlocks the
   admin-only "refresh data" control); customers get `false`.

   ```sql
   with new_user as (
     insert into auth.users (
       instance_id, id, aud, role, email, encrypted_password,
       email_confirmed_at, created_at, updated_at,
       raw_app_meta_data, raw_user_meta_data, is_sso_user, is_anonymous,
       -- GoTrue's login query scans these as strings; they MUST be '' not NULL,
       -- or sign-in fails with "Database error querying schema".
       confirmation_token, recovery_token, email_change, email_change_token_new,
       email_change_token_current, phone_change, phone_change_token, reauthentication_token
     ) values (
       '00000000-0000-0000-0000-000000000000', gen_random_uuid(),
       'authenticated', 'authenticated', 'owner@acme.com',
       extensions.crypt('TEMP-PASSWORD', extensions.gen_salt('bf')),
       now(), now(), now(),
       '{"provider":"email","providers":["email"]}'::jsonb, '{}'::jsonb, false, false,
       '', '', '', '', '', '', '', ''
     )
     returning id, email
   ),
   ident as (
     insert into auth.identities (provider_id, user_id, identity_data, provider,
                                  last_sign_in_at, created_at, updated_at)
     select id::text, id,
            jsonb_build_object('sub', id::text, 'email', email, 'email_verified', true),
            'email', now(), now(), now()
     from new_user returning user_id
   )
   insert into public.profiles (id, team_id, is_admin, email)
   select id, 'acme', false, email from new_user;
   ```

3. Give each person their email + temporary password. The dashboard's
   **Forgot password?** link emails a reset so they can set their own.

That's it — same dashboard, same feed, isolated workspace. To seed a team's
starting buy box (their real counties/criteria), upsert a `team_state` row with
`key = 'buybox'` and the buy-box JSON as `data`.

## Checking it worked

Sign in on two devices (or one normal + one private window) with the same
account: add a note or a list on one — it appears on the other within a second
or two. Make a list **Shared with team**, sign in as a different teammate, and
the list is there.

## Notes

- **Password resets** use Supabase's built-in email. For heavy use, attach a
  custom SMTP sender in the project's Auth settings.
- **Authorized redirect/site URL**: set the project's Auth **Site URL** to the
  app origin (`https://frankhatfellaaiagent-del.github.io`, plus any custom
  domain) so password-reset links point back to the app.
- The old Firebase path is retired; there is no `firestore.rules` anymore — the
  schema and policies live in the project's SQL migrations.
