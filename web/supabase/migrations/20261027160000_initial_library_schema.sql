-- Multi-tenant library schema. Every tenant-owned row is protected by RLS.
create extension if not exists pgcrypto with schema extensions;
create schema if not exists private;
revoke all on schema private from public, anon;
grant usage on schema private to authenticated;

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default '',
  created_at timestamptz not null default now()
);
create table public.libraries (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(trim(name)) between 2 and 120),
  created_at timestamptz not null default now()
);
create table public.library_members (
  library_id uuid not null references public.libraries(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null check (role in ('owner','admin','staff','reader')),
  created_at timestamptz not null default now(),
  primary key (library_id,user_id)
);
create index library_members_user_idx on public.library_members(user_id,library_id);

create table public.team_invites (
  id uuid primary key default gen_random_uuid(),
  library_id uuid not null references public.libraries(id) on delete cascade,
  email text not null,
  role text not null check (role in ('admin','staff','reader')),
  token_hash text not null unique,
  invited_by uuid not null references auth.users(id) on delete cascade,
  expires_at timestamptz not null default (now() + interval '7 days'),
  accepted_at timestamptz,
  created_at timestamptz not null default now()
);
create index team_invites_lookup_idx on public.team_invites(lower(email),expires_at) where accepted_at is null;

create table public.books (
  id uuid primary key default gen_random_uuid(),
  library_id uuid not null references public.libraries(id) on delete cascade,
  title text not null check (char_length(trim(title)) between 1 and 500),
  subtitle text not null default '',
  author text not null default '',
  coauthors text not null default '',
  isbn text not null default '',
  publisher text not null default '',
  publication_year text not null default '',
  edition text not null default '',
  volume text not null default '',
  cdd text not null default '',
  media text not null default 'Livro',
  copies integer not null default 1 check (copies between 1 and 10000),
  cover_path text,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index books_library_title_idx on public.books(library_id,lower(title));
create index books_library_cdd_idx on public.books(library_id,cdd);
create index books_isbn_idx on public.books(library_id,isbn) where isbn <> '';

create table public.readers (
  id uuid primary key default gen_random_uuid(),
  library_id uuid not null references public.libraries(id) on delete cascade,
  full_name text not null check (char_length(trim(full_name)) between 1 and 200),
  registration_id text not null default '',
  email text not null default '',
  phone text not null default '',
  user_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);
create unique index readers_library_registration_uq on public.readers(library_id,registration_id) where registration_id <> '';
create index readers_library_name_idx on public.readers(library_id,lower(full_name));
create index readers_user_idx on public.readers(user_id,library_id);

create table public.loans (
  id uuid primary key default gen_random_uuid(),
  library_id uuid not null references public.libraries(id) on delete cascade,
  book_id uuid not null references public.books(id) on delete restrict,
  reader_id uuid not null references public.readers(id) on delete restrict,
  checked_out_at timestamptz not null default now(),
  due_at date not null,
  returned_at timestamptz,
  renewals smallint not null default 0 check (renewals between 0 and 2),
  status text not null default 'active' check (status in ('active','returned')),
  created_by uuid references auth.users(id) on delete set null,
  check ((status='active' and returned_at is null) or (status='returned' and returned_at is not null))
);
create index loans_active_book_idx on public.loans(library_id,book_id) where status='active';
create index loans_reader_idx on public.loans(library_id,reader_id,checked_out_at desc);
create index loans_due_idx on public.loans(library_id,due_at) where status='active';

create table public.reservations (
  id uuid primary key default gen_random_uuid(),
  library_id uuid not null references public.libraries(id) on delete cascade,
  book_id uuid not null references public.books(id) on delete restrict,
  reader_id uuid not null references public.readers(id) on delete restrict,
  status text not null default 'active' check (status in ('active','ready','fulfilled','cancelled')),
  created_at timestamptz not null default now(),
  fulfilled_at timestamptz
);
create unique index reservations_one_open_per_reader on public.reservations(book_id,reader_id) where status in ('active','ready');
create index reservations_queue_idx on public.reservations(library_id,book_id,created_at) where status in ('active','ready');

create table public.notifications (
  id uuid primary key default gen_random_uuid(),
  library_id uuid not null references public.libraries(id) on delete cascade,
  reader_id uuid not null references public.readers(id) on delete cascade,
  message text not null,
  read_at timestamptz,
  created_at timestamptz not null default now()
);
create index notifications_reader_idx on public.notifications(library_id,reader_id,created_at desc);

create table public.cdd_terms (
  code text primary key,
  label text not null,
  synonyms text[] not null default '{}'
);

create table public.audit_events (
  id bigint generated always as identity primary key,
  library_id uuid not null references public.libraries(id) on delete cascade,
  actor_id uuid references auth.users(id) on delete set null,
  action text not null,
  entity text not null,
  entity_id uuid,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index audit_library_created_idx on public.audit_events(library_id,created_at desc);

create or replace function private.is_member(p_library_id uuid, p_roles text[] default null)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.library_members m
    where m.library_id=p_library_id and m.user_id=(select auth.uid())
      and (p_roles is null or m.role=any(p_roles))
  );
$$;
create or replace function private.shares_library(p_user_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.library_members mine
    join public.library_members other on other.library_id=mine.library_id
    where mine.user_id=(select auth.uid()) and other.user_id=p_user_id
  );
$$;
revoke all on function private.is_member(uuid,text[]) from public,anon;
revoke all on function private.shares_library(uuid) from public,anon;
grant execute on function private.is_member(uuid,text[]) to authenticated;
grant execute on function private.shares_library(uuid) to authenticated;

alter table public.profiles enable row level security;
alter table public.libraries enable row level security;
alter table public.library_members enable row level security;
alter table public.team_invites enable row level security;
alter table public.books enable row level security;
alter table public.readers enable row level security;
alter table public.loans enable row level security;
alter table public.reservations enable row level security;
alter table public.notifications enable row level security;
alter table public.cdd_terms enable row level security;
alter table public.audit_events enable row level security;

grant select,update on public.profiles to authenticated;
grant select,update on public.libraries to authenticated;
grant select on public.library_members to authenticated;
grant insert on public.team_invites to authenticated;
grant select,insert,update,delete on public.books to authenticated;
grant select,insert,update,delete on public.readers to authenticated;
grant select on public.loans,public.reservations,public.notifications,public.audit_events to authenticated;
grant update on public.notifications to authenticated;
grant select on public.cdd_terms to anon,authenticated;
grant usage,select on sequence public.audit_events_id_seq to authenticated;

grant select on public.cdd_terms to anon;
create policy cdd_read on public.cdd_terms for select to anon,authenticated using (true);
create policy profile_read on public.profiles for select to authenticated using (id=(select auth.uid()) or private.shares_library(id));
create policy profile_update on public.profiles for update to authenticated using (id=(select auth.uid())) with check (id=(select auth.uid()));
create policy library_read on public.libraries for select to authenticated using (private.is_member(id));
create policy library_update on public.libraries for update to authenticated using (private.is_member(id,array['owner','admin'])) with check (private.is_member(id,array['owner','admin']));
create policy member_read on public.library_members for select to authenticated using (user_id=(select auth.uid()) or private.is_member(library_id,array['owner','admin','staff']));
create policy invite_create on public.team_invites for insert to authenticated with check (
  invited_by=(select auth.uid()) and expires_at <= now()+interval '7 days'
  and ((role in ('staff','reader') and private.is_member(library_id,array['owner','admin','staff']))
    or (role='admin' and private.is_member(library_id,array['owner'])))
);
create policy book_read on public.books for select to authenticated using (private.is_member(library_id));
create policy book_create on public.books for insert to authenticated with check (private.is_member(library_id,array['owner','admin','staff']));
create policy book_update on public.books for update to authenticated using (private.is_member(library_id,array['owner','admin','staff'])) with check (private.is_member(library_id,array['owner','admin','staff']));
create policy book_delete on public.books for delete to authenticated using (private.is_member(library_id,array['owner','admin','staff']));
create policy reader_read on public.readers for select to authenticated using (private.is_member(library_id,array['owner','admin','staff']) or user_id=(select auth.uid()));
create policy reader_create on public.readers for insert to authenticated with check (private.is_member(library_id,array['owner','admin','staff']));
create policy reader_update on public.readers for update to authenticated using (private.is_member(library_id,array['owner','admin','staff'])) with check (private.is_member(library_id,array['owner','admin','staff']));
create policy reader_delete on public.readers for delete to authenticated using (private.is_member(library_id,array['owner','admin','staff']));
create policy loan_read on public.loans for select to authenticated using (
  private.is_member(library_id,array['owner','admin','staff']) or exists (
    select 1 from public.readers r where r.id=reader_id and r.user_id=(select auth.uid())
  )
);
create policy reservation_read on public.reservations for select to authenticated using (
  private.is_member(library_id,array['owner','admin','staff']) or exists (
    select 1 from public.readers r where r.id=reader_id and r.user_id=(select auth.uid())
  )
);
create policy notification_read on public.notifications for select to authenticated using (
  private.is_member(library_id,array['owner','admin','staff']) or exists (
    select 1 from public.readers r where r.id=reader_id and r.user_id=(select auth.uid())
  )
);
create policy notification_mark_read on public.notifications for update to authenticated using (
  exists (select 1 from public.readers r where r.id=reader_id and r.user_id=(select auth.uid()))
) with check (exists (select 1 from public.readers r where r.id=reader_id and r.user_id=(select auth.uid())));
create policy event_read on public.audit_events for select to authenticated using (private.is_member(library_id,array['owner','admin','staff']));

-- Auth signup: an invitation joins the named library; otherwise the signup creates a new private library.
create or replace function private.handle_new_user()
returns trigger language plpgsql security definer set search_path = '' as $$
declare v_name text; v_library_id uuid; v_role text; v_invite_id uuid; v_token text;
begin
  v_name := coalesce(nullif(trim(new.raw_user_meta_data->>'display_name'),''), split_part(new.email,'@',1));
  v_token := nullif(new.raw_user_meta_data->>'invite_token','');
  insert into public.profiles(id,display_name) values(new.id,v_name);
  if v_token is not null then
    select i.id,i.library_id,i.role into v_invite_id,v_library_id,v_role
    from public.team_invites i
    where lower(i.email)=lower(new.email)
      and i.token_hash=encode(extensions.digest(convert_to(v_token,'UTF8'),'sha256'),'hex')
      and i.accepted_at is null and i.expires_at>now()
    for update;
    if v_invite_id is null then raise exception 'Convite inválido ou expirado.'; end if;
    update public.team_invites set accepted_at=now() where id=v_invite_id;
  else
    v_library_id:=gen_random_uuid();
    if char_length(trim(coalesce(new.raw_user_meta_data->>'library_name',''))) < 2 then
      raise exception 'Informe o nome da biblioteca.';
    end if;
    insert into public.libraries(id,name) values(v_library_id,trim(new.raw_user_meta_data->>'library_name'));
    v_role:='owner';
  end if;
  insert into public.library_members(library_id,user_id,role) values(v_library_id,new.id,v_role);
  if v_role='reader' then
    update public.readers set user_id=new.id where library_id=v_library_id and lower(email)=lower(new.email) and user_id is null;
    if not found then
      insert into public.readers(library_id,full_name,email,user_id)
      values(v_library_id,v_name,coalesce(new.email,''),new.id);
    end if;
  end if;
  return new;
end;
$$;
revoke all on function private.handle_new_user() from public,anon,authenticated;
create trigger on_auth_user_created after insert on auth.users for each row execute function private.handle_new_user();

-- Privileged circulation is private and validates identity/tenant ownership itself. Public RPC wrappers are invoker-only.
create or replace function private.checkout_book(p_library_id uuid,p_book_id uuid,p_reader_id uuid,p_due_at date)
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_copies integer; v_active integer; v_user_role text; v_first_reader uuid; v_status text; v_loan uuid;
begin
  if auth.uid() is null then raise exception 'Sessão necessária.'; end if;
  select m.role into v_user_role from public.library_members m where m.library_id=p_library_id and m.user_id=auth.uid();
  if v_user_role is null then raise exception 'Sem acesso a esta biblioteca.'; end if;
  if v_user_role not in ('owner','admin','staff') and not exists(
    select 1 from public.readers r where r.id=p_reader_id and r.library_id=p_library_id and r.user_id=auth.uid()
  ) then raise exception 'Só pode solicitar o próprio empréstimo.'; end if;
  select b.copies into v_copies from public.books b where b.id=p_book_id and b.library_id=p_library_id for update;
  if v_copies is null then raise exception 'Material não encontrado.'; end if;
  if not exists(select 1 from public.readers r where r.id=p_reader_id and r.library_id=p_library_id) then raise exception 'Leitor não encontrado nesta biblioteca.'; end if;
  select count(*) into v_active from public.loans l where l.book_id=p_book_id and l.library_id=p_library_id and l.status='active';
  if v_active>=v_copies then raise exception 'Não há exemplar disponível.'; end if;
  select q.reader_id,q.status into v_first_reader,v_status from public.reservations q
    where q.book_id=p_book_id and q.library_id=p_library_id and q.status in ('ready','active') order by q.created_at,q.id limit 1;
  if v_first_reader is not null and v_first_reader<>p_reader_id then raise exception 'Há uma reserva anterior para este material.'; end if;
  insert into public.loans(library_id,book_id,reader_id,due_at,created_by) values(p_library_id,p_book_id,p_reader_id,p_due_at,auth.uid()) returning id into v_loan;
  if v_first_reader=p_reader_id then update public.reservations set status='fulfilled',fulfilled_at=now() where book_id=p_book_id and reader_id=p_reader_id and library_id=p_library_id and status in ('ready','active'); end if;
  insert into public.audit_events(library_id,actor_id,action,entity,entity_id) values(p_library_id,auth.uid(),'empréstimo','loan',v_loan);
  return v_loan;
end;
$$;
create or replace function private.renew_loan(p_library_id uuid,p_loan_id uuid)
returns date language plpgsql security definer set search_path = '' as $$
declare v_due date; v_count integer; v_reader uuid;
begin
  if auth.uid() is null or not private.is_member(p_library_id) then raise exception 'Sem acesso a esta biblioteca.'; end if;
  select l.due_at,l.renewals,l.reader_id into v_due,v_count,v_reader from public.loans l where l.id=p_loan_id and l.library_id=p_library_id and l.status='active' for update;
  if v_due is null then raise exception 'Empréstimo ativo não encontrado.'; end if;
  if not private.is_member(p_library_id,array['owner','admin','staff']) and not exists(select 1 from public.readers r where r.id=v_reader and r.user_id=auth.uid()) then raise exception 'Só pode renovar o próprio empréstimo.'; end if;
  if v_count>=2 then raise exception 'Limite de renovações atingido.'; end if;
  if exists(select 1 from public.reservations q where q.book_id=(select l.book_id from public.loans l where l.id=p_loan_id) and q.library_id=p_library_id and q.status in ('active','ready')) then raise exception 'Este material tem reserva pendente.'; end if;
  update public.loans set due_at=greatest(v_due,current_date)+30,renewals=renewals+1 where id=p_loan_id returning due_at into v_due;
  insert into public.audit_events(library_id,actor_id,action,entity,entity_id) values(p_library_id,auth.uid(),'renovação','loan',p_loan_id);
  return v_due;
end;
$$;
create or replace function private.return_loan(p_library_id uuid,p_loan_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
declare v_book uuid; v_reader uuid; v_copies integer; v_active integer; v_next uuid;
begin
  if auth.uid() is null or not private.is_member(p_library_id,array['owner','admin','staff']) then raise exception 'Apenas a equipa pode registar devoluções.'; end if;
  select l.book_id,l.reader_id into v_book,v_reader from public.loans l where l.id=p_loan_id and l.library_id=p_library_id and l.status='active' for update;
  if v_book is null then raise exception 'Empréstimo ativo não encontrado.'; end if;
  perform 1 from public.books b where b.id=v_book and b.library_id=p_library_id for update;
  update public.loans set status='returned',returned_at=now() where id=p_loan_id;
  select b.copies into v_copies from public.books b where b.id=v_book;
  select count(*) into v_active from public.loans l where l.book_id=v_book and l.library_id=p_library_id and l.status='active';
  if v_active<v_copies then
    select q.reader_id into v_next from public.reservations q where q.book_id=v_book and q.library_id=p_library_id and q.status='active' order by q.created_at,q.id limit 1 for update;
    if v_next is not null then
      update public.reservations set status='ready' where book_id=v_book and reader_id=v_next and library_id=p_library_id and status='active';
      insert into public.notifications(library_id,reader_id,message) values(p_library_id,v_next,'Um exemplar reservado está disponível para levantamento.');
    end if;
  end if;
  insert into public.audit_events(library_id,actor_id,action,entity,entity_id) values(p_library_id,auth.uid(),'devolução','loan',p_loan_id);
end;
$$;
create or replace function private.reserve_book(p_library_id uuid,p_book_id uuid,p_reader_id uuid)
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_copies integer; v_active integer; v_user_role text; v_reservation uuid;
begin
  if auth.uid() is null then raise exception 'Sessão necessária.'; end if;
  select m.role into v_user_role from public.library_members m where m.library_id=p_library_id and m.user_id=auth.uid();
  if v_user_role is null then raise exception 'Sem acesso a esta biblioteca.'; end if;
  if v_user_role not in ('owner','admin','staff') and not exists(select 1 from public.readers r where r.id=p_reader_id and r.library_id=p_library_id and r.user_id=auth.uid()) then raise exception 'Só pode reservar para si.'; end if;
  select b.copies into v_copies from public.books b where b.id=p_book_id and b.library_id=p_library_id for update;
  if v_copies is null then raise exception 'Material não encontrado.'; end if;
  if not exists(select 1 from public.readers r where r.id=p_reader_id and r.library_id=p_library_id) then raise exception 'Leitor não encontrado.'; end if;
  select count(*) into v_active from public.loans l where l.book_id=p_book_id and l.library_id=p_library_id and l.status='active';
  if v_active<v_copies then raise exception 'Há exemplares disponíveis para empréstimo.'; end if;
  insert into public.reservations(library_id,book_id,reader_id) values(p_library_id,p_book_id,p_reader_id) returning id into v_reservation;
  insert into public.audit_events(library_id,actor_id,action,entity,entity_id) values(p_library_id,auth.uid(),'reserva','reservation',v_reservation);
  return v_reservation;
end;
$$;
revoke all on function private.checkout_book(uuid,uuid,uuid,date) from public,anon;
revoke all on function private.renew_loan(uuid,uuid) from public,anon;
revoke all on function private.return_loan(uuid,uuid) from public,anon;
revoke all on function private.reserve_book(uuid,uuid,uuid) from public,anon;
grant execute on function private.checkout_book(uuid,uuid,uuid,date) to authenticated;
grant execute on function private.renew_loan(uuid,uuid) to authenticated;
grant execute on function private.return_loan(uuid,uuid) to authenticated;
grant execute on function private.reserve_book(uuid,uuid,uuid) to authenticated;
create or replace function public.checkout_book(p_library_id uuid,p_book_id uuid,p_reader_id uuid,p_due_at date)
returns uuid language sql security invoker set search_path = '' as $$ select private.checkout_book(p_library_id,p_book_id,p_reader_id,p_due_at); $$;
create or replace function public.renew_loan(p_library_id uuid,p_loan_id uuid)
returns date language sql security invoker set search_path = '' as $$ select private.renew_loan(p_library_id,p_loan_id); $$;
create or replace function public.return_loan(p_library_id uuid,p_loan_id uuid)
returns void language sql security invoker set search_path = '' as $$ select private.return_loan(p_library_id,p_loan_id); $$;
create or replace function public.reserve_book(p_library_id uuid,p_book_id uuid,p_reader_id uuid)
returns uuid language sql security invoker set search_path = '' as $$ select private.reserve_book(p_library_id,p_book_id,p_reader_id); $$;
grant execute on function public.checkout_book(uuid,uuid,uuid,date),public.renew_loan(uuid,uuid),public.return_loan(uuid,uuid),public.reserve_book(uuid,uuid,uuid) to authenticated;

insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values('book-covers','book-covers',false,5242880,array['image/jpeg','image/png','image/webp','image/avif'])
on conflict(id) do update set public=false,file_size_limit=5242880,allowed_mime_types=excluded.allowed_mime_types;
create policy cover_member_read on storage.objects for select to authenticated using (
  bucket_id='book-covers' and (storage.foldername(name))[1] ~* '^[0-9a-f-]{36}$'
  and private.is_member(((storage.foldername(name))[1])::uuid)
);
create policy cover_staff_insert on storage.objects for insert to authenticated with check (
  bucket_id='book-covers' and (storage.foldername(name))[1] ~* '^[0-9a-f-]{36}$'
  and private.is_member(((storage.foldername(name))[1])::uuid,array['owner','admin','staff'])
);
create policy cover_staff_update on storage.objects for update to authenticated using (
  bucket_id='book-covers' and (storage.foldername(name))[1] ~* '^[0-9a-f-]{36}$'
  and private.is_member(((storage.foldername(name))[1])::uuid,array['owner','admin','staff'])
) with check (
  bucket_id='book-covers' and (storage.foldername(name))[1] ~* '^[0-9a-f-]{36}$'
  and private.is_member(((storage.foldername(name))[1])::uuid,array['owner','admin','staff'])
);
create policy cover_staff_delete on storage.objects for delete to authenticated using (
  bucket_id='book-covers' and (storage.foldername(name))[1] ~* '^[0-9a-f-]{36}$'
  and private.is_member(((storage.foldername(name))[1])::uuid,array['owner','admin','staff'])
);



