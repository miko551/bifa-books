-- Run once in Supabase > SQL Editor
create table if not exists users(
  id bigint primary key, first_name text, username text, lang text,
  is_banned boolean default false, created_at timestamptz default now());
create table if not exists categories(
  id serial primary key, name_en text not null, name_om text not null, sort int default 0);
create table if not exists products(
  id uuid primary key default gen_random_uuid(), code text unique not null,
  title text not null, description text, price numeric(12,2) not null, cover_url text,
  category_id int references categories(id) on delete set null,
  status text not null default 'draft', sales int not null default 0,
  created_at timestamptz default now());
create table if not exists product_files(
  id uuid primary key default gen_random_uuid(),
  product_id uuid references products(id) on delete cascade,
  channel_id bigint not null, message_id bigint not null, created_at timestamptz default now());
create table if not exists orders(
  id uuid primary key default gen_random_uuid(), code text unique not null,
  user_id bigint references users(id), product_id uuid references products(id) on delete set null,
  price numeric(12,2) not null, method text not null, payer_info text, screenshot_path text,
  status text not null default 'pending', reject_reason text, delivered boolean default false,
  decided_by bigint, decided_at timestamptz, created_at timestamptz default now());
create index if not exists orders_status on orders(status, created_at desc);
create table if not exists settings(key text primary key, value jsonb not null);

alter table users enable row level security; alter table categories enable row level security;
alter table products enable row level security; alter table product_files enable row level security;
alter table orders enable row level security; alter table settings enable row level security;

insert into categories(name_en,name_om,sort) values
 ('Books','Kitaabota',1),('Courses','Leenjiiwwan',2),('Business','Daldala',3),('Education','Barnoota',4)
on conflict do nothing;
insert into settings(key,value) values('payment','{
 "cbe":{"account":"1000800961835","holder":"MILKIYAS REGASA"},
 "awash":{"account":"013201753751400","holder":"MILKIYAS REGASA"},
 "telebirr":{"account":"0954996766","holder":"MILKIYAS REGASA"}}') on conflict do nothing;
insert into storage.buckets(id,name,public) values('covers','covers',true),('payments','payments',false)
on conflict do nothing;
