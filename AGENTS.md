
- estamos em um ambiente grafico linux ubuntu 26.04 com wayland e gnome, em um terminal bash sem permissoes de root.
- se precisar que eu faça qualquer operação como root, como instalar algo com apt, me peça que eu instalo.
- se tiver qualquer tipo de duvida pergunte antes de prosseguir.
- converse comigo apenas em portugues brasileiro. faça todo o resto em ingles americano.
- nunca utilize o diretorio /tmp. ao inves disso, se precisar criar coisas temporarias, utilize o diretorio local ./tmp
- nunca utilize sub agentes. faça tudo na sessão principal.
- sempre planeje tudo em um arquivo PLAN.md e crie uma lista de tarefas em CHECKLIST.md. mantenha ambos sempre atualizados a medida que for trabalhando.
- mantenha sempre atualizados os arquivos FEATURES_SUMMARY.md que tera a lista resumida de todas as funcionalidades implementadas e FEATURES_DETAIL.md que terá a explicação detalhada de todas as funcionalidades implementadas.
- utilize qualquer meio ou tecnologia necessaria como forma de teste ou de etapa intermediaria para alcancar os objetivos finais.
- nunca utilize operacoes de escrita do git, como checkout. utilize apenas operacoes de leitura como log e status.

- estamos criando um aplicativo CLI em varias linguagens e runtimes diferentes, sendo que a implementação em golang é a referencia, que inicia um servidor web que expoe uma api rest http para realizar operacoes de baixo nivel no sistema onde o servidor esta rodando, como manipulacao do sistema de arquivos: criar, modificar, deletar, renomear, ler e obter meta-informacoes de arquivos e diretorios, bem como quaisquer outras operacoes que queiramos implementar (como rodar um comando shell e retornar o return code, stdout e stderr).
- deve haver um endpoint 'help' que retorna a lista com url, nome, descricao, formato de parametros e todos os detalhes de todas as rotas implementadas
- o servidor deve aceitar requisicoes de qualquer origem (CORS Cross-Origin Resource Sharing) porque a razao de existencia do servidor é justamente permitir criação de aplicacoes web frontend com acesso a certos recursos nativos do sistema do usuario, permitindo por exemplo criar um gerenciador de arquivos real apenas como um app web frontend sem utilizar electron, nw.js ou outras tecnologias quaisquer.
- devem ser sempre criados testes automatizados para testar todas as funcionalidades da aplicacao, que devem ser executados sempre para evitar que alteracoes na aplicacao gerem regressoes.
- ao realizar alteracoes nos diversos runtimes/ports do serviço, faça na seguinte ordem: go, rust, python, nodejs, php, ruby, kotlin, dotnet, c++
