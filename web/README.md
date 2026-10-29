# Biblioteca Pessoal — beta web

Aplicação web de teste, publicada separadamente da página informativa e da edição local.

Cada conta cria ou aceita convite para uma biblioteca própria. A base de teste usa Supabase Auth, Postgres e políticas RLS; não importe dados privados da aplicação local.

## Desenvolvimento

1. Copie `.env.example` para `.env` e preencha `VITE_SUPABASE_URL` e `VITE_SUPABASE_PUBLISHABLE_KEY`.
2. Execute `npm ci` e `npm run dev`.
3. O deploy beta é feito pelo Cloudflare Pages a partir da pasta `web/` na branch `main`.

A página informativa permanece em `landing/` e é publicada por um projeto Pages separado.
