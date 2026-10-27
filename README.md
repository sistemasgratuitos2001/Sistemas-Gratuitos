# Biblioteca Pessoal

Aplicação para gerir o acervo de uma casa, escritório ou pequena biblioteca. O produto pretendido é **instalado e usado localmente**: cada instalação mantém a sua própria base de dados, leitores, empréstimos e capas.

## Estado atual

A versão funcional desta fase usa Python, Flask e SQLite. Está em testes; ainda não há instalador oficial nem atualização automática. O código também contém um protótipo web antigo em `web/`, mantido apenas como referência técnica. Ele não é a forma de utilização nem de distribuição da biblioteca.

## Executar no Windows

1. Instale Python 3, se ainda não estiver instalado.
2. Execute `Iniciar Biblioteca.bat`.
3. Escolha **L** para usar somente neste computador ou **R** para testes na rede local.

No modo de rede, o computador que iniciou a biblioteca precisa permanecer ligado. A base fica nele; os outros computadores acedem pela rede local. Este modo é experimental e não deve ser publicado na Internet nem encaminhado pelo router.

A primeira execução cria o administrador e apresenta uma senha temporária única na janela de arranque. Guarde-a para entrar e altere-a quando o sistema pedir. As contas e palavras-passe ficam na base local.

## Dados locais e atualizações

A base SQLite e as capas ficam em `%LOCALAPPDATA%\BibliotecaPessoal-<id>`, separadas da pasta do programa para preservar os dados quando o código for atualizado. Faça cópias de segurança regulares dessa pasta.

As versões do programa serão publicadas em **GitHub Releases** depois de existir um pacote de instalação adequado. Por enquanto, este repositório é código-fonte para desenvolvimento; descarregar o ZIP do GitHub não equivale a instalar o programa.

O projeto está a ser preparado para Windows, macOS e Linux, mas o iniciador atual é específico do Windows. A distribuição multiplataforma será definida numa etapa posterior.

## Privacidade

O repositório não deve conter bases SQLite de utilizadores, capas privadas, ficheiros de e-mail ou segredos. Esses ficheiros são excluídos pelo `.gitignore`. A aplicação não envia automaticamente os dados locais para a nuvem.

## Código e colaboração

- Código-fonte: https://github.com/sistemasgratuitos2001/Sistemas-Gratuitos
- Página informativa: https://biblioteca-pessoal-5tk.pages.dev

Consulte também [o protótipo web arquivado](web/README.md).