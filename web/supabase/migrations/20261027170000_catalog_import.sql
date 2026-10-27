create unique index if not exists books_no_duplicate_catalog on public.books(library_id,lower(trim(title)),lower(trim(author)),media);

create or replace function private.import_books(p_library_id uuid,p_rows jsonb)
returns integer language plpgsql security definer set search_path = '' as $$
declare v_role text; v_added integer;
begin
  if auth.uid() is null then raise exception 'Sessão necessária.'; end if;
  select m.role into v_role from public.library_members m where m.library_id=p_library_id and m.user_id=auth.uid();
  if v_role not in ('owner','admin','staff') then raise exception 'A equipa da biblioteca deve importar o catálogo.'; end if;
  if jsonb_typeof(p_rows)<>'array' or jsonb_array_length(p_rows)>300 then raise exception 'Cada lote de importação aceita até 300 linhas.'; end if;
  insert into public.books(library_id,title,subtitle,author,coauthors,isbn,publisher,publication_year,edition,volume,cdd,media,copies,created_by)
  select p_library_id,trim(x.title),coalesce(x.subtitle,''),coalesce(x.author,''),coalesce(x.coauthors,''),coalesce(x.isbn,''),coalesce(x.publisher,''),coalesce(x.publication_year,''),coalesce(x.edition,''),coalesce(x.volume,''),coalesce(x.cdd,''),coalesce(nullif(trim(x.media),''),'Livro'),greatest(1,least(10000,coalesce(nullif(x.copies,'')::integer,1))),auth.uid()
  from jsonb_to_recordset(p_rows) as x(title text,subtitle text,author text,coauthors text,isbn text,publisher text,publication_year text,edition text,volume text,cdd text,media text,copies text)
  where nullif(trim(x.title),'') is not null
  on conflict do nothing;
  get diagnostics v_added=row_count;
  insert into public.audit_events(library_id,actor_id,action,entity,details) values(p_library_id,auth.uid(),'importação CSV','books',jsonb_build_object('inserted',v_added));
  return v_added;
end;
$$;
revoke all on function private.import_books(uuid,jsonb) from public,anon;
grant execute on function private.import_books(uuid,jsonb) to authenticated;
create or replace function public.import_books(p_library_id uuid,p_rows jsonb)
returns integer language sql security invoker set search_path = '' as $$ select private.import_books(p_library_id,p_rows); $$;
grant execute on function public.import_books(uuid,jsonb) to authenticated;
