As Cenas do Solin chegam ao macOS: combine câmeras e mídias e escolha o que mostrar no projetor e no Zoom, com uma cena independente para cada saída. Também ficou mais fácil preparar as mídias de discursos públicos, operar câmeras PTZ e gravar reuniões com arquivos menores. No Windows, foram corrigidos ruídos de fundo que podiam ocorrer ao reproduzir vídeos e áudios em computadores mais modestos.

### Notas de atualização

> [!IMPORTANT]
>
> - **Linux:** Confira seu sistema antes de atualizar: esta versão requer glibc 2.43 ou posterior, com Ubuntu 26.04 como referência. Se usa Ubuntu 24.04 ou anterior, atualize o sistema para usar esta versão do Solin. Use X11 ou Wayland com XWayland.
> - **Câmera virtual no macOS:** Para enviar as cenas ao Zoom, instale o OBS, habilite a extensão **OBS Virtual Camera** e selecione essa câmera no Zoom. Pare a saída no OBS antes de ativá-la no Solin. A extensão não é necessária para projetar cenas nas TVs ou no projetor.

### Adicionado

- **Cenas do Solin:**
  - No macOS, envie a composição completa das cenas para a projeção e a câmera virtual. Por exemplo, mostre uma mídia no projetor e uma composição com câmera no Zoom, controlando cada saída separadamente.
  - Acompanhe uma cena em uma janela maior enquanto trabalha no restante do Solin. Para câmeras PTZ compatíveis, mova, ajuste o zoom e salve presets com miniaturas para reconhecer cada enquadramento. Ao mover uma câmera que está no ar, o Solin pede confirmação.
  - Cadastre câmeras IP compatíveis com ONVIF usando a busca na rede ou por endereço. Escolha o fluxo de vídeo encontrado e deixe o Solin preencher as configurações RTSP e PTZ.
  - Troque de enquadramento com a transição **Fundir**: uma câmera compartilhada pelas duas cenas se move e muda de tamanho suavemente até a nova posição. As fontes que entram ou saem da composição aparecem ou desaparecem suavemente.
- **Publicações:**
  - Adicione apenas as mídias de que precisa: ao abrir um arquivo `.jwpub`, confira o conteúdo e selecione as imagens, os vídeos ou os áudios que deseja inserir na reunião ou playlist. A seleção também funciona com arquivos baixados pelo navegador do Solin.
  - Para preparar um discurso público, abra **Mídias de discursos públicos…**, indique o arquivo S-34mp com as mídias dos esboços e selecione as que serão usadas. O Solin lembra o arquivo escolhido e, nas reuniões de fim de semana, coloca as mídias na seção do discurso público.
- **Zoom (Windows):**
  - Prepare o Zoom com **Início da reunião**: o Solin conecta seu áudio e vídeo e silencia os participantes. Para a conversa antes ou depois, **Antes/depois da reunião** desconecta seu áudio e vídeo e solicita que os participantes ativem o microfone.
  - Automatize atalhos junto com esses controles: em **Atalhos Automáticos**, escolha os eventos **Antes da reunião**, **Início da reunião** ou **Fim da reunião** para executar as teclas configuradas após as ações do Zoom.
  - Vincule o cântico de fundo aos controles para pará-lo ao iniciar a reunião e reproduzi-lo antes ou depois. Se preferir usar a parada programada do cântico, ela também pode preparar o Zoom para o início da reunião.
- **Atualizações:** Consulte o que mudou em cada versão em **Configurações → Sobre**. Nesse mesmo local, escolha se deseja receber betas para testar novidades antes da estável; mostrar as notas das betas no histórico é uma opção separada.

### Alterado

- **Cenas do Solin:**
  - Controle projeção e câmera virtual separadamente pelos botões de monitor e câmera, inclusive no editor. Para mostrar a mesma composição nas duas saídas, selecione a mesma cena em ambas. Esses controles substituem **Transmitir ao vivo** e o vínculo de **Exibir nas janelas de mídia**. No painel de cenas, mostre os controles de projeção pelo botão de monitor quando precisar; eles ficam ocultos por padrão.
  - Encontre as cenas que ficaram fora da parte visível do painel usando a roda do mouse, o touchpad ou a barra de rolagem. O painel flutuante se ajusta ao espaço disponível na tela.
  - Monte enquadramentos que ultrapassam as bordas da cena: mova as fontes parcialmente para fora da área, inclusive com coordenadas negativas, e redimensione ou recorte sem que elas saltem de volta para dentro do quadro.
