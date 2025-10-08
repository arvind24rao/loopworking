-- Loop and thread (if not exist). Adjust names if you like.
insert into public.loops (id, name, created_by)
values ('e94bd651-5bac-4e39-8537-fe8c788c1475','AB Test Loop','b8d99c3c-0d3a-4773-a324-a6bc60dee64e')
on conflict (id) do nothing;

insert into public.threads (id, loop_id)
values ('b01164e6-c719-4fb1-b2d0-85755e7ebf38','e94bd651-5bac-4e39-8537-fe8c788c1475')
on conflict (id) do nothing;

-- Members (A, B, BOT) for that loop
insert into public.members (loop_id, profile_id, role) values
  ('e94bd651-5bac-4e39-8537-fe8c788c1475','b8d99c3c-0d3a-4773-a324-a6bc60dee64e','member'),
  ('e94bd651-5bac-4e39-8537-fe8c788c1475','0dd8b495-6a25-440d-a6e4-d8b7a77bc688','member'),
  ('e94bd651-5bac-4e39-8537-fe8c788c1475','b59042b5-9cee-4c20-ad5d-8a0ad42cb374','agent')
on conflict (loop_id, profile_id) do nothing;
