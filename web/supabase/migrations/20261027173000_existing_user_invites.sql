alter table public.team_invites add column if not exists accepted_by uuid;

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
    update public.team_invites set accepted_at=now(),accepted_by=new.id where id=v_invite_id;
  else
    v_library_id:=gen_random_uuid();
    if char_length(trim(coalesce(new.raw_user_meta_data->>'library_name',''))) < 2 then
      raise exception 'Informe o nome da biblioteca.';
    end if;
    insert into public.libraries(id,name) values(v_library_id,trim(new.raw_user_meta_data->>'library_name'));
    v_role:='owner';
  end if;
  insert into public.library_members(library_id,user_id,role) values(v_library_id,new.id,v_role)
  on conflict(library_id,user_id) do nothing;
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

create or replace function private.accept_invite(p_token text)
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_user uuid; v_email text; v_library_id uuid; v_role text; v_accepted_at timestamptz; v_accepted_by uuid; v_invite_id uuid; v_name text;
begin
  v_user:=auth.uid();
  if v_user is null then raise exception 'Inicie sessão para aceitar o convite.'; end if;
  select lower(u.email) into v_email from auth.users u where u.id=v_user;
  if v_email is null then raise exception 'Conta autenticada sem e-mail.'; end if;
  select i.id,i.library_id,i.role,i.accepted_at,i.accepted_by
  into v_invite_id,v_library_id,v_role,v_accepted_at,v_accepted_by
  from public.team_invites i
  where lower(i.email)=v_email
    and i.token_hash=encode(extensions.digest(convert_to(p_token,'UTF8'),'sha256'),'hex')
  for update;
  if v_invite_id is null then raise exception 'Convite inválido ou não corresponde ao e-mail desta conta.'; end if;
  if v_accepted_at is not null then
    if v_accepted_by=v_user and exists(select 1 from public.library_members m where m.library_id=v_library_id and m.user_id=v_user) then return v_library_id; end if;
    raise exception 'Este convite já foi utilizado.';
  end if;
  if (select i.expires_at from public.team_invites i where i.id=v_invite_id)<=now() then raise exception 'Este convite expirou.'; end if;
  update public.team_invites set accepted_at=now(),accepted_by=v_user where id=v_invite_id;
  insert into public.library_members(library_id,user_id,role) values(v_library_id,v_user,v_role)
  on conflict(library_id,user_id) do update set role=excluded.role;
  if v_role='reader' then
    select p.display_name into v_name from public.profiles p where p.id=v_user;
    update public.readers set user_id=v_user where library_id=v_library_id and lower(email)=v_email and user_id is null;
    if not found then
      insert into public.readers(library_id,full_name,email,user_id)
      values(v_library_id,coalesce(v_name,split_part(v_email,'@',1)),v_email,v_user);
    end if;
  end if;
  return v_library_id;
end;
$$;
revoke all on function private.accept_invite(text) from public,anon;
grant execute on function private.accept_invite(text) to authenticated;
create or replace function public.accept_invite(p_token text)
returns uuid language sql security invoker set search_path = '' as $$ select private.accept_invite(p_token); $$;
grant execute on function public.accept_invite(text) to authenticated;
