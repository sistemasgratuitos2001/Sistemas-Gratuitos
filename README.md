# Biblioteca Pessoal

Sistema gratuito para catálogos pessoais e pequenas bibliotecas: acervo, leitores, empréstimos e reservas. O projeto mantém duas formas de uso: a aplicação local já existente e uma nova aplicação web multi-biblioteca.

## Aplicação local (Windows)

Execute `Iniciar Biblioteca.bat`. Escolha **L** para abrir somente neste computador (padrão) ou **R** para permitir acesso a outros computadores da mesma rede local. O modo R não publica o servidor na Internet. Os dados locais ficam em `%LOCALAPPDATA%\BibliotecaPessoal-<id>` e permanecem independentes da nuvem.

A cópia local usa Flask e SQLite, permite importar CSV e Excel, guarda capas no computador e exige que contas antigas com `1234` definam uma senha própria. As 262 thumbs temáticas ficam em `cdd_thumbs_assets.zip` e são instaladas localmente sem substituir personalizações existentes. Consulte este README e as mensagens do iniciador antes de alterar os dados.

## Aplicação web

A aplicação web fica em [`web/`](web/README.md), usa React/Vite, Cloudflare Pages e Supabase. A base online começa vazia para que dados de leitores e itens da instalação local não sejam enviados sem uma escolha explícita.

- Repositório do código: https://github.com/sistemasgratuitos2001/Sistemas-Gratuitos
- Aplicação web: será publicada no endereço Pages associado ao repositório após a autorização da integração GitHub↔Cloudflare.
- Configuração, autenticação, variáveis públicas e limites: [`web/README.md`](web/README.md).

Na aplicação web, **Criar nova biblioteca** no início de sessão cria uma biblioteca privada e a primeira conta torna-se proprietária. A equipa cria colaboradores e leitores na área **Equipa > Convidar pessoa**; os links são de uso único e expiram em sete dias. Isso permite cadastro no próprio ecrã inicial sem dar a qualquer novo utilizador acesso a bibliotecas de terceiros.

## Dados e segurança

O código não deve conter banco SQLite, cópias de capas particulares, configuração de e-mail, senhas ou chaves administrativas. O `.gitignore` protege esses ficheiros. O navegador recebe somente a chave publicável do Supabase; a chave `service_role` nunca é usada no frontend.

A área online aplica RLS (Row Level Security) por biblioteca. Empréstimo, renovação, devolução e reserva passam por operações no banco com verificação do utilizador e bloqueio do exemplar, para evitar dois empréstimos simultâneos do último exemplar.

## Licença e contribuições

O código está público para consulta e colaboração. Ainda não foi atribuída uma licença formal; confirme as condições antes de reutilizar o projeto fora deste repositório.