- **Gravação:** Grave reuniões ocupando menos espaço, com a codificação otimizada e 720p como resolução padrão. Escolha 360p, 540p, 720p ou 1080p conforme a nitidez e o tamanho de arquivo desejados, sem alterar a resolução da câmera virtual enviada ao Zoom.
- **Cânticos de fundo:** Teste ou reproduza um vídeo ou áudio sem ouvir dois sons ao mesmo tempo: o Solin silencia o cântico de fundo automaticamente e restaura o volume quando você sai da mídia. O cântico continua avançando em silêncio e não volta a tocar durante pausas ou carregamentos.
- **Armazenamento de reuniões:** O Solin usa o espaço em disco de forma mais inteligente: limpa extrações temporárias, descarta publicações antigas sem uso do cache e guarda uma única cópia de imagens iguais usadas por vários perfis. Isso pode reduzir significativamente o armazenamento acumulado, especialmente ao preparar muitas reuniões ou usar vários idiomas e perfis.
- **Pastas vinculadas:** Coloque mídias na pasta de uma reunião para que apareçam no Solin, mesmo sem sincronizar a organização da reunião entre computadores. Agora você pode criar essa pasta pelo Solin e mantê-la ao desativar a sincronização; excluí-la é uma ação separada, com confirmação. Também é possível criar pastas de playlists vinculadas pelo aplicativo.
- **Perfis:**
  - Encontre e selecione perfis com mais facilidade em janelas pequenas ou quando há muitos perfis. A tela se adapta ao espaço disponível, com rolagem e navegação pelo teclado.
  - Ao criar um perfil, escolher a congregação já preenche o nome do perfil e ativa os cânticos de fundo automáticos quando há horários de reunião disponíveis. Nomes de perfil editados por você são preservados.

### Corrigido

- **Câmeras:**
  - Conecte uma câmera com o Solin aberto e ela aparecerá na lista automaticamente. No Windows, reconectar uma câmera que já está na cena permite recuperar sua imagem sem reiniciar o Solin, inclusive se ela estava desconectada ao abrir o aplicativo.
  - Acompanhe câmeras IP com menos atraso acrescentado pelo Solin: a recepção RTSP foi ajustada para priorizar a imagem ao vivo e dispensar o processamento do áudio da câmera. Também foram corrigidas a aplicação das opções TCP/UDP e de buffer e a exibição nas pré-visualizações, na projeção e nas miniaturas com a câmera virtual desligada.
- **Câmera virtual:** Identifique quando o vídeo realmente está chegando à saída. Se o envio travar, o Solin tenta recuperá-lo automaticamente e avisa se não conseguir, em vez de manter a indicação de funcionamento sem vídeo.
- **Reprodução:** Mais estabilidade ao reproduzir arquivos locais ou pela rede, com correções de falhas ao mudar a velocidade, avançar ou retroceder e retomar após interrupções de conexão. Também foram corrigidos reinícios indevidos ao terminar uma mídia. No Windows, melhorada a continuidade do áudio para evitar cortes, estalos e ruídos de fundo que podiam ser mais perceptíveis em computadores mais modestos.
- **Reuniões:**
  - A escolha de baixar manualmente agora também vale para as publicações da reunião. Antes, elas ainda eram baixadas durante a navegação, mesmo com os downloads automáticos desativados. Agora, clique em **Baixar** quando quiser preparar uma reunião ainda não disponível.
  - Reuniões já preparadas recebem atualizações de conteúdo e recuperam imagens ausentes preservando suas edições, reduzindo a necessidade de refazer a preparação quando uma imagem deixa de aparecer.
- **Projeção:**
  - Mover o mouse para a tela de projeção não deixa mais o ponteiro sobre o conteúdo mostrado à assistência.
  - Ao ajustar uma foto ou ilustração de uma reunião ou playlist na pré-visualização interativa, as opções **Ajustar à proporção da projeção** e **Manter a imagem cobrindo o quadro** podiam alterar ou desfazer o zoom e a posição da imagem na TV sem clicar em **Aplicar ao projetor**. Agora, essas opções ajustam apenas a prévia; a imagem projetada só muda quando você aplica os ajustes.
- **Atualizações:** Abra links das notas de versão sem perder o texto que estava lendo. Em instalações compatíveis do Windows, clique em **Atualizar agora** uma única vez: após baixar e verificar o pacote, o Solin inicia o instalador.
- **Diálogos:** Ao abrir notificações, atualizações ou escolher o destino de uma mídia, a janela aparece centralizada no Solin, inclusive depois de movê-lo para outro monitor.

### Removido

- **Zoom (Windows):** Removida a contagem de assistência, que podia deixar de contar participantes fora da parte visível da lista do Zoom.
