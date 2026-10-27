# Biblioteca Web

Aplicação para pequenas bibliotecas e acervos pessoais. React/Vite no Cloudflare Pages; Supabase Auth, PostgreSQL e Storage no backend.

## Desenvolvimento

1. Instale Node.js 20.19+ (o build usa Vite 8).
2. Copie `.env.example` para `.env.local` e configure:
   - `VITE_SUPABASE_URL`
   - `VITE_SUPABASE_PUBLISHABLE_KEY`
3. `npm ci` e `npm run dev`.
4. O schema completo e os índices estão em `supabase/migrations/`; execute também `supabase/seed.sql` para preencher `cdd_terms` com as classes e os sinónimos de `cdd_base.txt`.

Nunca coloque uma chave `service_role` ou segredo SMTP em `VITE_*`, no código do navegador ou no repositório.

## Publicação e autenticação

- O projeto Pages usa diretório raiz `web`, comando `npm run build` e saída `dist`.
- Configure no Pages as variáveis públicas `VITE_SUPABASE_URL` e `VITE_SUPABASE_PUBLISHABLE_KEY`.
- No Supabase Auth, configure o URL do site e os URLs de redirecionamento para o domínio `*.pages.dev` publicado. Para o fluxo de produção, configure SMTP próprio, confirmação de e-mail e recuperação de senha.
- Configure a política de cadastro do Supabase de acordo com o lançamento pretendido. O cadastro aberto sempre cria uma biblioteca privada nova; entrar numa biblioteca existente exige um convite válido.

## Funções presentes nesta versão web

- Criação de conta na página inicial, início de sessão e recuperação de senha.
- Bibliotecas isoladas por RLS; perfis proprietário, administrador, colaborador e leitor.
- Convites de uso único por link, associados ao e-mail e válidos por sete dias.
- Catálogo, capas locais no Storage privado, sugestão de CDD baseada no índice e nos títulos já catalogados.
- Importação CSV progressiva (ficheiro até 2 GB; lotes de até 300 linhas). Em caso de interrupção, o mesmo CSV pode ser reimportado: a restrição de duplicados evita repetir títulos já gravados. Nesta versão web, Excel ainda não está disponível; a aplicação local continua a aceitar Excel.
- Leitores, consulta privada de empréstimos, renovações, reservas e notificações de levantamento.
- Relatórios básicos e registo de auditoria.

## Base de dados

As migrações ativam RLS em todas as tabelas expostas, limitam dados à biblioteca associada à sessão e protegem imagens do Storage com caminhos prefixados pelo ID da biblioteca. As rotinas privilegiadas ficam no schema `private`, fora da API pública, e validam novamente o utilizador antes de executar. Nunca enviar a base SQLite local automaticamente para a nuvem.

