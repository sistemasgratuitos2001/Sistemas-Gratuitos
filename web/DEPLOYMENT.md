# Publicação Cloudflare Pages

O site está ligado ao ramo main do GitHub e usa:

- Diretório de aplicação: web
- Comando: npm ci && npm run build
- Saída: dist
- URL: https://biblioteca-pessoal-5tk.pages.dev
- API Supabase: https://sktzbmqlqlvrjizdosin.supabase.co

Os valores públicos do Supabase estão nas variáveis de build do Pages. Nenhuma chave administrativa fica no navegador.

## Ativação de contas

No Supabase Dashboard, em Authentication -> URL Configuration:

1. Defina o Site URL como https://biblioteca-pessoal-5tk.pages.dev.
2. Adicione https://biblioteca-pessoal-5tk.pages.dev/** aos Redirect URLs.
3. Antes de abrir cadastros ao público, configure SMTP próprio e confirmação de e-mail. O envio padrão do Supabase é limitado para testes.

A página inicial permite criar uma nova biblioteca privada. Para inserir alguém numa biblioteca existente, um proprietário/colaborador cria um convite em Equipa e partilha o link com o endereço de e-mail indicado. Um convite não dá acesso a outras bibliotecas.

## Dados locais

A base SQLite existente não foi migrada. O Supabase começa sem livros, leitores ou utilizadores; cada novo cadastro cria o seu próprio espaço. A classificação CDD do projeto foi carregada na tabela pública de referência.

Ao publicar uma atualização, o Pages compila automaticamente cada commit no ramo main que altere ficheiros dentro de web/.