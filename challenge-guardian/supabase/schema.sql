-- Challenge Guardian multi-tenant schema.
-- Run this in the Supabase SQL editor (or via supabase db push).

-- One row per signed-up trader.
create table if not exists tenants (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  propr_api_key text not null,
  telegram_token text,
  telegram_chat_id text,
  discord_webhook text,
  preset text not null default '1step',
  balance numeric,                       -- fallback when auto-detection fails
  enable_actions boolean not null default false,
  auto_flatten_at numeric,               -- e.g. 0.95; null = off
  digest_hour int default 20,            -- UTC hour; null = off
  active boolean not null default true,
  updated_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (user_id)
);

create or replace function touch_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end $$ language plpgsql;

drop trigger if exists tenants_touch on tenants;
create trigger tenants_touch before update on tenants
  for each row execute function touch_updated_at();

alter table tenants enable row level security;
drop policy if exists "own tenant" on tenants;
create policy "own tenant" on tenants
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Equity time series the dashboard charts (written by the worker).
create table if not exists equity_samples (
  id bigint generated always as identity primary key,
  tenant_id uuid not null references tenants (id) on delete cascade,
  account_label text not null,
  equity numeric not null,
  daily_floor numeric,
  dd_floor numeric,
  peak numeric,
  positions int,
  created_at timestamptz not null default now()
);
create index if not exists equity_samples_tenant_time
  on equity_samples (tenant_id, created_at desc);

alter table equity_samples enable row level security;
drop policy if exists "own samples" on equity_samples;
create policy "own samples" on equity_samples for select using (
  exists (select 1 from tenants t where t.id = tenant_id and t.user_id = auth.uid())
);

-- Alert/event feed shown on the dashboard (written by the worker).
create table if not exists guardian_events (
  id bigint generated always as identity primary key,
  tenant_id uuid not null references tenants (id) on delete cascade,
  message text not null,
  created_at timestamptz not null default now()
);
create index if not exists guardian_events_tenant_time
  on guardian_events (tenant_id, created_at desc);

alter table guardian_events enable row level security;
drop policy if exists "own events" on guardian_events;
create policy "own events" on guardian_events for select using (
  exists (select 1 from tenants t where t.id = tenant_id and t.user_id = auth.uid())
);

-- Live updates for the dashboard.
alter publication supabase_realtime add table equity_samples;
alter publication supabase_realtime add table guardian_events;
